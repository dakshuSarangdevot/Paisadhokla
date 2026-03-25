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

cursor.execute("""
CREATE TABLE IF NOT EXISTS logs(
user_id TEXT,
target TEXT,
timestamp TEXT
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
    cursor.execute("SELECT points FROM users WHERE id=?", (str(uid),))
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

# ---------------- SEARCH LOG ----------------

def log_search(user_id,target):

    timestamp=time.strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        "INSERT INTO logs(user_id,target,timestamp) VALUES(?,?,?)",
        (user_id,target,timestamp)
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
        await update.message.reply_text("⛔ Not approved yet.")
        return

    now = time.time()

    if user.id in last_query and now-last_query[user.id] < RATE_LIMIT:
        await update.message.reply_text("⏳ Slow down.")
        return

    last_query[user.id] = now

    points = get_points(user.id)

    if points < SEARCH_COST:
        await update.message.reply_text("❌ Not enough points.")
        return

    remove_points(user.id,SEARCH_COST)

    loading = await update.message.reply_text("🔎 Searching database...")

    result = await fetch_api(target)

    if not result:
        await loading.edit_text("❌ No record found.")
        return

    log_search(str(user.id),target)

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

    await loading.edit_text(msg,parse_mode="Markdown")

# ---------------- START ----------------

async def start(update,context):

    user=update.effective_user
    ensure_user(user.id)

    balance=get_points(user.id)

    keyboard=[
        [
            KeyboardButton(
                "🎯 Select Telegram Target",
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

👤 {user.first_name}
🆔 `{user.id}`
💰 Balance: `{balance}`

Send:
Telegram ID
@username
or 🎯 Target
"""

    await update.message.reply_text(msg,parse_mode="Markdown",reply_markup=markup)

    if not is_approved(user.id):

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve",callback_data=f"approve_{user.id}"),
                InlineKeyboardButton("❌ Reject",callback_data=f"reject_{user.id}")
            ]
        ])

        await context.bot.send_message(
            OWNER_CHAT_ID,
            f"🚨 Access Request\n\n{user.first_name}\nID: {user.id}",
            reply_markup=keyboard
        )

# ---------------- TARGET ----------------

async def user_shared(update,context):

    shared=update.message.user_shared

    if not shared:
        return

    await lookup(update,context,str(shared.user_id))

# ---------------- ADMIN BUTTONS ----------------

async def admin_buttons(update,context):

    query=update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    uid=query.data.split("_")[1]

    if query.data.startswith("approve_"):
        approve_user(uid)
        await query.edit_message_text(f"✅ Approved {uid}")

    if query.data.startswith("reject_"):
        disapprove_user(uid)
        await query.edit_message_text(f"❌ Rejected {uid}")

# ---------------- STATS ----------------

async def stats(update,context):

    balance=get_points(update.effective_user.id)

    await update.message.reply_text(
f"""
📊 YOUR STATS

Points: `{balance}`
""",
parse_mode="Markdown"
)

# ---------------- SEARCH LOGS ----------------

async def logs(update,context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    cursor.execute(
        "SELECT user_id,target,timestamp FROM logs ORDER BY ROWID DESC LIMIT 10"
    )

    rows=cursor.fetchall()

    if not rows:
        await update.message.reply_text("No logs.")
        return

    text="🔎 LAST SEARCHES\n\n"

    for r in rows:

        text+=f"User: {r[0]}\nTarget: {r[1]}\nTime: {r[2]}\n\n"

    await update.message.reply_text(text)

# ---------------- HELP ----------------

async def help_cmd(update,context):

    if update.effective_user.id == OWNER_CHAT_ID:

        msg="""
/start
/help
/stats

ADMIN:
/approve ID
/disapprove ID
/addpoints ID AMOUNT
/broadcast MESSAGE
/logs
"""

    else:

        msg="""
/start
/help
/stats
"""

    await update.message.reply_text(msg)

# ---------------- BROADCAST ----------------

async def broadcast(update,context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast MESSAGE")
        return

    msg=" ".join(context.args)

    cursor.execute("SELECT id FROM users")

    sent=0

    for row in cursor.fetchall():

        try:
            await context.bot.send_message(row[0],msg)
            sent+=1
        except:
            pass

    await update.message.reply_text(f"Sent to {sent}")

# ---------------- TEXT HANDLER ----------------

async def text_handler(update,context):

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
            await update.message.reply_text("Username not found")

        return

    if text.isdigit():
        await lookup(update,context,text)

# ---------------- HANDLERS ----------------

telegram_app.add_handler(CommandHandler("start",start))
telegram_app.add_handler(CommandHandler("help",help_cmd))
telegram_app.add_handler(CommandHandler("stats",stats))
telegram_app.add_handler(CommandHandler("broadcast",broadcast))
telegram_app.add_handler(CommandHandler("logs",logs))

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
