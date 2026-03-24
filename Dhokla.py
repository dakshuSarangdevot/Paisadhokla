import os
import json
import time
import asyncio
import requests
from flask import Flask, request

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"

SEARCH_COST = 1
RATE_LIMIT = 5

users = {}
protected_ids = set()
approved_users = set()
last_query = {}

app = Flask(name)

#---------- FILE STORAGE ----------

def load_users():
global users
try:
with open("users.json") as f:
users = json.load(f)
except:
users = {}

def save_users():
with open("users.json","w") as f:
json.dump(users,f)

---------- USER POINTS ----------

def get_points(uid):
if str(uid) not in users:
users[str(uid)] = {"points":0}
return users[str(uid)]["points"]

def add_points(uid, amount):
if str(uid) not in users:
users[str(uid)] = {"points":0}
users[str(uid)]["points"] += amount
save_users()

def remove_points(uid, amount):
if str(uid) not in users:
users[str(uid)] = {"points":0}

users[str(uid)]["points"] -= amount

if users[str(uid)]["points"] < 0:
    users[str(uid)]["points"] = 0

save_users()

---------- API ----------

def fetch_api(target):

for _ in range(2):
    try:
        r = requests.get(
            API_URL,
            params={"key":API_KEY,"id":target},
            timeout=10
        )

        data = r.json()

        if not data.get("result"):
            return None

        return data["result"]

    except:
        time.sleep(1)

return None

---------- TELEGRAM ----------

request_config = HTTPXRequest(connect_timeout=20, read_timeout=20, write_timeout=20)

telegram_app = Application.builder()
.token(BOT_TOKEN)
.request(request_config)
.build()

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

---------- LOOKUP ----------

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

result = fetch_api(target)

if not result:
    await update.message.reply_text("❌ No record found.")
    return

tg_id = result["tg_id"]
number = result["number"]
country = result["country"]
code = result["country_code"]

message = f"""

📡 OSINT RESULT

━━━━━━━━━━━━━━
👤 Telegram ID
"{tg_id}"

📞 Phone
"{code} {number}"

🌍 Country
{country}

━━━━━━━━━━━━━━
"""

await update.message.reply_text(message, parse_mode="Markdown")

---------- COMMANDS ----------

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):

user = update.effective_user

keyboard = [
    [KeyboardButton("🔎 Lookup Database")],
    [KeyboardButton("🎯 Target", request_user=True)],
    [KeyboardButton("📊 Stats")]
]

markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

if user.id not in approved_users:

    await update.message.reply_text(
        "⛔ Not approved.",
        reply_markup=markup
    )

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

async def stats(update:Update, context:ContextTypes.DEFAULT_TYPE):

uid = update.effective_user.id

balance = get_points(uid)

msg = f"""

📊 YOUR STATS

User ID: {uid}
Points: {balance}
"""

await update.message.reply_text(msg)

---------- ADMIN ----------

async def approve(update:Update, context:ContextTypes.DEFAULT_TYPE):

if update.effective_user.id != OWNER_CHAT_ID:
    return

uid = int(context.args[0])

approved_users.add(uid)

await update.message.reply_text(f"Approved {uid}")

async def addpoints(update:Update, context:ContextTypes.DEFAULT_TYPE):

if update.effective_user.id != OWNER_CHAT_ID:
    return

uid = int(context.args[0])
pts = int(context.args[1])

add_points(uid, pts)

await update.message.reply_text("Points added")

async def protectid(update:Update, context:ContextTypes.DEFAULT_TYPE):

if update.effective_user.id != OWNER_CHAT_ID:
    return

protected_ids.add(context.args[0])

await update.message.reply_text("ID protected")

---------- MESSAGE HANDLER ----------

async def text_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):

text = update.message.text

if text == "📊 Stats":
    await stats(update, context)
    return

if text.isdigit():
    await lookup(update, context, text)

---------- TARGET BUTTON ----------

async def target_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):

if update.message.user_shared:

    uid = update.message.user_shared.user_id

    await lookup(update, context, str(uid))

---------- HANDLERS ----------

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("approve", approve))
telegram_app.add_handler(CommandHandler("addpoints", addpoints))
telegram_app.add_handler(CommandHandler("protectid", protectid))

telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.USER_SHARED, target_handler))

---------- WEBHOOK ----------

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

---------- STARTUP ----------

load_users()

loop.run_until_complete(telegram_app.initialize())
loop.run_until_complete(telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}"))
