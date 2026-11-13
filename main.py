import os
import json
import time
import sqlite3
import hashlib
import hmac
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update, WebAppInfo
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, CommandHandler

# ====== تنظیمات (همه از متغیر محیطی) ======
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 1839907139
WEBAPP_URL = "https://zyr-official.github.io/zyr-miner/app.html"
CHANNEL_ID = "@ZYRMining"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DB_PATH = os.getenv("DB_PATH", "/data/zyr.db")
TON_RECEIVER = "UQBke3M55AgQ7qRoAHf1Li9t_c-ZEjnugAmMZ5cTRCg1oo24"
PAY_BASE_NANO = 100_000_000          # 0.1 TON
TONCENTER_KEY = os.getenv("TONCENTER_KEY", "")
ALLOW_INSECURE = os.getenv("ALLOW_INSECURE", "0") == "1"   # فقط برای تست
MIN_WITHDRAW = 10000
REFERRAL_REWARD = 300
COUNT_ONLY_PAID_FRIENDS = True       # بوست فقط با دوستانِ پرداخت‌کرده (ضد تقلب)
CHECKIN_REWARDS = [5, 10, 15, 20, 25, 30]   # روز ۱ تا ۶
TZ_OFFSET = 12600                    # تهران UTC+3:30 ؛ ریست حضور روزانه ساعت ۰۰:۰۰ تهران
# ==========================================


def get_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
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
        friend_id TEXT UNIQUE,
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
    # مهاجرت دیتابیس‌های قدیمی
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "checkin_streak" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN checkin_streak INTEGER DEFAULT 0")
    if "last_checkin_day" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN last_checkin_day INTEGER DEFAULT 0")
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ref_friend ON referrals(friend_id)")
    except sqlite3.IntegrityError:
        print("[DB] duplicate friend_id rows exist in referrals; unique index skipped")
    conn.commit()
    conn.close()


init_db()


class UserSyncRequest(BaseModel):
    user_id: str
    first_name: str
    referrer_id: str | None = None
    init_data: str | None = None


class ClaimMiningRequest(BaseModel):
    user_id: str
    amount: float | None = None
    init_data: str | None = None


class WithdrawRequest(BaseModel):
    user_id: str
    username: str
    amount: float
    address: str
    init_data: str | None = None


class MemberCheckRequest(BaseModel):
    user_id: str | None = None
    init_data: str | None = None


class ConfirmPaymentRequest(BaseModel):
    user_id: str
    wallet: str | None = None
    init_data: str | None = None


class ClaimRefRequest(BaseModel):
    user_id: str
    friend_id: str
    init_data: str | None = None


class CheckinRequest(BaseModel):
    user_id: str
    init_data: str | None = None


def verify_init_data(init_data: str) -> str | None:
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, received_hash):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > 86400 * 7:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return str(user.get("id"))
    except Exception as e:
        print(f"[verify_init_data] {e}")
        return None


def resolve_user_id(req) -> str:
    verified = verify_init_data(getattr(req, "init_data", None) or "")
    if verified:
        return verified
    if ALLOW_INSECURE and getattr(req, "user_id", None):
        return str(req.user_id)
    raise HTTPException(status_code=401, detail="Invalid Telegram session")


def rate_for(friends: int):
    if friends >= 10:
        return 150, 4
    if friends >= 6:
        return 130, 3
    if friends >= 3:
        return 110, 2
    return 80, 1


def boost_count(c, uid: str) -> int:
    if COUNT_ONLY_PAID_FRIENDS:
        c.execute("""SELECT COUNT(*) AS n FROM referrals r JOIN users u ON u.id=r.friend_id
                     WHERE r.referrer_id=? AND u.has_paid=1""", (uid,))
    else:
        c.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (uid,))
    return c.fetchone()["n"]


def expected_pay_amount(uid: str) -> int:
    """مبلغ یکتا برای هر کاربر (۰.۱ TON + چند نانو) تا تراکنش یک نفر را دیگری ادعا نکند."""
    try:
        return PAY_BASE_NANO + (int(uid) % 1_000_000)
    except ValueError:
        return PAY_BASE_NANO


def find_referrer(c, code: str | None):
    if not code:
        return None
    code = code.strip()
    c.execute("SELECT id FROM users WHERE id=?", (code,))
    r = c.fetchone()
    if r:
        return r["id"]
    if len(code) == 6:   # لینک‌های قدیمی (۶ رقم آخر)
        c.execute("SELECT id FROM users WHERE substr(id,-6)=? COLLATE NOCASE", (code,))
        rows = c.fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    return None


