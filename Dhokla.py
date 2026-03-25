import os
import time
import sqlite3
import asyncio
import httpx
from flask import Flask, request

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUser,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
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

# ---------------- DATABASE ----------------

conn = sqlite3.connect("database.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
id TEXT PRIMARY KEY,
points INTEGER DEFAULT 0,
approved INTEGER DEFAULT 0
)
""")

conn.commit()

# ---------------- USER SYSTEM ----------------

def ensure_user(uid):

    cursor.execute(
        "INSERT OR IGNORE INTO users(id,points,approved) VALUES(?,?,?)",
        (str(uid),0,0)
    )

    conn.commit()

def get_points(uid):

    ensure_user(uid)

    cursor.execute(
        "SELECT points FROM users WHERE id=?",
        (str(uid),)
    )

    return cursor.fetchone()[0]

def add_points(uid,amount):

    ensure_user(uid)

    cursor.execute(
        "UPDATE users SET points = points + ? WHERE id=?",
        (amount,str(uid))
    )

    conn.commit()

def remove_points(uid,amount):

    ensure_user(uid)

    cursor.execute(
        "UPDATE users SET points = MAX(points-?,0) WHERE id=?",
        (amount,str(uid))
    )

    conn.commit()

def is_approved(uid):

    ensure_user(uid)

    cursor.execute(
        "SELECT approved FROM users WHERE id=?",
        (str(uid),)
    )

    return cursor.fetchone()[0] == 1

def approve_user(uid):

    ensure_user(uid)

    cursor.execute(
        "UPDATE users SET approved=1 WHERE id=?",
        (str(uid),)
    )

    conn.commit()

def disapprove_user(uid):

    cursor.execute(
        "UPDATE users SET approved=0 WHERE id=?",
        (str(uid),)
    )

    conn.commit()

# ---------------- API ----------------

async def fetch_api(target):

    try:

        async with httpx.AsyncClient(timeout=10) as client:

            r = await client.get(
                API_URL,
                params={"key":API_KEY,"id":target}
            )

            data = r.json()

            if "result" not in data:
                return None

            return data["result"]

    except:
        return None

# ---------------- LOOKUP ----------------

last_query = {}

async def lookup(update,context,target):

    user = update.effective_user

    if not is_approved(user.id):

        await update.message.reply_text("⛔ You are not approved yet.")
        return

    now = time.time()

    if user.id in last_query and now-last_query[user.id] < RATE_LIMIT:

        await update.message.reply_text("⏳ Slow down")
        return

    last_query[user.id] = now

    points = get_points(user.id)

    if points < SEARCH_COST:

        await update.message.reply_text("❌ Not enough points")
        return

    remove_points(user.id,SEARCH_COST)

    result = await fetch_api(target)

    if not result:

        await update.message.reply_text("❌ No record found")
        return

    msg=f"""
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

    await update.message.reply_text(msg,parse_mode="Markdown")

# ---------------- START ----------------

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    user=update.effective_user

    ensure_user(user.id)

    balance=get_points(user.id)

    keyboard=[
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

    markup=ReplyKeyboardMarkup(keyboard,resize_keyboard=True)

    msg=f"""
✨ WELCOME TO OSINT BOT

👤 User: {user.first_name}
🆔 UID: `{user.id}`
💰 Balance: `{balance}` pts

Send:
• Telegram ID
• @username
• or press Target
"""

    await update.message.reply_text(msg,parse_mode="Markdown",reply_markup=markup)

    if not is_approved(user.id):

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve",callback_data=f"approve_{user.id}"),
                InlineKeyboardButton("❌ Reject",callback_data=f"reject_{user.id}")
            ]
        ])

        notify=f"""
🚨 USER REQUEST

👤 {user.first_name}
🆔 {user.id}
"""

        await context.bot.send_message(
            OWNER_CHAT_ID,
            notify,
            reply_markup=keyboard
        )

# ---------------- ADMIN BUTTONS ----------------

