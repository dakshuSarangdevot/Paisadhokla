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

# ================= EVENT LOOP FIX =================

loop = asyncio.new_event_loop()

def run_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=run_loop, daemon=True).start()

# ================= LOGGING =================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= ENV =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"

SEARCH_COST = 5

# ================= PACKAGES =================

PREMIUM_PACKAGES = [
{"id": "basic", "name": "🟢 BASIC", "points": 50, "price": 500},
{"id": "pro", "name": "🟡 PRO", "points": 250, "price": 2000},
{"id": "elite", "name": "🔴 ELITE", "points": 1000, "price": 7000}
]

# ================= GLOBAL =================

PAYMENT_REQUESTS = {}

app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()

# ================= DATABASE =================

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

CREATE TABLE IF NOT EXISTS protected_ids(
id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS banned_users(
id TEXT PRIMARY KEY
);
""")

    conn.commit()
    # ================= USER SYSTEM =================

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

def approve_user(uid):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("UPDATE users SET approved=1 WHERE id=?", (str(uid),))

    conn.commit()

def is_banned(uid):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM banned_users WHERE id=?", (str(uid),))

    return cursor.fetchone() is not None
    # ================= ADMIN COMMANDS =================

async def add_points(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /addpoints USER_ID AMOUNT")
        return

    uid = context.args[0]
    amount = int(context.args[1])

    update_user_points(uid, amount)

    await update.message.reply_text(f"Added {amount} points to {uid}")


async def remove_points(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /removepoints USER_ID AMOUNT")
        return

    uid = context.args[0]
    amount = int(context.args[1])

    update_user_points(uid, -amount)

    await update.message.reply_text(f"Removed {amount} points from {uid}")


async def user_info(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /userinfo USER_ID")
        return

    uid = context.args[0]

    user = get_user(uid)

    if not user:
        await update.message.reply_text("User not found")
        return

    msg = f"""
USER INFO

ID: {uid}
USERNAME: {user['username']}
POINTS: {user['points']}
APPROVED: {user['approved']}
JOINED: {user['created_at']}
"""

    await update.message.reply_text(msg)


async def protect_id(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = context.args[0]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("INSERT OR IGNORE INTO protected_ids(id) VALUES(?)",(uid,))

    conn.commit()

    await update.message.reply_text(f"Protected {uid}")


async def unprotect_id(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = context.args[0]

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM protected_ids WHERE id=?", (uid,))

    conn.commit()

    await update.message.reply_text(f"Unprotected {uid}")
    # ================= HANDLERS =================

telegram_app.add_handler(CommandHandler("addpoints", add_points))
telegram_app.add_handler(CommandHandler("removepoints", remove_points))
telegram_app.add_handler(CommandHandler("userinfo", user_info))
telegram_app.add_handler(CommandHandler("protectid", protect_id))
telegram_app.add_handler(CommandHandler("unprotectid", unprotect_id))

# ================= WEBHOOK =================

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():

    update = Update.de_json(
        request.get_json(force=True),
        telegram_app.bot
    )

    asyncio.run_coroutine_threadsafe(
        telegram_app.process_update(update),
        loop
    )

    return "OK"


@app.route("/")
def home():
    return "PREMIUM OSINT BOT RUNNING"


# ================= STARTUP =================

async def init_bot():

    await telegram_app.initialize()

    await telegram_app.bot.set_webhook(
    f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

    logger.info("Bot Initialized")


if __name__ == "__main__":

    if not all([BOT_TOKEN, OWNER_CHAT_ID, WEBHOOK_URL]):

        print("Missing ENV variables")

        exit(1)

    loop.create_task(init_bot())

    port = int(os.getenv("PORT", 5000))

    app.run(host="0.0.0.0", port=port)
