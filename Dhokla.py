import os
import json
import time
import asyncio
import httpx
from flask import Flask, request

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"

SEARCH_COST = 1
RATE_LIMIT = 5

app = Flask(__name__)

telegram_app = Application.builder().token(BOT_TOKEN).build()

users = {}
approved_users = set()
protected_ids = set()
last_query = {}

# ---------------- STORAGE ----------------

def load_json(file, default):
    try:
        with open(file) as f:
            return json.load(f)
    except:
        return default

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f)

def load_data():
    global users, approved_users, protected_ids

    users = load_json("users.json", {})
    approved_users.update(load_json("approved.json", []))
    protected_ids.update(load_json("protected.json", []))

def save_users():
    save_json("users.json", users)

def save_lists():
    save_json("approved.json", list(approved_users))
    save_json("protected.json", list(protected_ids))

# ---------------- POINTS ----------------

def get_points(uid):

    uid = str(uid)

    if uid not in users:
        users[uid] = {"points": 0}

    return users[uid]["points"]

def add_points(uid, amount):

    uid = str(uid)

    if uid not in users:
        users[uid] = {"points": 0}

    users[uid]["points"] += amount
    save_users()

def remove_points(uid, amount):

    uid = str(uid)

    if uid not in users:
        users[uid] = {"points": 0}

    users[uid]["points"] -= amount

    if users[uid]["points"] < 0:
        users[uid]["points"] = 0

    save_users()

# ---------------- API ----------------

async def fetch_api(target):

    try:

        async with httpx.AsyncClient(timeout=10) as client:

            r = await client.get(
                API_URL,
                params={"key": API_KEY, "id": target}
            )

            data = r.json()

            if "result" not in data:
                return None

            return data["result"]

    except:
        return None

# ---------------- LOOKUP ----------------

async def lookup(update, context, target):

    user = update.effective_user

    if target in protected_ids:
        await update.message.reply_text("🚫 This ID is protected.")
        return

    if user.id not in approved_users:
        await update.message.reply_text("⛔ Not approved.")
        return

    now = time.time()

    if user.id in last_query and now - last_query[user.id] < RATE_LIMIT:
        await update.message.reply_text("⏳ Slow down.")
        return

    last_query[user.id] = now

    points = get_points(user.id)

    if points < SEARCH_COST:
        await update.message.reply_text("❌ Not enough points.")
        return

    remove_points(user.id, SEARCH_COST)

    result = await fetch_api(target)

    if not result:
        await update.message.reply_text("❌ No record found.")
        return

    msg = f"""
📡 *OSINT RESULT*

━━━━━━━━━━━━━━
👤 *Telegram ID*
`{result['tg_id']}`

📞 *Phone*
`{result['country_code']} {result['number']}`

🌍 *Country*
{result['country']}

━━━━━━━━━━━━━━
"""

    await update.message.reply_text(msg, parse_mode="Markdown")

# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    keyboard = [
        [KeyboardButton("🔎 Lookup Database")],
        [KeyboardButton("📊 Stats")]
    ]

    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    if user.id not in approved_users:

        await update.message.reply_text("⛔ Not approved.", reply_markup=markup)

        msg = f"""
Access Request

{user.first_name}
@{user.username}
ID: {user.id}

/approve {user.id}
"""

        await context.bot.send_message(OWNER_CHAT_ID, msg)
        return

    await update.message.reply_text("✨ Welcome to OSINT Bot", reply_markup=markup)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    uid = update.effective_user.id
    balance = get_points(uid)

    msg = f"""
📊 YOUR STATS

User ID: {uid}
Points: {balance}
"""

    await update.message.reply_text(msg)

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = int(context.args[0])

    approved_users.add(uid)
    save_lists()

    await update.message.reply_text(f"Approved {uid}")

async def addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = int(context.args[0])
    pts = int(context.args[1])

    add_points(uid, pts)

    await update.message.reply_text("Points added")

async def protectid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    protected_ids.add(context.args[0])
    save_lists()

    await update.message.reply_text("ID protected")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "📊 Stats":
        await stats(update, context)
        return

    if text.isdigit():
        await lookup(update, context, text)

# ---------------- HANDLERS ----------------

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("approve", approve))
telegram_app.add_handler(CommandHandler("addpoints", addpoints))
telegram_app.add_handler(CommandHandler("protectid", protectid))

telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
)

# ---------------- WEBHOOK ----------------

@app.route("/")
def home():
    return "Bot running"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
async def webhook():

    update = Update.de_json(
        request.get_json(force=True),
        telegram_app.bot
    )

    await telegram_app.process_update(update)

    return "ok"

# ---------------- STARTUP ----------------

load_data()

def start_bot():

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def setup():

        await telegram_app.initialize()

        await telegram_app.bot.set_webhook(
            f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )

    loop.run_until_complete(setup())

start_bot()
