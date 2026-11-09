import os
import sqlite3
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# ====== تنظیمات ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "9204005359:AAFFb-wJeEsKmNrOJSczuGGLx-CBv1TS2Ko")
ADMIN_ID = 1839907139
# =====================

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def init_db():
    conn = sqlite3.connect('/tmp/zyr.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, address TEXT, status TEXT)''')
    conn.commit()
    conn.close()
init_db()

class WithdrawRequest(BaseModel):
    user_id: str
    username: str
    amount: float
    address: str

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

@app.get("/tonconnect-manifest.json")
async def get_manifest():
    return JSONResponse({
        "url": "https://sinapalani-bot.github.io/zyr-miner/",
        "name": "ZYR Miner",
        "iconUrl": "https://sinapalani-bot.github.io/zyr-miner/icon.png"
    })

telegram_app = Application.builder().token(BOT_TOKEN).build()

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

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
def read_root():
    return {"status": "Backend is running"}
