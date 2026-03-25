# Dhokla Bot v4 Stable (No referral system)

import os
import time
import sqlite3
import asyncio
import httpx
import logging
from flask import Flask, request
from contextlib import contextmanager
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SEARCH_COST = 5
API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"

app = Flask(__name__)
telegram_app = None


# ================= DATABASE =================

@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        points INTEGER DEFAULT 0,
        approved INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_search REAL
        );

        CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        target TEXT,
        result TEXT,
        timestamp TEXT
        );
        """)


# ================= USER =================

def ensure_user(uid, username=None, name=None):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id,username,first_name) VALUES(?,?,?)",
            (str(uid), username, name)
        )


def get_user(uid):
    ensure_user(uid)
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id=?",
            (str(uid),)
        ).fetchone()


def add_points(uid, amount):
    with db() as conn:
        conn.execute(
            "UPDATE users SET points = points + ? WHERE id=?",
            (amount, str(uid))
        )


# ================= SEARCH =================

async def search_target(target):

    async with httpx.AsyncClient(timeout=15) as client:

        r = await client.get(
            API_URL,
            params={"key": API_KEY, "id": target}
        )

        data = r.json()

        return data.get("result")


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    ensure_user(user.id, user.username, user.first_name)

    data = get_user(user.id)

    if data["approved"] != 1 and user.id != OWNER_CHAT_ID:

        await update.message.reply_text(
            "❌ Waiting for admin approval"
        )

        return

    balance = data["points"]

    keyboard = [
        ["SEARCH"],
        ["STATS", "BUY POINTS"]
    ]

    await update.message.reply_text(
        f"💰 Balance: {balance}",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# ================= SEARCH HANDLER =================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text
    user = update.effective_user
    data = get_user(user.id)

    if text == "STATS":

        await update.message.reply_text(
            f"💰 Points: {data['points']}"
        )

        return

    if text == "BUY POINTS":

        await update.message.reply_text(
            "Contact admin to buy points"
        )

        return

    if data["points"] < SEARCH_COST:

        await update.message.reply_text(
            "❌ Not enough points"
        )

        return

    await update.message.reply_text("🔎 Searching...")

    result = await search_target(text)

    add_points(user.id, -SEARCH_COST)

    if not result:

        await update.message.reply_text("❌ No data found")

        return

    await update.message.reply_text(str(result))


# ================= ADMIN COMMANDS =================

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve userID")
        return

    uid = context.args[0]

    with db() as conn:
        conn.execute("UPDATE users SET approved=1 WHERE id=?", (uid,))

    await update.message.reply_text(f"✅ Approved {uid}")


async def addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = context.args[0]
    amount = int(context.args[1])

    add_points(uid, amount)

    await update.message.reply_text(f"Added {amount} points to {uid}")


async def godstats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    with db() as conn:

        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        points = conn.execute("SELECT SUM(points) FROM users").fetchone()[0] or 0

    await update.message.reply_text(
        f"""
👑 ADMIN PANEL

Users: {users}
Total Points: {points}
"""
    )


# ================= WEBHOOK =================

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():

    global telegram_app

    try:

        # Lazy initialization (fix for Render/Gunicorn)
        if telegram_app is None:

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def init():
                global telegram_app

                telegram_app = Application.builder().token(BOT_TOKEN).build()

                telegram_app.add_handler(CommandHandler("start", start))
                telegram_app.add_handler(CommandHandler("approve", approve))
                telegram_app.add_handler(CommandHandler("addpoints", addpoints))
                telegram_app.add_handler(CommandHandler("godstats", godstats))

                telegram_app.add_handler(MessageHandler(filters.TEXT, handle_text))

                await telegram_app.initialize()

            loop.run_until_complete(init())

        data = request.get_json()

        update = Update.de_json(data, telegram_app.bot)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        loop.run_until_complete(
            telegram_app.process_update(update)
        )

        return "OK"

    except Exception as e:

        logger.error(e)

        return "ERROR", 500


    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}")


if __name__ == "__main__":

    asyncio.run(init())

    port = int(os.getenv("PORT", 10000))

    app.run(host="0.0.0.0", port=port)
