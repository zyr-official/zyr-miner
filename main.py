import os
import json
import time
import base64
import sqlite3
import hashlib
import hmac
import math
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update, WebAppInfo
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, CommandHandler

# ====== تنظیمات ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "9204005359:AAGJ-532L12GA8T_w01ORxpH1HhLDl28Bqc")

ADMIN_ID = 1839907139
WEBAPP_URL = "https://zyr-official.github.io/zyr-miner/app.html"
CHANNEL_ID = "@ZYRMining"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "zyr_secret_change_me")

# دیتابیس باید روی دیسک پایدار باشد؛ /tmp پاک می‌شود و باعث "User not found" و ریست ماینینگ می‌شد
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "zyr.db"))

TON_RECEIVER = os.getenv("TON_RECEIVER", "UQBke3M55AgQ7qRoAHf1Li9t_c-ZEjnugAmMZ5cTRCg1oo24")
PAY_NANO = 100_000_000  # 0.1 TON
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
# فقط برای تست بیرون از تلگرام: ALLOW_UNVERIFIED=1
ALLOW_UNVERIFIED = os.getenv("ALLOW_UNVERIFIED", "0") == "1"
MIN_WITHDRAW = 10000
# =====================


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


@contextmanager
def db_tx():
    """تراکنش اتمیک؛ در صورت خطا rollback می‌شود."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn.cursor()
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        first_name TEXT,
        balance REAL DEFAULT 0,
        referrer_id TEXT,
        referral_earned REAL DEFAULT 0,
        has_paid INTEGER DEFAULT 0,
        last_mining REAL DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id TEXT,
        friend_id TEXT,
        reward_claimed INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, amount REAL, address TEXT, status TEXT
    )''')

    # جلوگیری از استفاده‌ی چندباره از یک تراکنش پرداخت
    c.execute('''CREATE TABLE IF NOT EXISTS used_payments (
        tx_key TEXT PRIMARY KEY,
        user_id TEXT,
        created REAL
    )''')
    conn.close()


init_db()


class UserSyncRequest(BaseModel):
    user_id: str | None = None
    first_name: str = "ZYR User"
    referrer_id: str | None = None
    init_data: str | None = None


class ClaimMiningRequest(BaseModel):
    user_id: str | None = None
    amount: float | None = None
    init_data: str | None = None


class WithdrawRequest(BaseModel):
    user_id: str | None = None
    username: str = "ZYR User"
    amount: float
    address: str
    init_data: str | None = None


class MemberCheckRequest(BaseModel):
    user_id: str | None = None
    init_data: str | None = None


class ConfirmPaymentRequest(BaseModel):
    user_id: str | None = None
    tx_hash: str | None = None
    wallet_address: str | None = None
    init_data: str | None = None


class ClaimRefRequest(BaseModel):
    user_id: str | None = None
    friend_id: str
    init_data: str | None = None


def verify_init_data(init_data: str) -> str | None:
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, received_hash):
            return None
        user = json.loads(parsed.get("user", "{}"))
        uid = user.get("id")
        return str(uid) if uid else None
    except Exception as e:
        print(f"[verify_init_data] {e}")
        return None


def resolve_user_id(req) -> str:
    init_data = getattr(req, 'init_data', None)
    if init_data:
        verified = verify_init_data(init_data)
        if verified:
            return verified
        if not ALLOW_UNVERIFIED:
            raise HTTPException(status_code=401, detail="Invalid Telegram session, reopen the app")
    if ALLOW_UNVERIFIED and getattr(req, 'user_id', None):
        return str(req.user_id)
    raise HTTPException(status_code=401, detail="Please open the app from Telegram")


def get_rate(c, uid: str):
    c.execute("SELECT COUNT(*) as count FROM users WHERE referrer_id=?", (uid,))
    invited_count = c.fetchone()['count']
    if invited_count >= 10:
        return 150, 4, invited_count
    if invited_count >= 6:
        return 130, 3, invited_count
    if invited_count >= 3:
        return 110, 2, invited_count
    return 80, 1, invited_count


telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/check-member")
async def check_member(req: MemberCheckRequest):
    uid = resolve_user_id(req)
    bot = Bot(token=BOT_TOKEN)
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=int(uid))
        if member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return {"isMember": True}
        return {"isMember": False}
    except Exception as e:
        return {"isMember": False, "error": str(e)}


@app.post("/sync-user")
async def sync_user(req: UserSyncRequest):
    uid = resolve_user_id(req)
    current_time = time.time()
    first_name = (req.first_name or "ZYR User")[:64]

    with db_tx() as c:
        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        user = c.fetchone()

        if not user:
            c.execute(
                "INSERT INTO users (id, first_name, last_mining) VALUES (?, ?, ?)",
                (uid, first_name, current_time)
            )
            if req.referrer_id:
                code = str(req.referrer_id).strip()
                c.execute("SELECT id FROM users WHERE substr(id, -6) = ? AND id != ? LIMIT 1", (code, uid))
                ref_user = c.fetchone()
                if ref_user:
                    c.execute("INSERT INTO referrals (referrer_id, friend_id, reward_claimed) VALUES (?, ?, 0)", (ref_user['id'], uid))
                    c.execute("UPDATE users SET referrer_id=? WHERE id=?", (ref_user['id'], uid))
            c.execute("SELECT * FROM users WHERE id=?", (uid,))
            user = c.fetchone()
        elif first_name and user['first_name'] != first_name:
            c.execute("UPDATE users SET first_name=? WHERE id=?", (first_name, uid))

        if uid == str(ADMIN_ID) and not user['has_paid']:
            c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (current_time, uid))
            c.execute("SELECT * FROM users WHERE id=?", (uid,))
            user = c.fetchone()

        rate_per_day, level, invited_count = get_rate(c, uid)

        c.execute("SELECT COUNT(*) as count FROM users u JOIN referrals r ON u.id = r.friend_id WHERE r.referrer_id=? AND u.has_paid=1", (uid,))
        active_invited_count = c.fetchone()['count']

        tokens_per_second = rate_per_day / 86400
        last_mining = user['last_mining'] if (user['last_mining'] and user['last_mining'] > 0) else current_time
        time_diff = max(0, current_time - last_mining)
        pending_mined = time_diff * tokens_per_second if user['has_paid'] else 0

        c.execute("SELECT u.id, u.first_name, u.has_paid, r.reward_claimed FROM referrals r JOIN users u ON r.friend_id = u.id WHERE r.referrer_id=?", (uid,))
        friends_list = [{
            "id": row['id'],
            "name": row['first_name'],
            "has_paid": bool(row['has_paid']),
            "reward_claimed": bool(row['reward_claimed'])
        } for row in c.fetchall()]

        result = {
            "balance": round(user['balance'], 5),
            "pending_mined": round(pending_mined, 5),
            "has_paid": bool(user['has_paid']),
            "invited": invited_count,
            "active_invited": active_invited_count,
            "referral_earned": round(user['referral_earned'], 2),
            "rate_per_day": rate_per_day,
            "level": level,
            "friends": friends_list
        }
    return result


@app.post("/claim-mining")
async def claim_mining(req: ClaimMiningRequest):
    uid = resolve_user_id(req)
    try:
        with db_tx() as c:
            c.execute("SELECT balance, last_mining, has_paid FROM users WHERE id=?", (uid,))
            user = c.fetchone()
            current_time = time.time()

            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            if not user['has_paid']:
                raise HTTPException(status_code=400, detail="Miner not activated")

            rate_per_day, _, _ = get_rate(c, uid)
            tokens_per_sec = rate_per_day / 86400
            last_mining = user['last_mining'] if (user['last_mining'] and user['last_mining'] > 0) else current_time
            time_diff = max(0, current_time - last_mining)
            server_pending = time_diff * tokens_per_sec

            new_balance = user['balance'] + server_pending
            c.execute("UPDATE users SET balance = ?, last_mining = ? WHERE id = ?", (new_balance, current_time, uid))
        return {"ok": True, "new_balance": round(new_balance, 5), "claimed": round(server_pending, 5)}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[CLAIM ERROR] {e}")
        raise HTTPException(status_code=500, detail="Server error, try again")


# ---------- تایید واقعی پرداخت روی بلاکچین ----------
def addr_hash(addr: str) -> str | None:
    """هش ۳۲ بایتی آدرس را از فرمت raw یا user-friendly بیرون می‌کشد."""
    try:
        addr = addr.strip()
        if ":" in addr:
            return addr.split(":", 1)[1].lower()
        a = addr.replace("+", "-").replace("/", "_")
        raw = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
        if len(raw) == 36:
            return raw[2:34].hex()
    except Exception:
        pass
    return None


async def find_payments(sender_addr: str) -> list[str]:
    want = addr_hash(sender_addr)
    if not want:
        return []
    headers = {"X-API-Key": TONCENTER_API_KEY} if TONCENTER_API_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://toncenter.com/api/v2/getTransactions",
                params={"address": TON_RECEIVER, "limit": 40},
                headers=headers,
            )
        data = r.json()
    except Exception as e:
        print(f"[PAYMENT] toncenter error: {e}")
        return []
    if not data.get("ok"):
        return []

    now = time.time()
    keys = []
    for tx in data.get("result", []):
        msg = tx.get("in_msg") or {}
        src = msg.get("source")
        if not src:
            continue
        try:
            value = int(msg.get("value", "0"))
        except Exception:
            continue
        if value < PAY_NANO:
            continue
        if addr_hash(src) != want:
            continue
        if now - tx.get("utime", 0) > 3600:
            continue
        tid = tx.get("transaction_id", {})
        keys.append(f"{tid.get('lt')}:{tid.get('hash')}")
    return keys


@app.post("/confirm-payment")
async def confirm_payment(req: ConfirmPaymentRequest):
    uid = resolve_user_id(req)

    conn = get_db()
    row = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if row and row['has_paid']:
        return {"ok": True, "message": "Already active"}

    if not req.wallet_address:
        raise HTTPException(status_code=400, detail="wallet_address required")

    keys = await find_payments(req.wallet_address)
    if not keys:
        return {"ok": False, "pending": True, "message": "Payment not found yet"}

    current_time = time.time()
    with db_tx() as c:
        for k in keys:
            c.execute("SELECT 1 FROM used_payments WHERE tx_key=?", (k,))
            if c.fetchone():
                continue
            c.execute("INSERT INTO used_payments (tx_key, user_id, created) VALUES (?, ?, ?)", (k, uid, current_time))
            c.execute("SELECT id FROM users WHERE id=?", (uid,))
            if c.fetchone():
                c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (current_time, uid))
            else:
                c.execute("INSERT INTO users (id, has_paid, last_mining) VALUES (?, 1, ?)", (uid, current_time))
            return {"ok": True, "message": "Payment confirmed"}
    return {"ok": False, "pending": True, "message": "Payment already used"}


@app.post("/claim-ref-reward")
async def claim_ref_reward(req: ClaimRefRequest):
    uid = resolve_user_id(req)
    with db_tx() as c:
        c.execute("SELECT * FROM referrals WHERE referrer_id=? AND friend_id=? AND reward_claimed=0", (uid, req.friend_id))
        ref = c.fetchone()
        if not ref:
            return {"ok": False, "message": "Reward not available"}

        c.execute("SELECT has_paid FROM users WHERE id=?", (req.friend_id,))
        friend = c.fetchone()
        if not friend or not friend['has_paid']:
            return {"ok": False, "message": "Friend has not paid 0.1 TON yet"}

        c.execute("UPDATE referrals SET reward_claimed = 1 WHERE id=? AND reward_claimed=0", (ref['id'],))
        if c.rowcount == 0:
            return {"ok": False, "message": "Reward not available"}
        c.execute("UPDATE users SET balance = balance + 300, referral_earned = referral_earned + 300 WHERE id=?", (uid,))
    return {"ok": True, "message": "300 ZYR claimed!"}


@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawRequest):
    uid = resolve_user_id(req)
    amount = req.amount
    address = (req.address or "").strip()

    if not math.isfinite(amount) or amount < MIN_WITHDRAW:
        raise HTTPException(status_code=400, detail=f"Minimum is {MIN_WITHDRAW} ZYR")
    if len(address) < 48:
        raise HTTPException(status_code=400, detail="Invalid TON address")

    with db_tx() as c:
        c.execute("UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?", (amount, uid, amount))
        if c.rowcount == 0:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        c.execute(
            "INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?, ?, ?, 'pending')",
            (uid, amount, address)
        )
        wd_id = c.lastrowid

    bot = Bot(token=BOT_TOKEN)
    text = (f"🔴 New Withdrawal Request\n\n"
            f"👤 User: {req.username} ({uid})\n"
            f"💰 Amount: {amount} ZYR\n"
            f"📫 Address: {address}")

    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}_{uid}_{amount}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}_{uid}_{amount}")
    ]]

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        print(f"[WITHDRAW] error: {e}")

    return {"ok": True}


@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse({
        "url": WEBAPP_URL,
        "name": "ZYR Miner",
        "iconUrl": "https://zyr-official.github.io/zyr-miner/icon.png"
    })


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user_name = update.effective_user.first_name or "Miner"
    ref_id = None
    if context.args and context.args[0].startswith("ref_"):
        ref_id = context.args[0].replace("ref_", "")

    url = WEBAPP_URL
    if ref_id:
        url += f"?startapp=ref_{ref_id}"

    welcome_text = (
        f"⚡ **Welcome to ZYR Cloud Miner, {user_name}!**\n\n"
        f"⛏️ **Mine ZYR inside the app:** Start your session, claim tokens, and withdraw to TON.\n"
        f"💎 **Activate your miner:** Deposit 0.1 TON to start earning 80 ZYR daily.\n"
        f"👥 **Invite friends:** Boost your mining rate and earn 300 ZYR when they pay 0.1 TON.\n\n"
        f"👇 **Tap below to start!**"
    )
    keyboard = [[InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url))]]
    try:
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Exception:
        # اگر اسم کاربر کاراکتر خاص مارک‌داون داشت
        await update.message.reply_text(welcome_text.replace("*", ""), reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user or query.from_user.id != ADMIN_ID:
        if query:
            await query.answer("Not allowed", show_alert=True)
        return
    await query.answer()
    try:
        parts = query.data.split("_", 3)
        action, wd_id = parts[0], int(parts[1])
    except Exception:
        await query.edit_message_text("❌ Invalid callback data.")
        return

    with db_tx() as c:
        c.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,))
        wd = c.fetchone()
        if not wd or wd['status'] != 'pending':
            text = "⚠️ This request was already processed."
        elif action == "approve":
            c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
            text = f"✅ Approved {wd['amount']} ZYR for user {wd['user_id']}.\n📫 {wd['address']}"
        elif action == "reject":
            c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (wd['amount'], wd['user_id']))
            c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
            text = f"❌ Rejected {wd['amount']} ZYR. Funds returned."
        else:
            text = "❌ Unknown action."
    await query.edit_message_text(text=text)


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        print(f"[WEBHOOK] error: {e}")
    return {"ok": True}


@app.get("/")
def read_root():
    return {"status": "Backend is running"}
