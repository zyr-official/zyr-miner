import os
import sqlite3
import time
import hashlib
import hmac
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl
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
DB_PATH = os.getenv("DB_PATH", "/tmp/zyr.db")
# =====================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
    amount: float
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
    tx_hash: str | None = None
    init_data: str | None = None

class ClaimRefRequest(BaseModel):
    user_id: str
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
        import json
        user = json.loads(parsed.get("user", "{}"))
        return str(user.get("id"))
    except Exception as e:
        print(f"[verify_init_data] {e}")
        return None

def resolve_user_id(req) -> str:
    if getattr(req, 'init_data', None):
        verified = verify_init_data(req.init_data)
        if verified:
            return verified
    if getattr(req, 'user_id', None):
        return str(req.user_id)
    raise HTTPException(status_code=400, detail="user_id required")

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
            ChatMemberStatus.CREATOR,
        ):
            return {"isMember": True}
        return {"isMember": False}
    except Exception as e:
        return {"isMember": False, "error": str(e)}

@app.post("/sync-user")
async def sync_user(req: UserSyncRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (uid,))
    user = c.fetchone()

    current_time = time.time()

    if not user:
        c.execute(
            "INSERT INTO users (id, first_name, referrer_id, last_mining) VALUES (?, ?, ?, ?)",
            (uid, req.first_name, req.referrer_id, current_time)
        )
        if req.referrer_id:
            c.execute("SELECT id FROM users WHERE substr(id, -6) = ? COLLATE NOCASE", (req.referrer_id,))
            ref_user = c.fetchone()
            if ref_user and ref_user['id'] != uid:
                c.execute("INSERT INTO referrals (referrer_id, friend_id, reward_claimed) VALUES (?, ?, 0)", (ref_user['id'], uid))
                c.execute("UPDATE users SET referrer_id=? WHERE id=?", (ref_user['id'], uid))
        
        conn.commit()
        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        user = c.fetchone()

    if uid == str(ADMIN_ID) and not user['has_paid']:
        c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (current_time, uid))
        conn.commit()
        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        user = c.fetchone()

    c.execute("SELECT COUNT(*) as count FROM users WHERE referrer_id=?", (uid,))
    invited_count = c.fetchone()['count']

    c.execute("SELECT COUNT(*) as count FROM users u JOIN referrals r ON u.id = r.friend_id WHERE r.referrer_id=? AND u.has_paid=1", (uid,))
    active_invited_count = c.fetchone()['count']

    if invited_count >= 10:
        rate_per_day, level = 150, 4
    elif invited_count >= 6:
        rate_per_day, level = 130, 3
    elif invited_count >= 3:
        rate_per_day, level = 110, 2
    else:
        rate_per_day, level = 80, 1

    tokens_per_second = rate_per_day / 86400
    last_mining = user['last_mining'] if (user['last_mining'] and user['last_mining'] > 0) else current_time
    time_diff = max(0, current_time - last_mining)

    # محاسبه دقیق مقدار استخراج شده در زمان آفلاین بودن
    pending_mined = time_diff * tokens_per_second if user['has_paid'] else 0

    c.execute("SELECT u.id, u.first_name, u.has_paid, r.reward_claimed FROM referrals r JOIN users u ON r.friend_id = u.id WHERE r.referrer_id=?", (uid,))
    friends_rows = c.fetchall()
    friends_list = []
    for row in friends_rows:
        friends_list.append({
            "id": row['id'],
            "name": row['first_name'],
            "has_paid": bool(row['has_paid']),
            "reward_claimed": bool(row['reward_claimed'])
        })

    conn.close()

    return {
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

@app.post("/claim-mining")
async def claim_mining(req: ClaimMiningRequest):
    try:
        uid = resolve_user_id(req)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT balance, last_mining, has_paid FROM users WHERE id=?", (uid,))
        user = c.fetchone()
        
        current_time = time.time()
        
        if not user:
            conn.close()
            raise HTTPException(status_code=404, detail="User not found")
            
        if not user['has_paid']:
            conn.close()
            raise HTTPException(status_code=400, detail="Miner not activated")

        c.execute("SELECT COUNT(*) as count FROM users WHERE referrer_id=?", (uid,))
        invited_count = c.fetchone()['count']
        
        if invited_count >= 10: rate_per_day = 150
        elif invited_count >= 6: rate_per_day = 130
        elif invited_count >= 3: rate_per_day = 110
        else: rate_per_day = 80

        tokens_per_sec = rate_per_day / 86400
        last_mining = user['last_mining'] if (user['last_mining'] and user['last_mining'] > 0) else current_time
        time_diff = max(0, current_time - last_mining)
        server_pending = time_diff * tokens_per_sec

        new_balance = user['balance'] + server_pending
        # آپدیت زمان شروع ماینینگ جدید به الان
        c.execute("UPDATE users SET balance = ?, last_mining = ? WHERE id = ?", (new_balance, current_time, uid))
            
        conn.commit()
        conn.close()
        return {"ok": True, "new_balance": round(new_balance, 5)}
    except Exception as e:
        print(f"[CLAIM ERROR] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/confirm-payment")
async def confirm_payment(req: ConfirmPaymentRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (uid,))
    user = c.fetchone()

    current_time = time.time()
    if not user:
        c.execute("INSERT INTO users (id, has_paid, last_mining) VALUES (?, 1, ?)", (uid, current_time))
    else:
        c.execute("UPDATE users SET has_paid=1, last_mining=? WHERE id=?", (current_time, uid))
        
    conn.commit()
    conn.close()
    return {"ok": True, "message": "Payment confirmed"}

@app.post("/claim-ref-reward")
async def claim_ref_reward(req: ClaimRefRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM referrals WHERE referrer_id=? AND friend_id=? AND reward_claimed=0", (uid, req.friend_id))
    ref = c.fetchone()
    if not ref:
        conn.close()
        return {"ok": False, "message": "Reward not available"}

    c.execute("SELECT has_paid FROM users WHERE id=?", (req.friend_id,))
    friend = c.fetchone()
    if not friend or not friend['has_paid']:
        conn.close()
        return {"ok": False, "message": "Friend has not paid 0.1 TON yet"}

    c.execute("UPDATE users SET balance = balance + 300, referral_earned = referral_earned + 300 WHERE id=?", (uid,))
    c.execute("UPDATE referrals SET reward_claimed = 1 WHERE referrer_id=? AND friend_id=?", (uid, req.friend_id))
    
    conn.commit()
    conn.close()
    return {"ok": True, "message": "300 ZYR claimed!"}

@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawRequest):
    uid = resolve_user_id(req)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE id=?", (uid,))
    user = c.fetchone()

    if not user or user['balance'] < req.amount:
        conn.close()
        raise HTTPException(status_code=400, detail="Insufficient balance")

    c.execute(
        "INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?, ?, ?, 'pending')",
        (uid, req.amount, req.address)
    )
    c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (req.amount, uid))
    wd_id = c.lastrowid
    conn.commit()
    conn.close()

    bot = Bot(token=BOT_TOKEN)
    text = (f"🔴 **New Withdrawal Request**\n\n"
            f"👤 User: {req.username} (`{uid}`)\n"
            f"💰 Amount: {req.amount} ZYR\n"
            f"📫 Address: `{req.address}`")

    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}_{uid}_{req.amount}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}_{uid}_{req.amount}")
    ]]

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
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
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        parts = query.data.split("_", 3)
        action, wd_id, user_id, amount_str = parts[0], parts[1], parts[2], parts[3]
        amount = float(amount_str)
    except Exception:
        await query.edit_message_text("❌ Invalid callback data.")
        return

    conn = get_db()
    c = conn.cursor()
    if action == "approve":
        c.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wd_id,))
        await query.edit_message_text(text=f"✅ Approved {amount} ZYR for user {user_id}.")
    elif action == "reject":
        c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id))
        c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wd_id,))
        await query.edit_message_text(text=f"❌ Rejected {amount} ZYR. Funds returned.")
    conn.commit()
    conn.close()

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
