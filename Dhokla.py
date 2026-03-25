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
from telegram.constants import ParseMode  # ← ADD THIS LINE
from telegram.constants import ParseMode as ParseModeConst

# Logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ENV VARS

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"
SEARCH_COST = 5

# Premium Packages

PREMIUM_PACKAGES = [
    {"id": "basic", "name": "🟢 BASIC", "points": 50, "price": 500},
    {"id": "pro", "name": "🟡 PRO", "points": 250, "price": 2000},
    {"id": "elite", "name": "🔴 ELITE", "points": 1000, "price": 7000}
]

# Global payment tracking

PAYMENT_REQUESTS = {}

app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()

# Thread-safe Database

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


#═══════════════════════════════════════════════════════ USER SYSTEM ═══════════════════════════════════════════════════════

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


#═══════════════════════════════════════════════════════ OSINT ENGINE ═══════════════════════════════════════════════════════

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

    # Update last search
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_search=? WHERE id=?", (time.time(), str(user_id)))

    # Deduct points
    update_user_points(user_id, -SEARCH_COST)

    # Loading animation
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
            f"💰 **Balance:** `{get_user(user_id)['points']}`\n"
            "👆 Try another ID",
            parse_mode=ParseModeConst.MARKDOWN
        )
        return

    # Log search
    cursor.execute(
        "INSERT INTO logs(user_id,target,result,timestamp,points_spent) VALUES(?,?,?,?,?)",
        (str(user_id), target, str(result), time.strftime("%Y-%m-%d %H:%M:%S"), SEARCH_COST)
    )
    conn.commit()

    # Premium result
    result_text = f"""

🎯 OSINT INTEL REPORT 🎯

━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 TG ID: {result.get('tg_id', 'N/A')}
📱 PHONE: {result.get('country_code', '')} {result.get('number', 'N/A')}
🌍 COUNTRY: {result.get('country', 'Unknown')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ Balance: {get_user(user_id)['points']} points
🔄 Next search ready!
"""
    await loading_msg.edit_text(result_text, parse_mode=ParseModeConst.MARKDOWN)


#═══════════════════════════════════════════════════════ MAIN HANDLERS ═══════════════════════════════════════════════════════

