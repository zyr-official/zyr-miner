import os
import re
import io
import json
import time
import hmac
import html
import base64
import hashlib
import sqlite3
import logging
from contextlib import contextmanager, asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, Update,
                      WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zyr")

# ====================== تنظیمات (همه از متغیر محیطی) ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9204005359:AAFFb-wJeEsKmNrOJSczuGGLx-CBv1TS2Ko")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1839907139"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://zyr-official.github.io/zyr-miner/")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ZYRMining")
BOT_USERNAME = os.getenv("BOT_USERNAME", "ZYRMining_bot")
APP_SHORT_NAME = os.getenv("APP_SHORT_NAME", "app")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://zyr-official.github.io")

TON_WALLET = os.getenv("TON_WALLET", "UQBke3M55AgQ7qRoAHf1Li9t_c-ZEjnugAmMZ5cTRCg1oo24")  # آدرس کیف پول دریافت‌کننده ۰.۱ TON
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
PRICE_TON = 0.1
PRICE_NANO = int(PRICE_TON * 1_000_000_000)

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://nann.pingooshop.ir")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "oVrDynBQ9thd1xBd_51RAkUUxQnkFUfAWlddE_B3-F0")

DB_PATH = os.getenv("DB_PATH", "zyr.db")                  # روی دیسک پایدار بذار
REFERRAL_REWARD = 300
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "100"))
INITDATA_MAX_AGE = 24 * 3600
# ==========================================================================


