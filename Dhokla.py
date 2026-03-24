import os
import re
import time
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"

approved_users = set()
banned_ids = set()
last_query = {}

RATE_LIMIT = 5

app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()


async def lookup(update, context, target):

    user = update.effective_user

    if target in banned_ids:
        await update.message.reply_text("🚫 This ID is blocked.")
        return

    now = time.time()

    if user.id in last_query and now - last_query[user.id] < RATE_LIMIT:
        await update.message.reply_text("⏳ Slow down.")
        return

    last_query[user.id] = now

    try:
        r = requests.get(API_URL, params={
            "key": API_KEY,
            "id": target
        }, timeout=10)

        data = r.text

    except:
        data = "⚠ API error"

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

    await update.message.reply_text(f"Approved {uid}")


async def disapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid = int(context.args[0])

    if uid in approved_users:
        approved_users.remove(uid)

    await update.message.reply_text(f"Removed {uid}")


async def banid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    banned_ids.add(context.args[0])

    await update.message.reply_text("ID banned")


async def unbanid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    banned_ids.remove(context.args[0])

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

    try:
        r = requests.get(API_URL, params={
            "key": API_KEY,
            "id": query
        })

        result = r.text

    except:
        result = "API error"

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


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    await telegram_app.process_update(update)
    return "ok"


import asyncio

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    asyncio.run(telegram_app.process_update(update))
    return "ok"
