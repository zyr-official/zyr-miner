import os
import re
import json
import math
import time
import html
import hmac
import hashlib
import sqlite3
import urllib.request
import urllib.parse
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, CommandHandler

# ====== تنظیمات (همه از env؛ توکن دیگر داخل کد نیست) ======
BOT_TOKEN = os.environ["9204005359:AAGJ-532L12GA8T_w01ORxpH1HhLDl28Bqc"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "1839907139"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://zyr-official.github.io/zyr-miner/app.html")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ZYRMining")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # باید با secret_token در setWebhook یکی باشد
TON_RECEIVER = os.getenv("TON_RECEIVER", "UQBke3M55AgQ7qRoAHf1Li9t_c-ZEjnugAmMZ5cTRCg1oo24")
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://zyr-official.github.io")
ZYR_TON_RATE = float(os.getenv("ZYR_TON_RATE", "0.0001"))
ALLOW_INSECURE_TEST = os.getenv("ALLOW_INSECURE_TEST", "0") == "1"  # فقط برای تست محلی

PRICE_NANO = 100_000_000          # 0.1 TON
REF_REWARD = 300
MIN_WITHDRAW = 10000
MAX_PENDING_SECONDS = 30 * 86400  # سقف انباشت استخراج
AUTH_MAX_AGE = 86400              # حداکثر عمر init_data

_default_db = "/data/zyr.db" if os.path.isdir("/data") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "zyr.db")
DB_PATH = os.getenv("DB_PATH", _default_db)  # روی دیسک پایدار بگذارید
# =====================

TON_ADDR_RE = re.compile(r"^(EQ|UQ)[A-Za-z0-9_-]{46}$")