# ------------------------------- دیتابیس -------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            referrer_id TEXT,
            referral_earned REAL NOT NULL DEFAULT 0,
            has_paid INTEGER NOT NULL DEFAULT 0,
            last_mining REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ref ON users(referrer_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS payments (
            tx_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount_nano INTEGER NOT NULL,
            created_at REAL NOT NULL
        )""")


init_db()


# --------------------------- احراز هویت تلگرام ---------------------------
def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "Missing initData")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(401, "Bad initData")
    received = pairs.pop("hash", None)
    if not received:
        raise HTTPException(401, "Bad initData")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401, "Invalid signature")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INITDATA_MAX_AGE:
            raise HTTPException(401, "initData expired")
        user = json.loads(pairs["user"])
        uid = str(int(user["id"]))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Bad initData")
    return {
        "id": uid,
        "first_name": (user.get("first_name") or "Miner")[:64],
        "username": user.get("username") or "",
        "start_param": pairs.get("start_param"),
    }


def clean_ref(value) -> str | None:
    if not value:
        return None
    value = str(value)
    if value.startswith("ref_"):
        value = value[4:]
    return value if value.isdigit() and len(value) <= 15 else None


# ------------------------------ منطق بازی ------------------------------
def level_info(active_invited: int) -> tuple[int, int]:
    """(rate_per_day, level)"""
    if active_invited >= 20:
        return 150, 4
    if active_invited >= 10:
        return 130, 3
    if active_invited >= 5:
        return 110, 2
    return 80, 1


def count_active(conn, uid: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM users WHERE referrer_id=? AND has_paid=1", (uid,)
    ).fetchone()["c"]


def count_invited(conn, uid: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM users WHERE referrer_id=?", (uid,)
    ).fetchone()["c"]


def pending_for(user, rate_per_day: int) -> float:
    if not user["has_paid"]:
        return 0.0
    return max(0.0, (time.time() - user["last_mining"]) * rate_per_day / 86400)


async def is_member(uid: str) -> bool:
    if int(uid) == ADMIN_ID:
        return True
    try:
        m = await telegram_app.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=int(uid))
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("member check failed: %s", e)
        return False


# ------------------------------- اپلیکیشن -------------------------------
telegram_app = Application.builder().token(BOT_TOKEN).build()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await telegram_app.initialize()
    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(
            url=WEBHOOK_URL.rstrip("/") + "/api/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
        )
    yield
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Init-Data"],
)


class SyncReq(BaseModel):
    referrer_id: str | None = None


class WithdrawReq(BaseModel):
    amount: float
    address: str


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/config")
def config():
    return {
        "wallet": TON_WALLET,
        "price_nano": PRICE_NANO,
        "price_ton": PRICE_TON,
        "min_withdraw": MIN_WITHDRAW,
        "bot_username": BOT_USERNAME,
        "app_short_name": APP_SHORT_NAME,
        "channel": CHANNEL_ID,
        "referral_reward": REFERRAL_REWARD,
    }


@app.get("/tonconnect-manifest.json")
def manifest():
    return JSONResponse({
        "url": WEBAPP_URL,
        "name": "ZYR Miner",
        "iconUrl": WEBAPP_URL.rstrip("/") + "/icon.png",
    })


@app.post("/check-member")
async def check_member(x_init_data: str = Header(default="")):
    u = verify_init_data(x_init_data)
    return {"isMember": await is_member(u["id"])}


@app.post("/sync-user")
def sync_user(req: SyncReq, x_init_data: str = Header(default="")):
    u = verify_init_data(x_init_data)
    uid = u["id"]
    ref = clean_ref(u["start_param"]) or clean_ref(req.referrer_id)
    if ref == uid:
        ref = None

    with db() as conn:
        with tx(conn):
            if ref and not conn.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
                ref = None
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            now = time.time()
            if not user:
                conn.execute(
                    "INSERT INTO users (id, first_name, referrer_id, last_mining, created_at) "
                    "VALUES (?,?,?,?,?)", (uid, u["first_name"], ref, now, now))
            elif not user["referrer_id"] and ref and not user["has_paid"]:
                conn.execute("UPDATE users SET referrer_id=? WHERE id=?", (ref, uid))
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

        invited = count_invited(conn, uid)
        active = count_active(conn, uid)
        rate, level = level_info(active)
        return {
            "balance": round(user["balance"], 5),
            "pending": round(pending_for(user, rate), 5),
            "has_paid": bool(user["has_paid"]),
            "invited": invited,
            "active_invited": active,
            "inactive_invited": invited - active,
            "referral_earned": round(user["referral_earned"], 2),
            "rate_per_day": rate,
            "level": level,
        }


@app.post("/claim")
async def claim(x_init_data: str = Header(default="")):
    u = verify_init_data(x_init_data)
    uid = u["id"]
    if not await is_member(uid):
        raise HTTPException(403, "Join the channel first")
    with db() as conn:
        with tx(conn):
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not user:
                raise HTTPException(404, "User not found")
            if not user["has_paid"]:
                raise HTTPException(400, "Activate your miner first")
            rate, _ = level_info(count_active(conn, uid))
            earned = pending_for(user, rate)
            now = time.time()
            conn.execute(
                "UPDATE users SET balance=balance+?, last_mining=? WHERE id=?",
                (earned, now, uid))
            bal = conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    return {"ok": True, "balance": round(bal, 5), "pending": 0, "claimed": round(earned, 5)}


# ------------------------ تأیید پرداخت از روی بلاکچین ------------------------
def _comment_of(in_msg: dict) -> str:
    msg = in_msg.get("message")
    if msg:
        return str(msg).strip()
    md = in_msg.get("msg_data") or {}
    if md.get("text"):
        try:
            return base64.b64decode(md["text"]).decode("utf-8", "ignore").strip()
        except Exception:
            return ""
    return ""


async def find_payment(uid: str) -> tuple[str, int] | None:
    expected = f"zyr-{uid}"
    headers = {"X-API-Key": TONCENTER_API_KEY} if TONCENTER_API_KEY else {}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://toncenter.com/api/v2/getTransactions",
            params={"address": TON_WALLET, "limit": 100},
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
    if not data.get("ok"):
        return None
    for t in data.get("result", []):
        m = t.get("in_msg") or {}
        if not m.get("source"):          # پیام خارجی؛ فقط انتقال داخلی قبول است
            continue
        try:
            value = int(m.get("value", "0"))
        except ValueError:
            continue
        if value >= PRICE_NANO and _comment_of(m) == expected:
            return t["transaction_id"]["hash"], value
    return None


@app.post("/verify-payment")
async def verify_payment(x_init_data: str = Header(default="")):
    u = verify_init_data(x_init_data)
    uid = u["id"]

    with db() as conn:
        user = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        raise HTTPException(404, "User not found")
    if user["has_paid"]:
        return {"ok": True, "already": True}

    try:
        found = await find_payment(uid)
    except Exception as e:
        log.warning("toncenter error: %s", e)
        raise HTTPException(502, "Could not reach TON network, try again")
    if not found:
        return {"ok": False, "message": "Payment not found yet. Wait a minute after sending."}
    tx_hash, value = found

    with db() as conn:
        with tx(conn):
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if user["has_paid"]:
                return {"ok": True, "already": True}
            if conn.execute("SELECT 1 FROM payments WHERE tx_hash=?", (tx_hash,)).fetchone():
                raise HTTPException(409, "Transaction already used")
            now = time.time()
            conn.execute("INSERT INTO payments VALUES (?,?,?,?)", (tx_hash, uid, value, now))
            conn.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (now, uid))
            if user["referrer_id"]:
                conn.execute(
                    "UPDATE users SET balance=balance+?, referral_earned=referral_earned+? WHERE id=?",
                    (REFERRAL_REWARD, REFERRAL_REWARD, user["referrer_id"]))
    return {"ok": True}


# -------------------------------- برداشت --------------------------------
ADDR_RE = re.compile(r"^([A-Za-z0-9_\-]{48}|-?\d:[0-9a-fA-F]{64})$")


@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawReq, x_init_data: str = Header(default="")):
    u = verify_init_data(x_init_data)
    uid = u["id"]
    amount = round(req.amount, 5)
    address = req.address.strip()

    if not (amount == amount) or amount < MIN_WITHDRAW:      # NaN و کمتر از حداقل
        raise HTTPException(400, f"Minimum withdrawal is {MIN_WITHDRAW} ZYR")
    if not ADDR_RE.match(address):
        raise HTTPException(400, "Invalid TON address")
    if not await is_member(uid):
        raise HTTPException(403, "Join the channel first")

    with db() as conn:
        with tx(conn):
            user = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
            if not user or not user["has_paid"]:
                raise HTTPException(400, "Activate your miner first")
            cur = conn.execute(
                "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                (amount, uid, amount))
            if cur.rowcount != 1:
                raise HTTPException(400, "Insufficient balance")
            cur = conn.execute(
                "INSERT INTO withdrawals (user_id, amount, address, status, created_at) "
                "VALUES (?,?,?,'pending',?)", (uid, amount, address, time.time()))
            wd_id = cur.lastrowid

    name = html.escape(u["first_name"])
    uname = f"@{html.escape(u['username'])}" if u["username"] else "-"
    text = (f"🔴 <b>New Withdrawal Request</b> #{wd_id}\n\n"
            f"👤 {name} ({uname}) <code>{uid}</code>\n"
            f"💰 {amount} ZYR\n"
            f"📫 <code>{html.escape(address)}</code>")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}"),
    ]])
    try:
        await telegram_app.bot.send_message(
            chat_id=ADMIN_ID, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error("admin notify failed for wd %s: %s", wd_id, e)
    return {"ok": True}


# ------------------------------- ربات تلگرام -------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = html.escape(update.effective_user.first_name or "Miner")
    ref = None
    if context.args:
        ref = clean_ref(context.args[0])
    url = WEBAPP_URL + (f"?ref=ref_{ref}" if ref else "")
    text = (
        f"⚡ <b>Welcome to ZYR Cloud Miner, {name}!</b>\n\n"
        f"⛏️ <b>Mine ZYR inside the app:</b> claim tokens and withdraw to TON.\n"
        f"💎 <b>Activate your miner:</b> deposit {PRICE_TON} TON to start earning 80 ZYR daily.\n"
        f"👥 <b>Invite friends:</b> raise your rate up to 150 ZYR/day and earn "
        f"{REFERRAL_REWARD} ZYR for every friend who activates.\n\n"
        f"👇 <b>Tap below to start!</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url))]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    try:
        action, wd_id = q.data.split("_", 1)
        wd_id = int(wd_id)
    except Exception:
        return
    if action not in ("approve", "reject"):
        return

    with db() as conn:
        with tx(conn):
            wd = conn.execute(
                "SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wd_id,)).fetchone()
            if not wd:
                result = None
            elif action == "approve":
                conn.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
                result = f"✅ Approved #{wd_id}: {wd['amount']} ZYR for user {wd['user_id']}.\nSend it manually to:\n{wd['address']}"
            else:
                conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
                conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (wd["amount"], wd["user_id"]))
                result = f"❌ Rejected #{wd_id}: {wd['amount']} ZYR returned to user {wd['user_id']}."
    await q.edit_message_text(result or "Already processed.")


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


@app.post("/api/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(403, "Forbidden")
    update = Update.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
