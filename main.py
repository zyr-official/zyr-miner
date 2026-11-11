import os
import sqlite3
import time
import hmac
import hashlib
import json
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    CommandHandler,
)

# ============================================================
# =======================  تنظیمات  ==========================
# ============================================================
BOT_TOKEN   = os.getenv("BOT_TOKEN", "9204005359:AAGJ-532L12GA8T_w01ORxpH1HhLDl28Bqc")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "1839907139"))
WEBAPP_URL  = os.getenv("WEBAPP_URL", "https://zyr-official.github.io/zyr-miner/")
BACKEND_URL = os.getenv("BACKEND_URL", "https://nann.pingooshop.ir")
CHANNEL_ID  = os.getenv("CHANNEL_ID", "@ZYRMining")
ICON_URL    = os.getenv("ICON_URL", "https://zyr-official.github.io/zyr-miner/icon.png")
DB_PATH     = os.getenv("DB_PATH", "/tmp/zyr.db")

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN is required!")

# ============================================================
# ======================  دیتابیس  ===========================
# ============================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            first_name TEXT,
            balance REAL DEFAULT 0,
            referrer_id TEXT,
            referral_earned REAL DEFAULT 0,
            has_paid INTEGER DEFAULT 0,
            last_mining REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            amount REAL,
            address TEXT,
            status TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()

# ============================================================
# ==============  اعتبارسنجی initData تلگرام  ================
# ============================================================
def validate_init_data(init_data: str) -> dict | None:
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            return None
        return parsed
    except Exception:
        return None


def get_verified_user_id(init_data: str | None) -> str:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")
    parsed = validate_init_data(init_data)
    if not parsed:
        raise HTTPException(status_code=401, detail="Invalid initData")
    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Malformed user in initData")
    uid = str(user.get("id", ""))
    if not uid:
        raise HTTPException(status_code=401, detail="No user id in initData")
    return uid


# ============================================================
# ===============  منطق نرخ ماینینگ  =========================
# ============================================================
def calc_rate(active_invited: int) -> tuple[float, int]:
    if active_invited >= 20:
        return 150.0, 4
    if active_invited >= 10:
        return 130.0, 3
    if active_invited >= 5:
        return 110.0, 2
    return 80.0, 1


# ============================================================
# ===============  مدل‌های درخواست  ===========================
# ============================================================
class UserSyncRequest(BaseModel):
    user_id: str
    first_name: str
    referrer_id: str | None = None


class ClaimRequest(BaseModel):
    user_id: str


class WithdrawRequest(BaseModel):
    user_id: str
    username: str
    amount: float
    address: str


class MemberCheckRequest(BaseModel):
    user_id: str


# ============================================================
# ==============  ساخت اپلیکیشن FastAPI  =====================
# ============================================================
telegram_app = Application.builder().token(BOT_TOKEN).build()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    if BACKEND_URL.startswith("https://"):
        try:
            await telegram_app.bot.set_webhook(
                f"{BACKEND_URL}/api/webhook",
                drop_pending_updates=True,
            )
            print(f"✅ Webhook set to {BACKEND_URL}/api/webhook")
        except Exception as e:
            print(f"⚠️ Webhook setup failed: {e}")

    yield

    await telegram_app.stop()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# ====================  API Endpoints  =======================
# ============================================================
@app.post("/check-member")
async def check_member(req: MemberCheckRequest):
    if req.user_id == str(ADMIN_ID):
        return {"isMember": True}

    bot = Bot(token=BOT_TOKEN)
    try:
        member = await bot.get_chat_member(
            chat_id=CHANNEL_ID, user_id=int(req.user_id)
        )
        return {"isMember": member.status in ("member", "administrator", "creator")}
    except Exception as e:
        return {"isMember": False, "error": str(e)}


@app.post("/sync-user")
async def sync_user(req: UserSyncRequest, x_init_data: str | None = Header(None)):
    verified_id = get_verified_user_id(x_init_data)
    if verified_id != req.user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch")

    conn = get_db()
    c = conn.cursor()
    is_admin = req.user_id == str(ADMIN_ID)

    c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()

    if not user:
        ref_id = req.referrer_id if req.referrer_id != req.user_id else None
        c.execute(
            """INSERT INTO users (id, first_name, referrer_id, has_paid, last_mining)
               VALUES (?, ?, ?, ?, ?)""",
            (req.user_id, req.first_name, ref_id, 1 if is_admin else 0, time.time()),
        )
        conn.commit()
    else:
        if is_admin and not user["has_paid"]:
            c.execute("UPDATE users SET has_paid=1 WHERE id=?", (req.user_id,))
            conn.commit()
        if (
            not user["referrer_id"]
            and req.referrer_id
            and req.referrer_id != req.user_id
        ):
            c.execute(
                "UPDATE users SET referrer_id=? WHERE id=?",
                (req.referrer_id, req.user_id),
            )
            conn.commit()

    c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()

    c.execute("SELECT COUNT(*) AS c FROM users WHERE referrer_id=?", (req.user_id,))
    invited_count = c.fetchone()["c"]

    c.execute(
        "SELECT COUNT(*) AS c FROM users WHERE referrer_id=? AND has_paid=1",
        (req.user_id,),
    )
    active_invited = c.fetchone()["c"]
    inactive_invited = invited_count - active_invited

    rate_per_day, level = calc_rate(active_invited)
    tokens_per_second = rate_per_day / 86400
    pending_earned = (time.time() - user["last_mining"]) * tokens_per_second

    conn.close()

    return {
        "balance": round(user["balance"], 5),
        "pending": round(pending_earned, 5),
        "has_paid": bool(user["has_paid"]),
        "invited": invited_count,
        "active_invited": active_invited,
        "inactive_invited": inactive_invited,
        "referral_earned": round(user["referral_earned"], 2),
        "rate_per_day": rate_per_day,
        "level": level,
    }