async def start(update, context):

    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)

    user_data = get_user(user.id)

    balance = user_data['points'] if user_data else 0
    approved = user_data['approved'] == 1 if user_data else False

    if not approved and user.id != OWNER_CHAT_ID:

        keyboard = [[InlineKeyboardButton("💎 REQUEST ACCESS", callback_data="request_access")]]
        markup = InlineKeyboardMarkup(keyboard)

        msg = f"""

🔥 PREMIUM OSINT BOT 🔥

👤 {user.first_name}
🆔 {user.id}
💎 Status: PENDING APPROVAL

⚠️ Admin approval required first!
"""

        await update.message.reply_text(msg, parse_mode=ParseModeConst.MARKDOWN, reply_markup=markup)

        # Notify owner
        owner_msg = f"""

🚨 NEW USER REQUEST 🚨

👤 {user.first_name} (@{user.username or 'N/A'})
🆔 {user.id}

[APPROVE/REJECT] 👇
"""

        kb = [
            [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{user.id}")],
            [InlineKeyboardButton("❌ DENY", callback_data=f"deny_{user.id}")]
        ]

        await context.bot.send_message(
            OWNER_CHAT_ID,
            owner_msg,
            parse_mode=ParseModeConst.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )

        return

    # Premium dashboard
    keyboard = [
        [KeyboardButton("🎯 SEARCH TARGET", request_user=KeyboardButtonRequestUser(request_id=1, user_is_bot=False))],
        ["💰 BUY POINTS", "📊 STATS"],
        ["👥 REFER", "🆘 SUPPORT"]
    ]

    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    dashboard = f"""

🌟 PREMIUM DASHBOARD 🌟

👤 {user.first_name}
💰 Balance: {balance} pts
⚡ Searches left: {balance//SEARCH_COST}

🎯 Send ID/@username or use button
Each search = {SEARCH_COST} points
"""

    await update.message.reply_text(dashboard, parse_mode=ParseModeConst.MARKDOWN, reply_markup=markup)

# Remaining code continues exactly same...

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "request_access":
        await query.edit_message_text(
            "✅ **Request sent to admin!**",
            parse_mode=ParseModeConst.MARKDOWN
        )
        return

    if user_id != OWNER_CHAT_ID:
        return

    # Admin actions
    if data.startswith("approve_"):
        uid = data.split("_")[1]
        approve_user(uid)
        await query.edit_message_text(f"✅ **APPROVED {uid}** ✅")

    elif data.startswith("deny_"):
        uid = data.split("_")[1]
        await query.edit_message_text(f"❌ **DENIED {uid}** ❌")


#═══════════════════════════════════════════════════════ PAYMENT SYSTEM ═══════════════════════════════════════════════════════

async def buy_points(update, context):
    keyboard = []

    for pkg in PREMIUM_PACKAGES:
        keyboard.append([
            InlineKeyboardButton(
                f"{pkg['name']} - ₹{pkg['price']} ({pkg['points']}pts)",
                callback_data=f"buy_{pkg['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    ])

    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "💎 CHOOSE PACKAGE:",
        reply_markup=markup,
        parse_mode=ParseModeConst.MARKDOWN
    )


async def buy_package_callback(update, context):
    query = update.callback_query
    pkg_id = query.data.split("_")[1]
    user_id = update.effective_user.id

    package = next(p for p in PREMIUM_PACKAGES if p['id'] == pkg_id)

    PAYMENT_REQUESTS[user_id] = {
        'package': package,
        'timestamp': time.time(),
        'status': 'pending'
    }

    msg = f"""

💰 PAYMENT PENDING 💰

📦 {package['name']}
💎 {package['points']} Points
💵 ₹{package['price']}

📱 UPI: sarangdevotarjunsingh9@okhdfc
📱 PhonePe/Paytm: Contact Admin

✅ Send screenshot after payment!
"""

    keyboard = [
        [InlineKeyboardButton("✅ PAID!", callback_data=f"paid_{user_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]

    await query.edit_message_text(
        msg,
        parse_mode=ParseModeConst.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def payment_proof_handler(update, context):

    user_id = update.effective_user.id

    if user_id not in PAYMENT_REQUESTS:
        return

    proof = (
        update.message.photo[-1].file_id
        if update.message.photo
        else update.message.text
    )

    package = PAYMENT_REQUESTS[user_id]['package']

    forward_text = f"""

🚨 PAYMENT PROOF 🚨

👤 {user_id}
📦 {package['name']}
💰 ₹{package['price']}

[VERIFY] 👇
"""

    keyboard = [
        [InlineKeyboardButton("✅ APPROVE", callback_data=f"confirm_{user_id}")],
        [InlineKeyboardButton("❌ REJECT", callback_data=f"reject_{user_id}")]
    ]

    await context.bot.send_photo(
        OWNER_CHAT_ID,
        proof,
        caption=forward_text,
        parse_mode=ParseModeConst.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await update.message.reply_text("✅ **Sent to admin!** ⏳")


async def confirm_payment(update, context):

    query = update.callback_query
    user_id = query.data.split("_")[1]

    package = PAYMENT_REQUESTS[int(user_id)]['package']

    update_user_points(user_id, package['points'])

    # Log purchase
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO purchases(user_id,package,points,price,timestamp) VALUES(?,?,?,?,?)",
        (
            user_id,
            package['name'],
            package['points'],
            package['price'],
            time.strftime("%Y-%m-%d %H:%M:%S")
        )
    )

    conn.commit()

    PAYMENT_REQUESTS[int(user_id)]['status'] = 'approved'

    await context.bot.send_message(
        user_id,
        f"🎉 **PAYMENT APPROVED!** 🎉\n\n"
        f"💎 **+{package['points']} Points**\n"
        f"💰 **Balance:** `{get_user(user_id)['points']}`\n\n"
        f"🚀 **Start searching!**",
        parse_mode=ParseModeConst.MARKDOWN
    )

    await query.edit_message_text(
        f"✅ **{user_id} APPROVED** ✅\n"
        f"💎 {package['points']}pts added!"
    )


#═══════════════════════════════════════════════════════ USER FEATURES ═══════════════════════════════════════════════════════

async def show_stats(update, context):

    user = get_user(update.effective_user.id)

    stats = f"""
📊 YOUR STATS 📊

👤 {user['first_name']}
💰 Points: {user['points']}
⚡ Searches left: {user['points']//SEARCH_COST}
📅 Joined: {user['created_at'][:10]}

🔥 Ready to hunt!
"""

    await update.message.reply_text(
        stats,
        parse_mode=ParseModeConst.MARKDOWN
    )


async def handle_text(update, context):

    text = update.message.text
    user_id = update.effective_user.id

    if text == "💰 BUY POINTS":
        await buy_points(update, context)
        return

    if text == "📊 STATS":
        await show_stats(update, context)
        return

    # Search
    target = text.strip()

    if target.isdigit() or (text.startswith('@') and len(text) > 1):

        can, reason = can_search(user_id)

        if not can:
            await update.message.reply_text(reason)
            return

        await perform_lookup(update, context, target)


async def handle_user_share(update, context):

    if update.message.user_shared:

        target = str(update.message.user_shared.user_id)

        can, reason = can_search(update.effective_user.id)

        if not can:
            await update.message.reply_text(reason)
            return

        await perform_lookup(update, context, target)


#═══════════════════════════════════════════════════════ OWNER GOD MODE ═══════════════════════════════════════════════════════

async def god_stats(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    conn = get_db()
    cursor = conn.cursor()

    stats = {
        'total_users': cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        'premium_users': cursor.execute("SELECT COUNT(*) FROM users WHERE approved=1").fetchone()[0],
        'total_points': cursor.execute("SELECT SUM(points) FROM users").fetchone()[0] or 0,
        'today_searches': cursor.execute("SELECT COUNT(*) FROM logs WHERE date(timestamp)=date('now')").fetchone()[0],
        'pending_payments': len([
            uid for uid, data in PAYMENT_REQUESTS.items()
            if data['status'] == 'pending'
        ])
    }

    dashboard = f"""

👑 GOD MODE v3.0 👑

👥 Total Users: {stats['total_users']}
💎 Premium: {stats['premium_users']}
💰 Points Value: ₹{stats['total_points']//10:,}
🔍 Today Searches: {stats['today_searches']}
⏳ Pending Payments: {stats['pending_payments']}

👇 Commands:
/pending - Payment queue
/broadcast MSG - Mass send
/wipeall - NUKE DATABASE ⚠️
"""

    await update.message.reply_text(
        dashboard,
        parse_mode=ParseModeConst.MARKDOWN
    )

async def pending_payments(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if not PAYMENT_REQUESTS:
        await update.message.reply_text("✅ **No pending payments**")
        return

    msg = "⏳ **PENDING PAYMENTS:**\n\n"

    for uid, data in PAYMENT_REQUESTS.items():
        if data['status'] == 'pending':
            pkg = data['package']
            msg += f"• `{uid}` - {pkg['name']} (₹{pkg['price']})\n"

    await update.message.reply_text(msg, parse_mode=ParseModeConst.MARKDOWN)


async def broadcast_cmd(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast Your message",
            parse_mode=ParseModeConst.MARKDOWN
        )
        return

    message = " ".join(context.args)
    sent = 0

    conn = get_db()
    cursor = conn.cursor()

    for user in cursor.execute("SELECT id FROM users WHERE approved=1"):
        try:
            await context.bot.send_message(user['id'], message)
            sent += 1
        except:
            pass

    await update.message.reply_text(f"📢 **Sent to {sent} users**")


async def wipe_all(update, context):

    if update.effective_user.id != OWNER_CHAT_ID:
        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM users")
    cursor.execute("DELETE FROM logs")
    cursor.execute("DELETE FROM purchases")

    conn.commit()

    await update.message.reply_text("💥 NUCLEAR WIPE COMPLETE ⚠️")


#═══════════════════════════════════════════════════════ WEBHOOK ═══════════════════════════════════════════════════════

# ALL HANDLERS

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("godstats", god_stats))
telegram_app.add_handler(CommandHandler("pending", pending_payments))
telegram_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
telegram_app.add_handler(CommandHandler("wipeall", wipe_all))

telegram_app.add_handler(CallbackQueryHandler(button_handler))
telegram_app.add_handler(CallbackQueryHandler(buy_package_callback, pattern="^buy_"))
telegram_app.add_handler(CallbackQueryHandler(confirm_payment, pattern="^confirm_"))

telegram_app.add_handler(MessageHandler(filters.StatusUpdate.USER_SHARED, handle_user_share))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
telegram_app.add_handler(MessageHandler(filters.PHOTO, payment_proof_handler))


import asyncio

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(telegram_app.initialize())


@app.route("/")
def home():
    return "🚀 PREMIUM OSINT BOT v3.0 - LIVE 💎"


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():

    update = Update.de_json(
        request.get_json(force=True),
        telegram_app.bot
    )

    import asyncio
    asyncio.get_event_loop().create_task(
    telegram_app.process_update(update)
    )
    )

    return "OK"


#═══════════════════════════════════════════════════════ STARTUP ═══════════════════════════════════════════════════════

async def init_bot():

    await telegram_app.initialize()

    await telegram_app.bot.set_webhook(
        f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

    logger.info("🚀 Bot initialized & webhook set!")
    print("✅ Bot ready! Deployed on:", WEBHOOK_URL)


if __name__ == "main":

    # Validate ENV
    if not all([BOT_TOKEN, str(OWNER_CHAT_ID), WEBHOOK_URL]):
        print("❌ Missing ENV: BOT_TOKEN, OWNER_CHAT_ID, WEBHOOK_URL")
        exit(1)

    print("🔥 Starting Premium OSINT Bot v3.0...")

    asyncio.run(init_bot())

    # Start Flask
    port = int(os.getenv("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
        )
