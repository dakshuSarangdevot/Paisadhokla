import os
import json
import time
import asyncio
import httpx
from flask import Flask, request

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUser
)

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

            if not data or "result" not in data:
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
📡 OSINT RESULT

━━━━━━━━━━━━━━
👤 Telegram ID
`{result['tg_id']}`

📞 Phone
`{result['country_code']} {result['number']}`

🌍 Country
{result['country']}

━━━━━━━━━━━━━━
"""

    await update.message.reply_text(msg, parse_mode="Markdown")

# ---------------- START ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    balance = get_points(user.id)

    keyboard = [
        [
            KeyboardButton(
                "🎯 Target",
                request_user=KeyboardButtonRequestUser(
                    request_id=1,
                    user_is_bot=False
                )
            )
        ],
        [
            KeyboardButton("📊 Stats"),
            KeyboardButton("🆘 Help")
        ]
    ]

    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    msg = f"""
✨ WELCOME TO OSINT BOT

👤 User: {user.first_name}
🆔 UID: `{user.id}`
💰 Balance: `{balance}` pts

Send:
• Telegram ID
• @username
• or use Target button
"""

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)

    if user.id not in approved_users:

        admin_msg = f"""
Access Request

{user.first_name}
@{user.username}
ID: {user.id}

/approve {user.id}
"""

        await context.bot.send_message(OWNER_CHAT_ID, admin_msg)

# ---------------- USER PICKER ----------------

async def user_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):

    uid = update.message.user_shared.user_id

    await lookup(update, context, str(uid))

# ---------------- STATS ----------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):

    uid = update.effective_user.id
    balance = get_points(uid)

    msg = f"""
📊 YOUR STATS

User ID: `{uid}`
Points: `{balance}`
"""

    await update.message.reply_text(msg, parse_mode="Markdown")

# ---------------- HELP ----------------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = """
🆘 COMMANDS

/start - Start bot
/help - Show help
/stats - Show balance

Admin:
/approve ID
/addpoints ID AMOUNT
/protectid ID
/admin
/broadcast MESSAGE
"""

    await update.message.reply_text(msg)

# ---------------- ADMIN ----------------

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    msg = f"""
ADMIN PANEL

Users: {len(users)}
Approved: {len(approved_users)}
Protected: {len(protected_ids)}
"""

    await update.message.reply_text(msg)

# ---------------- BROADCAST ----------------

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    message = " ".join(context.args)

    sent = 0

    for uid in users:

        try:

            await context.bot.send_message(uid, message)
            sent += 1

        except:
            pass

    await update.message.reply_text(f"Broadcast sent to {sent} users")

# ---------------- OWNER COMMANDS ----------------

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = int(context.args[0])
    approved_users.add(uid)
    save_lists()

    await update.message.reply_text("User approved")

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

# ---------------- TEXT HANDLER ----------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "📊 Stats":
        await stats(update, context)
        return

    if text == "🆘 Help":
        await help_cmd(update, context)
        return

    if text.startswith("@"):

        username = text.replace("@", "")

        try:

            chat = await context.bot.get_chat(f"@{username}")
            uid = chat.id

            await lookup(update, context, str(uid))

        except:

            await update.message.reply_text("❌ Cannot resolve username")

        return

    if text.isdigit():

        await lookup(update, context, text)

# ---------------- HANDLERS ----------------

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_cmd))
telegram_app.add_handler(CommandHandler("stats", stats))
telegram_app.add_handler(CommandHandler("admin", admin))
telegram_app.add_handler(CommandHandler("broadcast", broadcast))
telegram_app.add_handler(CommandHandler("approve", approve))
telegram_app.add_handler(CommandHandler("addpoints", addpoints))
telegram_app.add_handler(CommandHandler("protectid", protectid))

telegram_app.add_handler(
    MessageHandler(filters.StatusUpdate.USER_SHARED, user_shared)
)

telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
)

# ---------------- WEBHOOK ----------------

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

@app.route("/")
def home():
    return "Bot running"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():

    update = Update.de_json(request.get_json(force=True), telegram_app.bot)

    loop.run_until_complete(
        telegram_app.process_update(update)
    )

    return "ok"

# ---------------- START ----------------

load_data()

async def setup():

    await telegram_app.initialize()

    try:

        await telegram_app.bot.set_webhook(
            f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )

        print("Webhook set successfully")

    except Exception as e:

        print("Webhook error:", e)

loop.run_until_complete(setup())