# ---------------- Database ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def tx():
    """تراکنش اتمیک (BEGIN IMMEDIATE) با بستن مطمئن اتصال."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
    conn = get_db()
    try:
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
        c.execute('''CREATE TABLE IF NOT EXISTS payments (
            tx_hash TEXT PRIMARY KEY,
            user_id TEXT,
            created REAL
        )''')
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "rate" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN rate REAL DEFAULT 80")
        if "stash" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN stash REAL DEFAULT 0")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ref_friend ON referrals(friend_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id)")
    finally:
        conn.close()


init_db()


# ---------------- Models ----------------
class UserSyncRequest(BaseModel):
    user_id: str | None = None
    first_name: str | None = None
    referrer_id: str | None = None
    init_data: str | None = None


class ClaimMiningRequest(BaseModel):
    user_id: str | None = None
    init_data: str | None = None


class WithdrawRequest(BaseModel):
    user_id: str | None = None
    username: str | None = None
    amount: float
    address: str
    init_data: str | None = None


class MemberCheckRequest(BaseModel):
    user_id: str | None = None
    init_data: str | None = None


class ConfirmPaymentRequest(BaseModel):
    user_id: str | None = None
    tx_hash: str | None = None
    init_data: str | None = None


class ClaimRefRequest(BaseModel):
    user_id: str | None = None
    friend_id: str
    init_data: str | None = None


# ---------------- Auth ----------------
def verify_init_data(init_data: str) -> dict | None:
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
        auth_date = int(parsed.get("auth_date", "0"))
        if time.time() - auth_date > AUTH_MAX_AGE:
            return None
        user = json.loads(parsed.get("user", "{}"))
        if not user.get("id"):
            return None
        return user
    except Exception as e:
        print(f"[verify_init_data] {e}")
        return None


def resolve_user(req) -> tuple[str, dict | None]:
    """فقط init_data معتبر پذیرفته می‌شود (مگر در حالت تست محلی)."""
    init_data = getattr(req, "init_data", None)
    if init_data:
        user = verify_init_data(init_data)
        if user:
            return str(user["id"]), user
    if ALLOW_INSECURE_TEST and getattr(req, "user_id", None):
        return str(req.user_id), None
    raise HTTPException(status_code=401, detail="Invalid or expired Telegram session. Reopen the app.")


# ---------------- Mining logic ----------------
def level_for(active_invited: int) -> tuple[int, int]:
    if active_invited >= 10:
        return 150, 4
    if active_invited >= 6:
        return 130, 3
    if active_invited >= 3:
        return 110, 2
    return 80, 1


def settle(conn, uid: str, now: float) -> dict:
    """نرخ فعلی را حساب می‌کند؛ اگر لول عوض شده، سود قبلی با نرخ قبلی ذخیره می‌شود (بدون محاسبه عقب‌گرد)."""
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    active = conn.execute(
        "SELECT COUNT(*) FROM referrals r JOIN users f ON f.id=r.friend_id "
        "WHERE r.referrer_id=? AND f.has_paid=1", (uid,)
    ).fetchone()[0]
    rate, level = level_for(active)
    pending = 0.0
    if u["has_paid"]:
        last = u["last_mining"] if (u["last_mining"] and u["last_mining"] > 0) else now
        elapsed = min(max(0.0, now - last), MAX_PENDING_SECONDS)
        old_rate = u["rate"] or 80
        stash = u["stash"] or 0.0
        if old_rate != rate:
            stash += elapsed * old_rate / 86400
            conn.execute("UPDATE users SET stash=?, rate=?, last_mining=? WHERE id=?", (stash, rate, now, uid))
            elapsed = 0.0
        pending = stash + elapsed * rate / 86400
    return {"user": u, "rate": rate, "level": level, "pending": pending, "active": active}


# ---------------- TON payment verification ----------------
def find_payment(uid: str) -> str | None:
    """تراکنش ورودی به کیف پول مقصد با کامنت zyr_<uid> و مبلغ >= 0.1 TON را پیدا می‌کند."""
    url = ("https://toncenter.com/api/v2/getTransactions?address="
           + urllib.parse.quote(TON_RECEIVER) + "&limit=50")
    headers = {"User-Agent": "zyr-miner"}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"[toncenter] {e}")
        return None
    expected = f"zyr_{uid}"
    conn = get_db()
    try:
        for t in data.get("result", []) or []:
            m = t.get("in_msg") or {}
            try:
                value = int(m.get("value", "0"))
            except Exception:
                continue
            if str(m.get("message", "")) == expected and value >= PRICE_NANO:
                h = (t.get("transaction_id") or {}).get("hash")
                if h and not conn.execute("SELECT 1 FROM payments WHERE tx_hash=?", (h,)).fetchone():
                    return h
    finally:
        conn.close()
    return None


# ---------------- Telegram app ----------------
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
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/check-member")
async def check_member(req: MemberCheckRequest):
    uid, _ = resolve_user(req)
    try:
        member = await telegram_app.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=int(uid))
        ok = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
        return {"isMember": ok}
    except Exception as e:
        print(f"[check-member] {e} (bot must be admin of the channel)")
        return {"isMember": False, "error": "Could not verify membership"}


@app.post("/sync-user")
def sync_user(req: UserSyncRequest):
    uid, tu = resolve_user(req)
    name = ((tu or {}).get("first_name") or req.first_name or "ZYR User")[:64]
    now = time.time()

    with tx() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            referrer = None
            rid = (req.referrer_id or "").strip()
            if rid.isdigit() and rid != uid:
                r = conn.execute("SELECT id FROM users WHERE id=?", (rid,)).fetchone()
                if r:
                    referrer = r["id"]
            conn.execute(
                "INSERT INTO users (id, first_name, referrer_id, last_mining, rate, stash) VALUES (?, ?, ?, ?, 80, 0)",
                (uid, name, referrer, now),
            )
            if referrer:
                conn.execute("INSERT OR IGNORE INTO referrals (referrer_id, friend_id, reward_claimed) VALUES (?, ?, 0)",
                             (referrer, uid))
        elif user["first_name"] != name:
            conn.execute("UPDATE users SET first_name=? WHERE id=?", (name, uid))

        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if uid == str(ADMIN_ID) and not user["has_paid"]:
            conn.execute("UPDATE users SET has_paid=1, last_mining=?, rate=80, stash=0 WHERE id=?", (now, uid))

        s = settle(conn, uid, now)
        user = s["user"]
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

        rows = conn.execute(
            "SELECT u.id, u.first_name, u.has_paid, r.reward_claimed FROM referrals r "
            "JOIN users u ON r.friend_id = u.id WHERE r.referrer_id=?", (uid,)
        ).fetchall()
        friends = [{
            "id": r["id"], "name": r["first_name"],
            "has_paid": bool(r["has_paid"]), "reward_claimed": bool(r["reward_claimed"]),
        } for r in rows]

    return {
        "balance": round(user["balance"], 5),
        "pending_mined": round(s["pending"], 5),
        "has_paid": bool(user["has_paid"]),
        "invited": len(friends),
        "active_invited": s["active"],
        "referral_earned": round(user["referral_earned"], 2),
        "rate_per_day": s["rate"],
        "level": s["level"],
        "friends": friends,
        "ton_rate": ZYR_TON_RATE,
    }


@app.post("/claim-mining")
def claim_mining(req: ClaimMiningRequest):
    uid, _ = resolve_user(req)
    now = time.time()
    with tx() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not user["has_paid"]:
            raise HTTPException(status_code=400, detail="Miner not activated")
        s = settle(conn, uid, now)
        balance = conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        new_balance = balance + s["pending"]
        conn.execute("UPDATE users SET balance=?, stash=0, last_mining=? WHERE id=?", (new_balance, now, uid))
    return {"ok": True, "new_balance": round(new_balance, 5)}


@app.post("/confirm-payment")
def confirm_payment(req: ConfirmPaymentRequest):
    uid, _ = resolve_user(req)
    with tx() as conn:
        user = conn.execute("SELECT has_paid FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["has_paid"]:
            return {"ok": True, "message": "Already activated"}

    tx_hash = find_payment(uid)  # تماس شبکه بیرون از قفل دیتابیس
    if not tx_hash:
        return {"ok": False, "pending": True, "message": "Payment not found yet"}

    now = time.time()
    with tx() as conn:
        try:
            conn.execute("INSERT INTO payments (tx_hash, user_id, created) VALUES (?, ?, ?)", (tx_hash, uid, now))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Transaction already used")
        conn.execute("UPDATE users SET has_paid=1, last_mining=?, rate=80, stash=0 WHERE id=?", (now, uid))
    return {"ok": True, "message": "Payment confirmed"}


@app.post("/claim-ref-reward")
def claim_ref_reward(req: ClaimRefRequest):
    uid, _ = resolve_user(req)
    with tx() as conn:
        ref = conn.execute(
            "SELECT r.id FROM referrals r JOIN users f ON f.id=r.friend_id "
            "WHERE r.referrer_id=? AND r.friend_id=? AND r.reward_claimed=0 AND f.has_paid=1",
            (uid, req.friend_id),
        ).fetchone()
        if not ref:
            return {"ok": False, "message": "Reward not available"}
        cur = conn.execute("UPDATE referrals SET reward_claimed=1 WHERE id=? AND reward_claimed=0", (ref["id"],))
        if cur.rowcount != 1:
            return {"ok": False, "message": "Reward not available"}
        conn.execute(
            "UPDATE users SET balance = balance + ?, referral_earned = referral_earned + ? WHERE id=?",
            (REF_REWARD, REF_REWARD, uid),
        )
    return {"ok": True, "message": "300 ZYR claimed!"}


def _create_withdrawal(uid: str, amount: float, address: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id=? AND has_paid=1 AND balance >= ?",
            (amount, uid, amount),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        cur = conn.execute(
            "INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?, ?, ?, 'pending')",
            (uid, amount, address),
        )
        return cur.lastrowid


@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawRequest):
    uid, tu = resolve_user(req)
    amount = req.amount
    address = req.address.strip()

    if not math.isfinite(amount) or amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid amount")
    amount = round(amount, 5)
    if amount < MIN_WITHDRAW:
        raise HTTPException(status_code=400, detail=f"Minimum is {MIN_WITHDRAW:,} ZYR")
    if not TON_ADDR_RE.match(address):
        raise HTTPException(status_code=400, detail="Invalid TON address")

    wd_id = await run_in_threadpool(_create_withdrawal, uid, amount, address)

    username = (tu or {}).get("first_name") or req.username or "User"
    text = (f"🔴 <b>New Withdrawal Request</b>\n\n"
            f"👤 User: {html.escape(username[:64])} (<code>{uid}</code>)\n"
            f"💰 Amount: {amount} ZYR\n"
            f"📫 Address: <code>{html.escape(address)}</code>")
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}"),
    ]]
    try:
        await telegram_app.bot.send_message(
            chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception as e:
        print(f"[WITHDRAW] admin notify error (withdrawal #{wd_id} is saved as pending): {e}")

    return {"ok": True}


@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse({
        "url": WEBAPP_URL,
        "name": "ZYR Miner",
        "iconUrl": "https://zyr-official.github.io/zyr-miner/icon.png",
    })


# ---------------- Bot handlers ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user_name = html.escape(update.effective_user.first_name or "Miner")
    ref_id = None
    if context.args and context.args[0].startswith("ref_"):
        candidate = context.args[0].replace("ref_", "")
        if candidate.isdigit():
            ref_id = candidate

    url = WEBAPP_URL
    if ref_id:
        url += f"?ref={ref_id}"  # start_param روی دکمه web_app کار نمی‌کند؛ از query استفاده می‌شود

    welcome_text = (
        f"⚡ <b>Welcome to ZYR Cloud Miner, {user_name}!</b>\n\n"
        f"⛏️ <b>Mine ZYR inside the app:</b> Start your session, claim tokens, and withdraw to TON.\n"
        f"💎 <b>Activate your miner:</b> Deposit 0.1 TON to start earning 80 ZYR daily.\n"
        f"👥 <b>Invite friends:</b> Boost your mining rate and earn 300 ZYR when they pay 0.1 TON.\n\n"
        f"👇 <b>Tap below to start!</b>"
    )
    keyboard = [[InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url))]]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


def _process_withdrawal(action: str, wd_id: int):
    with tx() as conn:
        row = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not row or row["status"] != "pending":
            return None
        if action == "approve":
            conn.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
        else:
            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (row["amount"], row["user_id"]))
            conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
        return dict(row)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not query.from_user or query.from_user.id != ADMIN_ID:
        await query.answer("Not allowed", show_alert=True)
        return
    await query.answer()
    try:
        parts = (query.data or "").split("_")
        action, wd_id = parts[0], int(parts[1])  # سازگار با فرمت قدیمی approve_id_uid_amount
        if action not in ("approve", "reject"):
            raise ValueError
    except Exception:
        await query.edit_message_text("❌ Invalid callback data.")
        return

    row = await run_in_threadpool(_process_withdrawal, action, wd_id)
    if row is None:
        await query.edit_message_text("⚠️ This request was already processed or not found.")
        return
    if action == "approve":
        await query.edit_message_text(text=f"✅ Approved {row['amount']} ZYR for user {row['user_id']}.")
    else:
        await query.edit_message_text(text=f"❌ Rejected {row['amount']} ZYR. Funds returned.")


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET or not hmac.compare_digest(token, WEBHOOK_SECRET):
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