def register_with_referrer(uid: str, first_name: str, code: str | None):
    """کاربر را در صورت نبودن می‌سازد و اگر کد معتبر باشد (و کاربر هنوز رفرر ندارد) به رفرر وصل می‌کند."""
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        user = c.fetchone()
        if not user:
            c.execute("INSERT INTO users (id, first_name, last_mining) VALUES (?,?,?)",
                      (uid, first_name, time.time()))
            has_ref = False
        else:
            has_ref = bool(user["referrer_id"])
            if first_name and not user["first_name"]:
                c.execute("UPDATE users SET first_name=? WHERE id=?", (first_name, uid))
        if code and not has_ref:
            ref_id = find_referrer(c, code)
            if ref_id and ref_id != uid:
                c.execute("SELECT referrer_id FROM users WHERE id=?", (ref_id,))
                r = c.fetchone()
                if not (r and r["referrer_id"] == uid):
                    c.execute("UPDATE users SET referrer_id=? WHERE id=?", (ref_id, uid))
                    c.execute("INSERT OR IGNORE INTO referrals (referrer_id, friend_id, reward_claimed) VALUES (?,?,0)",
                              (ref_id, uid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- حضور روزانه ----------
def today_idx() -> int:
    return int((time.time() + TZ_OFFSET) // 86400)


def checkin_state(last_day: int, streak: int, today: int):
    """برمی‌گرداند: (روزهای تکمیل‌شده، آیا امروز قابل دریافت است)
    بعد از روز ۶ تمام می‌شود و دیگر قابل دریافت نیست. اگر یک روز جا بیفتد از روز ۱ شروع می‌شود."""
    last_day = last_day or 0
    streak = streak or 0
    total = len(CHECKIN_REWARDS)
    if streak >= total:
        return total, False           # کامل شده، برای همیشه
    if last_day == today:
        return streak, False
    if last_day == today - 1:
        return streak, True
    return 0, True                    # روز جا افتاده → از اول


def checkin_info(user) -> dict:
    today = today_idx()
    total = len(CHECKIN_REWARDS)
    claimed, available = checkin_state(user["last_checkin_day"], user["checkin_streak"], today)
    completed = claimed >= total
    if completed:
        next_day, next_reward = None, 0
    else:
        next_day = claimed + 1
        next_reward = CHECKIN_REWARDS[next_day - 1]
    return {
        "available": available,
        "completed": completed,
        "claimed_days": claimed,
        "next_day": next_day,
        "next_reward": next_reward,
        "rewards": CHECKIN_REWARDS,
    }


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
    allow_origins=["https://zyr-official.github.io"],
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
        ok = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
        return {"isMember": ok}
    except Exception as e:
        print(f"[check-member] {e}")
        return {"isMember": False, "error": str(e)}


@app.post("/sync-user")
async def sync_user(req: UserSyncRequest):
    uid = resolve_user_id(req)
    register_with_referrer(uid, req.first_name, req.referrer_id)

    conn = get_db()
    try:
        c = conn.cursor()
        now = time.time()

        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        user = c.fetchone()

        if uid == str(ADMIN_ID) and not user["has_paid"]:
            c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (now, uid))
            conn.commit()
            c.execute("SELECT * FROM users WHERE id=?", (uid,))
            user = c.fetchone()

        c.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (uid,))
        invited = c.fetchone()["n"]
        c.execute("""SELECT COUNT(*) AS n FROM referrals r JOIN users u ON u.id=r.friend_id
                     WHERE r.referrer_id=? AND u.has_paid=1""", (uid,))
        active = c.fetchone()["n"]

        rate, level = rate_for(boost_count(c, uid))
        last = user["last_mining"] if user["last_mining"] and user["last_mining"] > 0 else now
        pending = max(0, now - last) * rate / 86400 if user["has_paid"] else 0

        c.execute("""SELECT u.id, u.first_name, u.has_paid, r.reward_claimed
                     FROM referrals r JOIN users u ON r.friend_id=u.id
                     WHERE r.referrer_id=? ORDER BY r.id DESC""", (uid,))
        friends = [{
            "id": r["id"],
            "name": r["first_name"],
            "has_paid": bool(r["has_paid"]),
            "reward_claimed": bool(r["reward_claimed"]),
        } for r in c.fetchall()]

        return {
            "balance": round(user["balance"], 5),
            "pending_mined": round(pending, 5),
            "has_paid": bool(user["has_paid"]),
            "invited": invited,
            "active_invited": active,
            "referral_earned": round(user["referral_earned"], 2),
            "rate_per_day": rate,
            "level": level,
            "friends": friends,
            "checkin": checkin_info(user),
        }
    finally:
        conn.close()


@app.post("/claim-mining")
async def claim_mining(req: ClaimMiningRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT balance, last_mining, has_paid FROM users WHERE id=?", (uid,))
        user = c.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not user["has_paid"]:
            raise HTTPException(status_code=400, detail="Miner not activated")

        rate, _ = rate_for(boost_count(c, uid))
        now = time.time()
        last = user["last_mining"] if user["last_mining"] and user["last_mining"] > 0 else now
        earned = max(0, now - last) * rate / 86400
        new_balance = user["balance"] + earned
        c.execute("UPDATE users SET balance=?, last_mining=? WHERE id=?", (new_balance, now, uid))
        conn.commit()
        return {"ok": True, "new_balance": round(new_balance, 5)}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        print(f"[CLAIM ERROR] {e!r}")
        raise HTTPException(status_code=500, detail="Server error")
    finally:
        conn.close()


@app.post("/daily-checkin")
async def daily_checkin(req: CheckinRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        user = c.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if not user["has_paid"]:
            raise HTTPException(status_code=400, detail="Miner not activated")

        today = today_idx()
        claimed, available = checkin_state(user["last_checkin_day"], user["checkin_streak"], today)
        if not available:
            msg = "All 6 days completed" if claimed >= len(CHECKIN_REWARDS) else "Already checked in today"
            raise HTTPException(status_code=400, detail=msg)

        day = claimed + 1
        reward = CHECKIN_REWARDS[day - 1]
        c.execute("""UPDATE users SET balance=balance+?, checkin_streak=?, last_checkin_day=?
                     WHERE id=? AND COALESCE(last_checkin_day,0)<>?""",
                  (reward, day, today, uid, today))
        if c.rowcount != 1:
            raise HTTPException(status_code=400, detail="Already checked in today")
        conn.commit()
        c.execute("SELECT balance FROM users WHERE id=?", (uid,))
        return {"ok": True, "day": day, "reward": reward,
                "new_balance": round(c.fetchone()["balance"], 5)}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        print(f"[CHECKIN ERROR] {e!r}")
        raise HTTPException(status_code=500, detail="Server error")
    finally:
        conn.close()


async def find_payment(wallet: str, expected: int) -> str | None:
    """تراکنش ورودی با مبلغ دقیق، از کیف پول کاربر، در یک ساعت اخیر."""
    headers = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else {}
    params = {"account": TON_RECEIVER, "limit": 50, "sort": "desc"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://toncenter.com/api/v3/transactions", params=params, headers=headers)
        r.raise_for_status()
        txs = r.json().get("transactions", [])
    w = wallet.strip().lower()
    cutoff = time.time() - 3600
    conn = get_db()
    try:
        for tx in txs:
            msg = tx.get("in_msg") or {}
            src = (msg.get("source") or "").lower()
            try:
                value = int(msg.get("value") or 0)
            except ValueError:
                continue
            if src == w and value == expected and tx.get("now", 0) >= cutoff:
                h = tx.get("hash")
                if not conn.execute("SELECT 1 FROM payments WHERE tx_hash=?", (h,)).fetchone():
                    return h
    finally:
        conn.close()
    return None


@app.post("/confirm-payment")
async def confirm_payment(req: ConfirmPaymentRequest):
    uid = resolve_user_id(req)
    if not req.wallet:
        raise HTTPException(status_code=400, detail="wallet required")

    try:
        tx_hash = await find_payment(req.wallet, expected_pay_amount(uid))
    except Exception as e:
        print(f"[PAYMENT] toncenter error: {e!r}")
        raise HTTPException(status_code=502, detail="Blockchain lookup failed")
    if not tx_hash:
        raise HTTPException(status_code=404, detail="Payment not found yet")

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT OR IGNORE INTO users (id, last_mining) VALUES (?,?)", (uid, time.time()))
        c.execute("INSERT INTO payments (tx_hash, user_id, created) VALUES (?,?,?)",
                  (tx_hash, uid, time.time()))
        c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=? AND has_paid=0",
                  (time.time(), uid))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Transaction already used")
    finally:
        conn.close()
    return {"ok": True, "message": "Payment confirmed"}


@app.post("/claim-ref-reward")
async def claim_ref_reward(req: ClaimRefRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("""SELECT 1 FROM referrals r JOIN users u ON u.id=r.friend_id
                     WHERE r.referrer_id=? AND r.friend_id=? AND r.reward_claimed=0 AND u.has_paid=1""",
                  (uid, req.friend_id))
        if not c.fetchone():
            conn.rollback()
            return {"ok": False, "message": "Reward not available"}
        c.execute("UPDATE referrals SET reward_claimed=1 WHERE referrer_id=? AND friend_id=? AND reward_claimed=0",
                  (uid, req.friend_id))
        if c.rowcount != 1:
            conn.rollback()
            return {"ok": False, "message": "Reward not available"}
        c.execute("UPDATE users SET balance=balance+?, referral_earned=referral_earned+? WHERE id=?",
                  (REFERRAL_REWARD, REFERRAL_REWARD, uid))
        conn.commit()
        return {"ok": True, "message": f"{REFERRAL_REWARD} ZYR claimed!"}
    finally:
        conn.close()


@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawRequest):
    uid = resolve_user_id(req)
    if req.amount < MIN_WITHDRAW or len(req.address.strip()) < 48:
        raise HTTPException(status_code=400, detail="Invalid amount or address")

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=? AND has_paid=1",
                  (req.amount, uid, req.amount))
        if c.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=400, detail="Insufficient balance")
        c.execute("INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?,?,?, 'pending')",
                  (uid, req.amount, req.address.strip()))
        wd_id = c.lastrowid
        conn.commit()
    finally:
        conn.close()

    bot = Bot(token=BOT_TOKEN)
    text = (f"🔴 New Withdrawal Request\n\n"
            f"👤 User: {req.username} ({uid})\n"
            f"💰 Amount: {req.amount} ZYR\n"
            f"📫 Address: {req.address}")
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}"),
    ]]
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        print(f"[WITHDRAW] error: {e}")
    return {"ok": True}


