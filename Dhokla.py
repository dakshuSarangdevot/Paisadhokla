import os
import re
import time
import json
import asyncio
import requests
from flask import Flask, request

from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    ContextTypes,
    filters
)

from telegram.request import HTTPXRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"

RATE_LIMIT = 5
last_query = {}

USERS_FILE = "approved_users.json"
BANNED_FILE = "banned_ids.json"


def load_set(file):
    if not os.path.exists(file):
        return set()
    with open(file, "r") as f:
        return set(json.load(f))


def save_set(file, data):
    with open(file, "w") as f:
        json.dump(list(data), f)


approved_users = load_set(USERS_FILE)
banned_ids = load_set(BANNED_FILE)

app = Flask(__name__)

# Telegram request config (prevents timeout issues)
tg_request = HTTPXRequest(connect_timeout=20, read_timeout=20, write_timeout=20)

telegram_app = Application.builder()\
    .token(BOT_TOKEN)\
    .request(tg_request)\
    .build()

# Create event loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

loop.run_until_complete(telegram_app.initialize())


def fetch_api(target):
    for _ in range(2):
        try:
            r = requests.get(API_URL, params={
                "key": API_KEY,
                "id": target
            }, timeout=10)

            return r.text
        except:
            time.sleep(1)

    return "⚠ API error"


async def lookup(update, context, target):

    user = update.effective_user

    if str(target) in banned_ids:
        await update.message.reply_text("🚫 This ID is blocked.")
        return

    now = time.time()

    if user.id in last_query and now - last_query[user.id] < RATE_LIMIT:
        await update.message.reply_text("⏳ Slow down.")
        return

    last_query[user.id] = now

    data = fetch_api(target)

    await update.message.reply_text(data)

    notify = (
        f"📢 BOT SEARCH\n\n"
        f"User: {user.first_name}\n"
        f"Username: @{user.username}\n"
        f"UserID: {user.id}\n\n"
        f"Target: {target}"
    )

    try:
        await context.bot.send_message(OWNER_CHAT_ID, notify)
    except:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if user.id not in approved_users:

        await update.message.reply_text("⛔ Not approved.")

        msg = (
            f"Access request\n"
            f"{user.first_name} @{user.username}\n"
            f"{user.id}\n"
            f"/approve {user.id}"
        )

        await context.bot.send_message(OWNER_CHAT_ID, msg)

        return

    await update.message.reply_text("Send Telegram ID or username.")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = int(context.args[0])

    approved_users.add(uid)
    save_set(USERS_FILE, approved_users)

    await update.message.reply_text(f"Approved {uid}")


async def disapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = int(context.args[0])

    approved_users.discard(uid)
    save_set(USERS_FILE, approved_users)

    await update.message.reply_text(f"Removed {uid}")


async def banid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    banned_ids.add(context.args[0])
    save_set(BANNED_FILE, banned_ids)

    await update.message.reply_text("ID banned")


async def unbanid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    banned_ids.discard(context.args[0])
    save_set(BANNED_FILE, banned_ids)

    await update.message.reply_text("ID unbanned")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if user.id not in approved_users:
        await update.message.reply_text("⛔ Not approved.")
        return

    text = update.message.text

    ids = re.findall(r"\d{5,15}", text)

    if ids:
        await lookup(update, context, ids[0])
        return

    usernames = re.findall(r"@([A-Za-z0-9_]{5,})", text)

    if usernames:

        uname = "@" + usernames[0]

        try:
            chat = await context.bot.get_chat(uname)
            await lookup(update, context, chat.id)
        except:
            await update.message.reply_text("Username not found")


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.inline_query.query

    if not query:
        return

    result = fetch_api(query)

    results = [
        InlineQueryResultArticle(
            id="1",
            title="Lookup Result",
            input_message_content=InputTextMessageContent(result),
            description=query
        )
    ]

    await update.inline_query.answer(results, cache_time=1)


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("approve", approve))
telegram_app.add_handler(CommandHandler("disapprove", disapprove))
telegram_app.add_handler(CommandHandler("banid", banid))
telegram_app.add_handler(CommandHandler("unbanid", unbanid))

telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
telegram_app.add_handler(InlineQueryHandler(inline_query))


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
