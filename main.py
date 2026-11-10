import os
import sqlite3
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, CommandHandler

# ====== تنظیمات ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "9204005359:AAFFb-wJeEsKmNrOJSczuGGLx-CBv1TS2Ko")
ADMIN_ID = 1839907139
WEBAPP_URL = "https://zyr-official.github.io/zyr-miner/app.html"
CHANNEL_ID = "@ZYRMining" 
# =====================

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- دیتابیس ---
def get_db():
    conn = sqlite3.connect('/tmp/zyr.db')
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
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, address TEXT, status TEXT)''')
    conn.commit()
    conn.close()
init_db()

# --- مدل‌های داده ---
class UserSyncRequest(BaseModel):
    user_id: str
    first_name: str
    referrer_id: str | None = None

class WithdrawRequest(BaseModel):
    user_id: str
    username: str
    amount: float
    address: str

class MemberCheckRequest(BaseModel):
    user_id: str

# --- API بررسی عضویت در کانال ---
@app.post("/check-member")
async def check_member(req: MemberCheckRequest):
    bot = Bot(token=BOT_TOKEN)
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=int(req.user_id))
        if member.status in ['member', 'administrator', 'creator']:
            return {"isMember": True}
        return {"isMember": False}
    except Exception as e:
        return {"isMember": False, "error": str(e)}

# --- API سینک کردن کاربر (ورود اولیه) ---
@app.post("/sync-user")
async def sync_user(req: UserSyncRequest):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()
    
    if not user:
        c.execute("INSERT INTO users (id, first_name, referrer_id, last_mining) VALUES (?, ?, ?, ?)", 
                  (req.user_id, req.first_name, req.referrer_id, time.time()))
        conn.commit()
        c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
        user = c.fetchone()
    
    # ✅ شرط ادمین: اگر آیدی با آیدی ادمین یکی بود، خودکار پرداخت شده حساب میشه
    if req.user_id == str(ADMIN_ID) and not user['has_paid']:
        c.execute("UPDATE users SET has_paid=1 WHERE id=?", (req.user_id,))
        conn.commit()
        c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
        user = c.fetchone()
    
    c.execute("SELECT COUNT(*) as count FROM users WHERE referrer_id=?", (req.user_id,))
    invited_count = c.fetchone()['count']
    
    if invited_count >= 20:
        rate_per_day = 150
        level = 4
    elif invited_count >= 10:
        rate_per_day = 130
        level = 3
    elif invited_count >= 5:
        rate_per_day = 110
        level = 2
    else:
        rate_per_day = 80 
        level = 1
    
    tokens_per_second = rate_per_day / 86400
    
    time_diff = time.time() - user['last_mining']
    earned = time_diff * tokens_per_second
    new_balance = user['balance'] + earned
    
    c.execute("UPDATE users SET balance=?, last_mining=? WHERE id=?", (new_balance, time.time(), req.user_id))
    conn.commit()
    
    conn.close()
    return {
        "balance": round(new_balance, 5),
        "has_paid": bool(user['has_paid']),
        "invited": invited_count,
        "referral_earned": round(user['referral_earned'], 2),
        "rate_per_day": rate_per_day,
        "level": level
    }

# --- API ثبت پرداخت (تاییدیه) ---
@app.post("/confirm-payment")
async def confirm_payment(req: MemberCheckRequest):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()
    
    if user and not user['has_paid']:
        c.execute("UPDATE users SET has_paid=1 WHERE id=?", (req.user_id,))
        
        if user['referrer_id']:
            c.execute("UPDATE users SET balance=balance+300, referral_earned=referral_earned+300 WHERE id=?", (user['referrer_id'],))
        
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Payment confirmed"}
        
    conn.close()
    return {"ok": False, "message": "Already paid or user not found"}

# --- API برداشت ---
@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawRequest):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE id=?", (req.user_id,))
    user = c.fetchone()
    
    if not user or user['balance'] < req.amount:
        conn.close()
        raise HTTPException(status_code=400, detail="Insufficient balance")
        
    c.execute("INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?, ?, ?, 'pending')", (req.user_id, req.amount, req.address))
    c.execute("UPDATE users SET balance = balance - ? WHERE id=?", (req.amount, req.user_id))
    wd_id = c.lastrowid
    conn.commit()
    conn.close()

    bot = Bot(token=BOT_TOKEN)
    text = (f"🔴 **New Withdrawal Request**\n\n"
            f"👤 User: {req.username} (`{req.user_id}`)\n"
            f"💰 Amount: {req.amount} ZYR\n"
            f"📫 Address: `{req.address}`")
    
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wd_id}_{req.user_id}_{req.amount}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wd_id}_{req.user_id}_{req.amount}")
    ]]
    
    await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return {"ok": True}

@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse({
        "url": WEBAPP_URL,
        "name": "ZYR Miner",
        "iconUrl": "https://zyr-official.github.io/zyr-miner/icon.png"
    })

# --- ربات تلگرام ---
telegram_app = Application.builder().token(BOT_TOKEN).build()

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
        f"⛏️ **Mine ZYR inside the app:** Start your session, claim tokens, and withdraw to TON.\n"
        f"💎 **Activate your miner:** Deposit 0.1 TON to start earning 80 ZYR daily.\n"
        f"👥 **Invite friends:** Boost your mining rate (up to 150 ZYR/day) and earn 300 ZYR per friend.\n\n"
        f"👇 **Tap below to start!**"
    )
    keyboard = [[ InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=url)) ]]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

telegram_app.add_handler(CommandHandler("start", start_command))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action, wd_id, user_id, amount = data[0], data[1], data[2], float(data[3])
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

telegram_app.add_handler(CallbackQueryHandler(handle_callback))

@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
def read_root():
    return {"status": "Backend is running"}