@app.get("/tonconnect-manifest.json")
async def get_manifest():
    # کیف پول‌های وب باید بتوانند این فایل را از هر origin بخوانند
    return JSONResponse(
        {
            "url": "https://zyr-official.github.io/zyr-miner",
            "name": "ZYR Miner",
            "iconUrl": "https://zyr-official.github.io/zyr-miner/icon.png",
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    name = update.effective_user.first_name or "Miner"
    uid = str(update.effective_user.id)
    url = WEBAPP_URL
    ref_code = None
    if context.args and context.args[0].startswith("ref_"):
        ref_code = context.args[0][4:]
        url += f"?startapp={context.args[0]}"

    print(f"[START] uid={uid} args={context.args} ref_code={ref_code}")
    try:
        register_with_referrer(uid, name, ref_code)
        conn = get_db()
        row = conn.execute("SELECT referrer_id FROM users WHERE id=?", (uid,)).fetchone()
        conn.close()
        print(f"[START] saved referrer_id={row['referrer_id'] if row else None}")
    except Exception as e:
        print(f"[START] register error: {e!r}")

    text = (
        f"⚡ Welcome to ZYR Cloud Miner, {name}!\n\n"
        f"⛏️ Mine ZYR inside the app: start your session, claim tokens, and withdraw to TON.\n"
        f"💎 Activate your miner: deposit 0.1 TON to start earning 80 ZYR daily.\n"
        f"🎁 Daily check-in: claim up to 30 ZYR every day.\n"
        f"👥 Invite friends: boost your mining rate and earn 300 ZYR when they pay 0.1 TON.\n\n"
        f"👇 Tap below to start!"
    )
    keyboard = [[InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url))]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.from_user or query.from_user.id != ADMIN_ID:
        return
    try:
        action, wd_id = query.data.split("_", 1)
        wd_id = int(wd_id)
    except Exception:
        await query.edit_message_text("❌ Invalid callback data.")
        return

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wd_id,))
        wd = c.fetchone()
        if not wd:
            conn.rollback()
            await query.edit_message_text("⚠️ Already processed or not found.")
            return
        if action == "approve":
            c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
            msg = f"✅ Approved {wd['amount']} ZYR for user {wd['user_id']}.\n📫 {wd['address']}"
        else:
            c.execute("UPDATE users SET balance=balance+? WHERE id=?", (wd["amount"], wd["user_id"]))
            c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
            msg = f"❌ Rejected {wd['amount']} ZYR. Funds returned."
        conn.commit()
    finally:
        conn.close()
    await query.edit_message_text(text=msg)


telegram_app.add_handler(CommandHandler("start", start_command))
telegram_app.add_handler(CallbackQueryHandler(handle_callback))


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except Exception as e:
        print(f"[WEBHOOK] error: {e!r}")
    return {"ok": True}


@app.get("/")
def read_root():
    return {"status": "Backend is running"}
