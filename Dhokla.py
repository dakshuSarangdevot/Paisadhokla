import os
import time
import sqlite3
import asyncio
import httpx
import threading
import logging
from datetime import datetime
from flask import Flask, request
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestUser,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters
)
from telegram.constants import ParseMode
from telegram.constants import ParseMode as ParseModeConst

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"
SEARCH_COST = 5

PREMIUM_PACKAGES = [
    {"id": "basic", "name": "🟢 BASIC", "points": 50, "price": 500},
    {"id": "pro", "name": "🟡 PRO", "points": 250, "price": 2000},
    {"id": "elite", "name": "🔴 ELITE", "points": 1000, "price": 7000}
]

PAYMENT_REQUESTS = {}

app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()

local = threading.local()


def get_db():
    if not hasattr(local, 'conn'):
        local.conn = sqlite3.connect("database.db", check_same_thread=False)
        local.conn.row_factory = sqlite3.Row
        create_tables(local.conn)
    return local.conn


def create_tables(conn):
    cursor = conn.cursor()
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        points INTEGER DEFAULT 0,
        approved INTEGER DEFAULT 0,
        referrals INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_search REAL
    );
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        target TEXT,
        result TEXT,
        timestamp TEXT,
        points_spent INTEGER
    );
    CREATE TABLE IF NOT EXISTS purchases(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        package TEXT,
        points INTEGER,
        price INTEGER,
        timestamp TEXT
    );
    """)
    conn.commit()


def ensure_user(uid, username=None, first_name=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users(id, username, first_name) VALUES(?,?,?)",
        (str(uid), username, first_name)
    )
    conn.commit()


def get_user(uid):
    ensure_user(uid)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id=?", (str(uid),))
    return cursor.fetchone()


def update_user_points(uid, points_change):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET points = MAX(0, points + ?) WHERE id=?",
        (points_change, str(uid))
    )
    conn.commit()


def can_search(uid):
    user = get_user(uid)
    if not user or user['approved'] != 1:
        return False, "⛔ Not approved by admin"
    if user['points'] < SEARCH_COST:
        return False, f"💸 Need {SEARCH_COST} points"
    if user['last_search'] and time.time() - user['last_search'] < 30:
        return False, "⏳ Wait 30 seconds"
    return True, "OK"


def approve_user(uid):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET approved=1 WHERE id=?", (str(uid),))
    conn.commit()


async def search_target(target):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                API_URL,
                params={"key": API_KEY, "id": target},
                headers={"User-Agent": "OSINT-Bot/3.0"}
            )
            data = response.json()
            return data.get("result") if "result" in data else None
    except:
        return None


async def perform_lookup(update, context, target):
    user_id = update.effective_user.id

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_search=? WHERE id=?", (time.time(), str(user_id)))

    update_user_points(user_id, -SEARCH_COST)

    loading_msg = await update.message.reply_text(
        "🔎 **GLOBAL DATABASE SCAN**\n\n"
        "```▰▰▰▱▱▱▱▱▱```\n"
        "*Max 30 seconds...*",
        parse_mode=ParseModeConst.MARKDOWN
    )

    result = await search_target(target)

    if not result:
        await loading_msg.edit_text(
            "❌ **NO RECORDS FOUND**\n\n"
            f"💰 **Balance:** `{get_user(user_id)['points']}`",
            parse_mode=ParseModeConst.MARKDOWN
        )
        return

    cursor.execute(
        "INSERT INTO logs(user_id,target,result,timestamp,points_spent) VALUES(?,?,?,?,?)",
        (str(user_id), target, str(result), time.strftime("%Y-%m-%d %H:%M:%S"), SEARCH_COST)
    )
    conn.commit()

    result_text = f"""
🎯 **OSINT INTEL REPORT**

👤 TG ID: `{result.get('tg_id','N/A')}`
📱 PHONE: `{result.get('number','N/A')}`
🌍 COUNTRY: {result.get('country','Unknown')}

💰 Balance: `{get_user(user_id)['points']}`
"""
    await loading_msg.edit_text(result_text, parse_mode=ParseModeConst.MARKDOWN)


async def start(update, context):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)

    keyboard = [
        [KeyboardButton("🎯 SEARCH TARGET", request_user=KeyboardButtonRequestUser(request_id=1, user_is_bot=False))],
        ["💰 BUY POINTS", "📊 STATS"]
    ]

    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "🌟 **PREMIUM DASHBOARD** 🌟",
        parse_mode=ParseModeConst.MARKDOWN,
        reply_markup=markup
    )


async def handle_text(update, context):
    text = update.message.text
    user_id = update.effective_user.id

    if text == "💰 BUY POINTS":
        await update.message.reply_text("Choose package")
        return

    target = text.strip()

    if target.isdigit() or (text.startswith('@') and len(text) > 1):
        can, reason = can_search(user_id)

        if not can:
            await update.message.reply_text(reason)
            return

        await perform_lookup(update, context, target)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(telegram_app.initialize())


@app.route("/")
def home():
    return "🚀 PREMIUM OSINT BOT v3.0 - LIVE 💎"


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)

    loop.run_until_complete(telegram_app.process_update(update))

    return "OK"


async def init_bot():
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}")
    logger.info("Bot webhook set")


if __name__ == "__main__":

    if not all([BOT_TOKEN, OWNER_CHAT_ID, WEBHOOK_URL]):
        print("Missing ENV variables")
        exit(1)

    loop.run_until_complete(init_bot())

    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
