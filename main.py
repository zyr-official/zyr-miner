import hashlib
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

log = logging.getLogger("zyr")

# ====== Settings ======
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # REQUIRED: set as environment variable only
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

ADMIN_ID = 1839907139
WEBAPP_URL = "https://zyr-official.github.io/zyr-miner/"
WEBAPP_ORIGIN = "https://zyr-official.github.io"
BACKEND_URL = os.getenv("BACKEND_URL", "https://nann.pingooshop.ir")
CHANNEL_ID = "@ZYRMining"
TON_RECEIVER = "UQBke3M55AgQ7qRoAHf1Li9t_c-ZEjnugAmMZ5cTRCg1oo24"
PRICE_NANO = 100_000_000  # 0.1 TON
TONAPI_KEY = os.getenv("TONAPI_KEY", "")  # optional but recommended (higher rate limit)
DB_PATH = os.getenv("DB_PATH", "zyr.db")  # point this to a persistent volume, e.g. /data/zyr.db
MIN_WITHDRAW = 10_000
REFERRAL_BONUS = 300
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
# =======================

RATES = [(20, 150, 4), (10, 130, 3), (5, 110, 2), (0, 80, 1)]  # (min active friends, ZYR/day, level)
TON_ADDR_RE = re.compile(r"^[EU]Q[A-Za-z0-9_-]{46}$")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ---------- Database ----------
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def tx():
    """One atomic write transaction (BEGIN IMMEDIATE serialises writers)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_db():
    conn = connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            first_name TEXT,
            balance REAL DEFAULT 0,
            referrer_id TEXT,
            referral_earned REAL DEFAULT 0,
            has_paid INTEGER DEFAULT 0,
            last_mining REAL DEFAULT 0,
            rate REAL DEFAULT 80
        )"""
    )
    try:  # migration for databases created by the old version
        conn.execute("ALTER TABLE users ADD COLUMN rate REAL DEFAULT 80")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, amount REAL, address TEXT, status TEXT,
            created_at REAL DEFAULT 0
        )"""
    )
    try:
        conn.execute("ALTER TABLE withdrawals ADD COLUMN created_at REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS used_payments (tx_hash TEXT PRIMARY KEY, user_id TEXT)")
    conn.close()


init_db()


# ---------- Telegram initData auth ----------
def verify_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "Open the app from Telegram")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise HTTPException(401, "Invalid session")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401, "Invalid session")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            raise HTTPException(401, "Session expired, reopen the app")
        user = json.loads(pairs["user"])
        return {
            "id": str(int(user["id"])),
            "first_name": user.get("first_name", "User"),
            "start_param": pairs.get("start_param", ""),
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid session")


# ---------- Models ----------
class Auth(BaseModel):
    init_data: str


class WithdrawReq(Auth):
    amount: float = Field(gt=0, le=1e9)
    address: str


# ---------- Mining logic ----------
def rate_for(active: int):
    for need, rate, level in RATES:
        if active >= need:
            return rate, level
    return 80, 1


def get_or_create_user(c, u):
    row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    if row:
        return row
    ref = None
    sp = u["start_param"]
    if sp.startswith("ref_"):
        cand = sp[4:]
        if cand.isdigit() and cand != u["id"]:
            if c.execute("SELECT 1 FROM users WHERE id=?", (cand,)).fetchone():
                ref = cand
    c.execute(
        "INSERT INTO users (id, first_name, referrer_id, last_mining, rate) VALUES (?,?,?,?,80)",
        (u["id"], u["first_name"], ref, time.time()),
    )
    return c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()


def settle(c, uid):
    """Credit mined tokens since last settle at the PREVIOUS rate, then recompute the rate."""
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    now = time.time()
    balance = u["balance"]
    if u["has_paid"] and u["last_mining"] > 0:
        balance += max(0.0, now - u["last_mining"]) * (u["rate"] or 80) / 86400
    active = c.execute(
        "SELECT COUNT(*) FROM users WHERE referrer_id=? AND has_paid=1", (uid,)
    ).fetchone()[0]
    rate, _ = rate_for(active)
    c.execute("UPDATE users SET balance=?, last_mining=?, rate=? WHERE id=?", (balance, now, rate, uid))


# ---------- API ----------
@app.post("/check-member")
async def check_member(req: Auth):
    u = verify_init_data(req.init_data)
    try:
        member = await telegram_app.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=int(u["id"]))
        return {"isMember": member.status in ("member", "administrator", "creator")}
    except Exception as e:
        log.warning("check-member failed: %s", e)
        return {"isMember": False}


@app.post("/sync-user")
def sync_user(req: Auth):
    u = verify_init_data(req.init_data)
    with tx() as c:
        user = get_or_create_user(c, u)
        if u["id"] == str(ADMIN_ID) and not user["has_paid"]:
            c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (time.time(), u["id"]))
        settle(c, u["id"])
        user = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        invited = c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (u["id"],)).fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM users WHERE referrer_id=? AND has_paid=1", (u["id"],)
        ).fetchone()[0]
        rate, level = rate_for(active)
        return {
            "balance": round(user["balance"], 5),
            "has_paid": bool(user["has_paid"]),
            "invited": invited,
            "active": active,
            "referral_earned": round(user["referral_earned"], 2),
            "rate_per_day": rate,
            "level": level,
        }


async def find_payment(uid: str):
    """Look for an incoming tx of >= 0.1 TON to TON_RECEIVER carrying comment 'ZYR-<uid>'."""
    url = f"https://tonapi.io/v2/blockchain/accounts/{TON_RECEIVER}/transactions"
    headers = {"Authorization": f"Bearer {TONAPI_KEY}"} if TONAPI_KEY else {}
    async with httpx.AsyncClient(timeout=15) as cl:
        r = await cl.get(url, params={"limit": 100}, headers=headers)
    r.raise_for_status()
    wanted = f"ZYR-{uid}"
    for t in r.json().get("transactions", []):
        if not t.get("success", True):
            continue
        m = t.get("in_msg") or {}
        if int(m.get("value") or 0) < PRICE_NANO:
            continue
        body = m.get("decoded_body") or {}
        if body.get("text") == wanted:
            return t["hash"]
    return None


@app.post("/confirm-payment")
async def confirm_payment(req: Auth):
    u = verify_init_data(req.init_data)
    with tx() as c:
        row = c.execute("SELECT has_paid FROM users WHERE id=?", (u["id"],)).fetchone()
    if not row:
        raise HTTPException(400, "User not found")
    if row["has_paid"]:
        return {"ok": True, "message": "Already active"}

    try:
        tx_hash = await find_payment(u["id"])
    except Exception as e:
        log.warning("payment lookup failed: %s", e)
        return {"ok": False, "message": "Could not reach TON API, try again"}
    if not tx_hash:
        return {"ok": False, "message": "Payment not found yet"}

    with tx() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if user["has_paid"]:
            return {"ok": True, "message": "Already active"}
        if c.execute("SELECT 1 FROM used_payments WHERE tx_hash=?", (tx_hash,)).fetchone():
            return {"ok": False, "message": "Transaction already used"}
        c.execute("INSERT INTO used_payments (tx_hash, user_id) VALUES (?,?)", (tx_hash, u["id"]))
        c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (time.time(), u["id"]))
        ref = user["referrer_id"]
        if ref and c.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
            settle(c, ref)  # credit referrer's past time at the old rate first
            c.execute(
                "UPDATE users SET balance=balance+?, referral_earned=referral_earned+? WHERE id=?",
                (REFERRAL_BONUS, REFERRAL_BONUS, ref),
            )
    return {"ok": True, "message": "Payment confirmed"}


@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawReq):
    u = verify_init_data(req.init_data)
    address = req.address.strip()
    if not TON_ADDR_RE.match(address):
        raise HTTPException(400, "Invalid TON address")
    if req.amount < MIN_WITHDRAW:
        raise HTTPException(400, f"Minimum is {MIN_WITHDRAW:,} ZYR")

    with tx() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if not user or not user["has_paid"]:
            raise HTTPException(403, "Activate your miner first")
        if c.execute(
            "SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending'", (u["id"],)
        ).fetchone():
            raise HTTPException(400, "You already have a pending withdrawal")
        settle(c, u["id"])
        cur = c.execute(
            "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
            (req.amount, u["id"], req.amount),
        )
        if cur.rowcount != 1:
            raise HTTPException(400, "Insufficient balance")
        cur = c.execute(
            "INSERT INTO withdrawals (user_id, amount, address, status, created_at) VALUES (?,?,?,'pending',?)",
            (u["id"], req.amount, address, time.time()),
        )
        wd_id = cur.lastrowid

    text = (
        "🔴 <b>New Withdrawal Request</b>\n\n"
        f"👤 User: {html.escape(u['first_name'])} (<code>{u['id']}</code>)\n"
        f"💰 Amount: {req.amount:g} ZYR\n"
        f"📫 Address: <code>{html.escape(address)}</code>"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}"),
    ]]
    try:
        await telegram_app.bot.send_message(
            chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception as e:
        log.error("could not notify admin of withdrawal %s: %s", wd_id, e)
    return {"ok": True}


@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse({
        "url": WEBAPP_URL,
        "name": "ZYR Miner",
        "iconUrl": "https://zyr-official.github.io/zyr-miner/icon.png",
    })


# ---------- Telegram bot ----------
telegram_app = Application.builder().token(BOT_TOKEN).build()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = html.escape(update.effective_user.first_name or "Miner")
    text = (
        f"⚡ <b>Welcome to ZYR Cloud Miner, {name}!</b>\n\n"
        "⛏️ <b>Mine ZYR inside the app:</b> start your session and withdraw to TON.\n"
        "💎 <b>Activate your miner:</b> deposit 0.1 TON to start earning 80 ZYR daily.\n"
        "👥 <b>Invite friends:</b> boost your rate (up to 150 ZYR/day) and earn 300 ZYR per activated friend.\n\n"
        "👇 <b>Tap below to start!</b>"
    )
    kb = [[InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    try:
        action, wd_raw = q.data.split("_")
        wd_id = int(wd_raw)
    except Exception:
        return
    if action not in ("approve", "reject"):
        return

    with tx() as c:
        w = c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wd_id,)).fetchone()
        if w:
            if action == "approve":
                c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
            else:
                c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
                c.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))

    if not w:
        await q.edit_message_text("⚠️ This request was already processed.")
    elif action == "approve":
        await q.edit_message_text(
            f"✅ Approved {w['amount']:g} ZYR for user {w['user_id']}.\nSend the TON to:\n{w['address']}"
        )
    else:
        await q.edit_message_text(f"❌ Rejected {w['amount']:g} ZYR. Funds returned.")


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=f"{BACKEND_URL}/api/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
    )


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/")
def read_root():
    return {"status": "Backend is running"}