@app.post("/claim")
async def claim_tokens(req: ClaimRequest, x_init_data: str | None = Header(None)):
    verified_id = get_verified_user_id(x_init_data)
    if verified_id != req.user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()

    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    if not user["has_paid"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Activate your miner first")

    c.execute(
        "SELECT COUNT(*) AS c FROM users WHERE referrer_id=? AND has_paid=1",
        (req.user_id,),
    )
    active_invited = c.fetchone()["c"]
    rate_per_day, _ = calc_rate(active_invited)

    earned = (time.time() - user["last_mining"]) * (rate_per_day / 86400)
    new_balance = user["balance"] + earned

    c.execute(
        "UPDATE users SET balance=?, last_mining=? WHERE id=?",
        (new_balance, time.time(), req.user_id),
    )
    conn.commit()
    conn.close()

    return {"ok": True, "balance": round(new_balance, 5), "pending": 0}


@app.post("/confirm-payment")
async def confirm_payment(req: MemberCheckRequest):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()

    if user and not user["has_paid"]:
        c.execute("UPDATE users SET has_paid=1 WHERE id=?", (req.user_id,))
        if user["referrer_id"]:
            c.execute(
                """UPDATE users
                   SET balance = balance + 300,
                       referral_earned = referral_earned + 300
                   WHERE id=?""",
                (user["referrer_id"],),
            )
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Payment confirmed"}

    conn.close()
    return {"ok": False, "message": "Already paid or user not found"}


@app.post("/request-withdraw")
async def request_withdraw(
    req: WithdrawRequest, x_init_data: str | None = Header(None)
):
    verified_id = get_verified_user_id(x_init_data)
    if verified_id != req.user_id:
        raise HTTPException(status_code=403, detail="user_id mismatch")

    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()

    if not user or user["balance"] < req.amount:
        conn.close()
        raise HTTPException(status_code=400, detail="Insufficient balance")

    c.execute(
        """INSERT INTO withdrawals (user_id, amount, address, status)
           VALUES (?, ?, ?, 'pending')""",
        (req.user_id, req.amount, req.address),
    )
    c.execute(
        "UPDATE users SET balance = balance - ? WHERE id=?",
        (req.amount, req.user_id),
    )
    wd_id = c.lastrowid
    conn.commit()
    conn.close()

    bot = Bot(token=BOT_TOKEN)
    text = (
        f"🔴 **New Withdrawal Request**\n\n"
        f"👤 User: {req.username} (`{req.user_id}`)\n"
        f"💰 Amount: {req.amount} ZYR\n"
        f"📫 Address: `{req.address}`"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"approve_{wd_id}_{req.user_id}_{req.amount}",
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"reject_{wd_id}_{req.user_id}_{req.amount}",
            ),
        ]
    ]
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )
    return {"ok": True}


@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse(
        {
            "url": WEBAPP_URL,
            "name": "ZYR Miner",
            "iconUrl": ICON_URL,
        }
    )


# ============================================================
# =====================  Bot Handlers  =======================
# ============================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "Miner"
    ref_id = None
    if context.args and context.args[0].startswith("ref_"):
        ref_id = context.args[0].replace("ref_", "")

    url = WEBAPP_URL
    if ref_id:
        url += f"?startapp=ref_{ref_id}"

    welcome_text = (
        f"⚡ **Welcome to ZYR Cloud Miner, {user_name}!**\n\n"
        f"⛏️ Mine ZYR inside the app\n"
        f"💎 Activate your miner with 0.1 TON\n"
        f"👥 Invite friends for +300 ZYR\n\n"
        f"👇 **Tap below to start!**"
    )
    keyboard = [
        [InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url))]
    ]
    await update.message.reply_text(
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    if len(parts) < 4:
        return

    action, wd_id, user_id = parts[0], parts[1], parts[2]
    try:
        amount = float(parts[3])
    except ValueError:
        return

    conn = get_db()
    c = conn.cursor()

    if action == "approve":
        c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
        await query.edit_message_text(
            text=f"✅ Approved {amount} ZYR for user {user_id}."
        )
    elif action == "reject":
        c.execute(
            "UPDATE users SET balance = balance + ? WHERE id=?",
            (amount, user_id),
        )
        c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
        await query.edit_message_text(
            text=f"❌ Rejected {amount} ZYR. Funds returned."
        )

    conn.commit()
    conn.close()


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


# ============================================================
# =====================  Webhook / Root  =====================
# ============================================================
@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False}


@app.get("/")
def read_root():
    return {"status": "Backend is running", "bot": "ZYR Miner"}
