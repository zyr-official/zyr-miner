import os
import sqlite3
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, CommandHandler

# ====== تنظیمات ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "9204005359:AAFFb-wJeEsKmNrOJSczuGGLx-CBv1TS2Ko")
ADMIN_ID = 1839907139
# دقت کن آدرس جدید گیت‌هابت (zyr-official) اینجا باشه
WEBAPP_URL = "https://zyr-official.github.io/zyr-miner/"
CHANNEL_ID = "@ZYRMining" 
# =====================

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- دیتابیس ---
def init_db():
    conn = sqlite3.connect('/tmp/zyr.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, address TEXT, status TEXT)''')
    conn.commit()
    conn.close()
init_db()

# --- مدل‌های داده ---
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
        else:
            return {"isMember": False}
    except Exception as e:
        return {"isMember": False, "error": str(e)}

# --- API برداشت ---
@app.post("/request-withdraw")
async def request_withdraw(req: WithdrawRequest):
    conn = sqlite3.connect('/tmp/zyr.db')
    c = conn.cursor()
    c.execute("INSERT INTO withdrawals (user_id, amount, address, status) VALUES (?, ?, ?, 'pending')", (req.user_id, req.amount, req.address))
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

# --- فایل تنظیمات کیف پول ---
@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse({
        "url": WEBAPP_URL,
        "name": "ZYR Miner",
        "iconUrl": "https://zyr-official.github.io/zyr-miner/icon.png"
    })

# --- ربات تلگرام (دستور استارت) ---
telegram_app = Application.builder().token(BOT_TOKEN).build()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "Miner"
    
    welcome_text = (
        f"⚡ **Welcome to ZYR Cloud Miner, {user_name}!**\n\n"
        f"⛏️ **Mine ZYR inside the app:** Start your mining session, come back later to claim your tokens, and request withdrawals directly to your TON wallet.\n\n"
        f"💎 **Activate your miner:** Deposit 0.1 TON to start your cloud miner and earn 100 ZYR daily.\n\n"
        f"👥 **Invite friends:** Share your referral link. When they join and activate their miner, you get 300 ZYR instantly.\n\n"
        f"📈 **Live Dashboard:** Track your mining rate, balance, and progress in real-time.\n\n"
        f"ℹ️ *ZYR Miner is an independent service.*\n\n"
        f"👇 **Tap the button below to start!**"
    )
    
    keyboard = [[
        InlineKeyboardButton("🚀 Open ZYR Miner", web_app=WebAppInfo(url=WEBAPP_URL))
    ]]
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

telegram_app.add_handler(CommandHandler("start", start_command))

# --- دکمه‌های تایید/رد ادمین ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action, wd_id, user_id, amount = data[0], data[1], data[2], float(data[3])

    if action == "approve":
        await query.edit_message_text(text=f"✅ Approved {amount} ZYR for user {user_id}.")
    elif action == "reject":
        await query.edit_message_text(text=f"❌ Rejected {amount} ZYR for user {user_id}.")

telegram_app.add_handler(CallbackQueryHandler(handle_callback))

# --- وب‌هوک تلگرام ---
@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
def read_root():
    return {"status": "Backend is running"}
