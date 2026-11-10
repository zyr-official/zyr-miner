import os
import re
import json
import hmac
import time
import html
import hashlib
import sqlite3
import logging
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zyr")

# ====== تنظیمات (همه از Environment) ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "9204005359:AAFFb-wJeEsKmNrOJSczuGGLx-CBv1TS2Ko")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1839907139"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://zyr-official.github.io/zyr-miner/")
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://zyr-official.github.io")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ZYRMining")
TON_RECEIVER = os.getenv("TON_RECEIVER", "UQBke3M55AgQ7qRoAHf1Li9t_c-ZEjnugAmMZ5cTRCg1oo24")
PAY_NANOTON = 100_000_000                                  # 0.1 TON
DB_PATH = os.getenv("DB_PATH", "zyr.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://nann.pingooshop.ir/api/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "zyrWebhookSecret2026")
TONCENTER_KEY = os.getenv("TONCENTER_API_KEY", "")
MIN_WITHDRAW = 10_000
REF_BONUS = 300
ADDR_RE = re.compile(r"^(?:[EU]Q[A-Za-z0-9_-]{46}|0:[0-9a-fA-F]{64})$")
# ==========================================


# ---------- دیتابیس ----------
def get_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            first_name TEXT,
            balance REAL DEFAULT 0,
            accrued REAL DEFAULT 0,
            referrer_id TEXT,
            referral_earned REAL DEFAULT 0,
            has_paid INTEGER DEFAULT 0,
            last_mining REAL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, amount REAL, address TEXT, status TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS payments (
            tx_hash TEXT PRIMARY KEY, user_id TEXT, created REAL)""")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "accrued" not in cols:  # مهاجرت دیتابیس قدیمی
            conn.execute("ALTER TABLE users ADD COLUMN accrued REAL DEFAULT 0")
    conn.close()


init_db()


# ---------- احراز هویت تلگرام ----------
def verify_init_data(init_data: str, max_age: int = 86400):
    if not init_data:
        raise HTTPException(401, "Missing init data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise HTTPException(401, "Bad init data")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise HTTPException(401, "Invalid signature")
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            raise HTTPException(401, "Session expired, reopen the app")
        user = json.loads(pairs["user"])
    except (KeyError, ValueError):
        raise HTTPException(401, "Bad init data")
    return user, pairs.get("start_param", "")


# ---------- منطق ماینینگ ----------
def rate_for(active_friends: int):
    if active_friends >= 20:
        return 150, 4
    if active_friends >= 10:
        return 130, 3
    if active_friends >= 5:
        return 110, 2
    return 80, 1


def counts(c, uid):
    total = c.execute("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (uid,)).fetchone()["n"]
    active = c.execute("SELECT COUNT(*) n FROM users WHERE referrer_id=? AND has_paid=1", (uid,)).fetchone()["n"]
    return total, active


def settle(c, uid):
    """مقدار استخراج‌شده تا الان را به accrued اضافه می‌کند (با سرعت فعلی)."""
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    now = time.time()
    if u["has_paid"]:
        _, active = counts(c, uid)
        rate, _ = rate_for(active)
        add = max(0.0, now - u["last_mining"]) * rate / 86400
        c.execute("UPDATE users SET accrued=accrued+?, last_mining=? WHERE id=?", (add, now, uid))
    else:
        c.execute("UPDATE users SET last_mining=? WHERE id=?", (now, uid))


def state(c, uid):
    settle(c, uid)
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    total, active = counts(c, uid)
    rate, level = rate_for(active)
    return {
        "balance": round(u["balance"], 5),
        "pending": round(u["accrued"], 5),
        "has_paid": bool(u["has_paid"]),
        "invited": total,
        "active": active,
        "referral_earned": round(u["referral_earned"], 2),
        "rate_per_day": rate,
        "level": level,
    }


# ---------- مدل‌ها ----------
class Auth(BaseModel):
    init_data: str


class SyncReq(Auth):
    ref: str | None = None


class WithdrawReq(Auth):
    amount: float
    address: str


# ---------- برنامه ----------
telegram_app = Application.builder().token(BOT_TOKEN).build()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()
    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(
            WEBHOOK_URL, secret_token=WEBHOOK_SECRET or None, allowed_updates=["message", "callback_query"]
        )
    yield
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/")
def root():
    return {"status": "Backend is running"}


@app.get("/tonconnect-manifest.json")
async def manifest():
    return JSONResponse({
        "url": WEBAPP_URL.rstrip("/"),
        "name": "ZYR Miner",
        "iconUrl": WEBAPP_URL.rstrip("/") + "/icon.png",
    })


@app.post("/check-member")
async def check_member(req: Auth):
    user, _ = verify_init_data(req.init_data)
    if int(user["id"]) == ADMIN_ID:
        return {"isMember": True}
    try:
        m = await telegram_app.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=int(user["id"]))
        return {"isMember": m.status in ("member", "administrator", "creator")}
    except Exception as e:
        log.warning("check_member: %s", e)
        return {"isMember": False, "error": str(e)}


@app.post("/sync-user")
async def sync_user(req: SyncReq):
    user, start_param = verify_init_data(req.init_data)
    uid = str(user["id"])
    name = (user.get("first_name") or "User")[:64]

    conn = get_db()
    try:
        with conn:
            row = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                ref = start_param[4:] if start_param.startswith("ref_") else (req.ref or "")
                if not ref.isdigit() or ref == uid or not conn.execute(
                        "SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
                    ref = None
                conn.execute("INSERT INTO users (id, first_name, referrer_id, last_mining) VALUES (?,?,?,?)",
                             (uid, name, ref, time.time()))
            if int(uid) == ADMIN_ID:
                conn.execute("UPDATE users SET has_paid=1 WHERE id=? AND has_paid=0", (uid,))
            return state(conn, uid)
    finally:
        conn.close()


@app.post("/claim")
async def claim(req: Auth):
    user, _ = verify_init_data(req.init_data)
    uid = str(user["id"])
    conn = get_db()
    try:
        with conn:
            u = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
            if not u or not u["has_paid"]:
                raise HTTPException(403, "Miner not activated")
            settle(conn, uid)
            conn.execute("UPDATE users SET balance=balance+accrued, accrued=0 WHERE id=?", (uid,))
            return state(conn, uid)
    finally:
        conn.close()


# ---------- تأیید پرداخت روی بلاکچین ----------
async def find_payment(uid: str):
    comment = f"ZYR:{uid}"
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    params = {
        "account": TON_RECEIVER,
        "limit": 100,
        "sort": "desc",
        "start_utime": int(time.time()) - 3 * 86400,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://toncenter.com/api/v3/transactions", params=params, headers=headers)
        r.raise_for_status()
        for tx in r.json().get("transactions", []):
            msg = tx.get("in_msg") or {}
            if (tx.get("description") or {}).get("aborted"):
                continue
            dec = (msg.get("message_content") or {}).get("decoded") or {}
            if dec.get("comment") != comment:
                continue
            if int(msg.get("value") or 0) < PAY_NANOTON:
                continue
            return tx.get("hash")
    return None


@app.post("/confirm-payment")
async def confirm_payment(req: Auth):
    user, _ = verify_init_data(req.init_data)
    uid = str(user["id"])

    conn = get_db()
    try:
        u = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        if u["has_paid"]:
            return {"ok": True}
    finally:
        conn.close()

    try:
        tx_hash = await find_payment(uid)
    except Exception as e:
        log.warning("toncenter error: %s", e)
        return {"ok": False, "pending": True}
    if not tx_hash:
        return {"ok": False, "pending": True}

    conn = get_db()
    try:
        with conn:
            try:
                conn.execute("INSERT INTO payments (tx_hash, user_id, created) VALUES (?,?,?)",
                             (tx_hash, uid, time.time()))
            except sqlite3.IntegrityError:
                return {"ok": False, "message": "Transaction already used"}
            cur = conn.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=? AND has_paid=0",
                               (time.time(), uid))
            if cur.rowcount:
                ref = conn.execute("SELECT referrer_id FROM users WHERE id=?", (uid,)).fetchone()["referrer_id"]
                if ref and conn.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
                    settle(conn, ref)  # قبل از تغییر سرعت، سهم قبلی ثبت شود
                    conn.execute("UPDATE users SET balance=balance+?, referral_earned=referral_earned+? WHERE id=?",
                                 (REF_BONUS, REF_BONUS, ref))
        return {"ok": True}
    finally:
        conn.close()


# ---------- برداشت ----------
@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawReq):
    user, _ = verify_init_data(req.init_data)
    uid = str(user["id"])
    amount = req.amount
    address = req.address.strip()

    if not (amount == amount and amount != float("inf")) or amount < MIN_WITHDRAW:
        raise HTTPException(400, f"Minimum is {MIN_WITHDRAW} ZYR")
    if not ADDR_RE.match(address):
        raise HTTPException(400, "Invalid TON address")

    conn = get_db()
    try:
        with conn:
            u = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
            if not u or not u["has_paid"]:
                raise HTTPException(403, "Miner not activated")
            cur = conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                               (amount, uid, amount))
            if cur.rowcount == 0:
                raise HTTPException(400, "Insufficient balance")
            wd = conn.execute("INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?,?,?,'pending')",
                              (uid, amount, address))
            wd_id = wd.lastrowid
            new_balance = conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    finally:
        conn.close()

    text = (f"🔴 <b>New Withdrawal Request</b>\n\n"
            f"👤 User: {html.escape(user.get('first_name', ''))} (<code>{uid}</code>)\n"
            f"💰 Amount: {amount} ZYR\n"
            f"📫 Address: <code>{html.escape(address)}</code>")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}"),
    ]])
    try:
        await telegram_app.bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        log.error("admin notify failed: %s", e)
    return {"ok": True, "balance": round(new_balance, 5)}


# ---------- ربات ----------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = html.escape(update.effective_user.first_name or "Miner")
    url = WEBAPP_URL
    if context.args and context.args[0].startswith("ref_"):
        ref = context.args[0][4:]
        if ref.isdigit():
            url += f"?ref={ref}"
    text = (
        f"⚡ <b>Welcome to ZYR Cloud Miner, {name}!</b>\n\n"
        f"⛏️ <b>Mine ZYR inside the app:</b> Start your session, claim tokens, and withdraw to TON.\n"
        f"💎 <b>Activate your miner:</b> Deposit 0.1 TON to start earning 80 ZYR daily.\n"
        f"👥 <b>Invite friends:</b> Boost your mining rate (up to 150 ZYR/day) and earn {REF_BONUS} ZYR per friend.\n\n"
        f"👇 <b>Tap below to start!</b>"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url))]])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not allowed", show_alert=True)
        return
    await q.answer()
    try:
        action, wd_id = q.data.split("_")
        wd_id = int(wd_id)
    except ValueError:
        return
    if action not in ("approve", "reject"):
        return

    conn = get_db()
    try:
        with conn:
            w = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
            if not w:
                return
            new_status = "approved" if action == "approve" else "rejected"
            cur = conn.execute("UPDATE withdrawals SET status=? WHERE id=? AND status='pending'", (new_status, wd_id))
            if cur.rowcount == 0:
                await q.edit_message_text("⚠️ This request was already processed.")
                return
            if action == "reject":
                conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))
    finally:
        conn.close()

    if action == "approve":
        await q.edit_message_text(f"✅ Approved {w['amount']} ZYR for user {w['user_id']}.\n"
                                  f"Address: {w['address']}")
        msg = f"✅ Your withdrawal of {w['amount']} ZYR was approved."
    else:
        await q.edit_message_text(f"❌ Rejected {w['amount']} ZYR. Funds returned.")
        msg = f"❌ Your withdrawal of {w['amount']} ZYR was rejected. The amount was returned to your balance."
    try:
        await context.bot.send_message(int(w["user_id"]), msg)
    except Exception:
        pass


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    update = Update.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