async def admin_buttons(update:Update,context:ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    data = query.data
    uid = data.split("_")[1]

    if data.startswith("approve_"):

        approve_user(uid)

        await query.edit_message_text(
            f"✅ User {uid} approved"
        )

    elif data.startswith("reject_"):

        disapprove_user(uid)

        await query.edit_message_text(
            f"❌ User {uid} rejected"
        )

# ---------------- USER PICKER ----------------

async def user_shared(update:Update,context:ContextTypes.DEFAULT_TYPE):

    uid = update.message.user_shared.user_id
    await lookup(update,context,str(uid))

# ---------------- STATS ----------------

async def stats(update:Update,context:ContextTypes.DEFAULT_TYPE):

    uid = update.effective_user.id
    balance = get_points(uid)

    msg=f"""
📊 YOUR STATS

User ID: `{uid}`
Points: `{balance}`
"""

    await update.message.reply_text(msg,parse_mode="Markdown")

# ---------------- HELP ----------------

async def help_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id == OWNER_CHAT_ID:

        msg="""
🆘 COMMANDS

/start
/help
/stats

ADMIN:
/approve ID
/disapprove ID
/addpoints ID AMOUNT
/broadcast MESSAGE
/admin
"""

    else:

        msg="""
🆘 COMMANDS

/start
/help
/stats

Send:
Telegram ID
@username
or press Target
"""

    await update.message.reply_text(msg)

# ---------------- ADMIN PANEL ----------------

async def admin(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users WHERE approved=1")
    approved = cursor.fetchone()[0]

    msg=f"""
ADMIN PANEL

Users: {total}
Approved: {approved}
"""

    await update.message.reply_text(msg)

# ---------------- BROADCAST ----------------

async def broadcast(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    message=" ".join(context.args)

    cursor.execute("SELECT id FROM users")

    sent=0

    for row in cursor.fetchall():

        try:

            await context.bot.send_message(row[0],message)
            sent+=1

        except:
            pass

    await update.message.reply_text(f"Sent to {sent} users")

# ---------------- ADMIN COMMANDS ----------------

async def approve(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid=context.args[0]
    approve_user(uid)

    await update.message.reply_text("User approved")

async def disapprove(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid=context.args[0]
    disapprove_user(uid)

    await update.message.reply_text("User disapproved")

async def addpoints(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid=context.args[0]
    pts=int(context.args[1])

    add_points(uid,pts)

    await update.message.reply_text("Points added")

# ---------------- TEXT HANDLER ----------------

async def text_handler(update:Update,context:ContextTypes.DEFAULT_TYPE):

    text=update.message.text

    if text=="📊 Stats":
        await stats(update,context)
        return

    if text=="🆘 Help":
        await help_cmd(update,context)
        return

    if text.startswith("@"):

        try:

            chat=await context.bot.get_chat(text)
            await lookup(update,context,str(chat.id))

        except:

            await update.message.reply_text("❌ Username not found")

        return

    if text.isdigit():

        await lookup(update,context,text)

# ---------------- HANDLERS ----------------

telegram_app.add_handler(CommandHandler("start",start))
telegram_app.add_handler(CommandHandler("help",help_cmd))
telegram_app.add_handler(CommandHandler("stats",stats))
telegram_app.add_handler(CommandHandler("admin",admin))
telegram_app.add_handler(CommandHandler("broadcast",broadcast))
telegram_app.add_handler(CommandHandler("approve",approve))
telegram_app.add_handler(CommandHandler("disapprove",disapprove))
telegram_app.add_handler(CommandHandler("addpoints",addpoints))

telegram_app.add_handler(CallbackQueryHandler(admin_buttons))

telegram_app.add_handler(
    MessageHandler(filters.StatusUpdate.USER_SHARED,user_shared)
)

telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler)
)

# ---------------- WEBHOOK ----------------

@app.route("/")
def home():
    return "Bot running"

@app.route(f"/{BOT_TOKEN}",methods=["POST"])
def webhook():

    update = Update.de_json(
        request.get_json(force=True),
        telegram_app.bot
    )

    async def process():
        await telegram_app.process_update(update)

    asyncio.run(process())

    return "ok"

# ---------------- START ----------------

async def setup():
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(f"{WEBHOOK_URL}/{BOT_TOKEN}")
    print("Webhook set successfully")

asyncio.run(setup())
