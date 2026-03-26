import os
import time
import sqlite3
import asyncio
import httpx
import threading
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from contextlib import contextmanager
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestUser,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters
)
from telegram.constants import ParseMode as ParseModeConst
from telegram.error import TelegramError

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # kept for reference only

if not all([BOT_TOKEN, OWNER_CHAT_ID]):
    raise ValueError("❌ Missing required ENV vars: BOT_TOKEN, OWNER_CHAT_ID")

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"
SEARCH_COST = 5

PREMIUM_PACKAGES = [
    {"id": "basic", "name": "🟢 BASIC", "points": 50, "price": 500},
    {"id": "pro", "name": "🟡 PRO", "points": 250, "price": 2000},
    {"id": "elite", "name": "🔴 ELITE", "points": 1000, "price": 7000}
]

# Global payment tracking (thread-safe)
PAYMENT_REQUESTS = {}
PAYMENT_REQUESTS_LOCK = threading.Lock()

# New: Lightweight rate limiter for heavy load protection (max 5 searches / minute)
RATE_LIMIT = {}
RATE_LIMIT_LOCK = threading.Lock()

app = Flask(__name__)

# ====================== DATABASE ======================
@contextmanager
def get_db_connection():
    conn = sqlite3.connect("database.db", timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"DB error: {e}")
        raise
    finally:
        conn.close()

def create_tables():
    with get_db_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER DEFAULT 0,
            approved INTEGER DEFAULT 0,
            referrals INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_search REAL DEFAULT 0
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
        logger.info("✅ Database tables created/verified")

def ensure_user(uid, username=None, first_name=None):
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(id, username, first_name) VALUES(?,?,?)",
            (str(uid), username, first_name)
        )

def get_user(uid):
    ensure_user(uid)
    with get_db_connection() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (str(uid),)).fetchone()

def update_user_points(uid, points_change):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET points = MAX(0, points + ?) WHERE id=?",
            (points_change, str(uid))
        )

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ====================== VALIDATION & RATE LIMITING ======================
def validate_target(target: str) -> bool:
    target = target.strip()
    if target.isdigit():
        return 5 <= len(target) <= 15
    if target.startswith('@') and len(target) > 1:
        return 4 <= len(target) <= 32
    return False

def can_search(uid):
    user = get_user(uid)
    if not user or user['approved'] != 1:
        return False, "⛔ Not approved by admin"

    if user['points'] < SEARCH_COST:
        return False, f"💸 Need at least {SEARCH_COST} points"

    # 30-second cooldown
    if user['last_search'] and time.time() - user['last_search'] < 30:
        return False, "⏳ Wait 30 seconds between searches"

    # Better rate limiting: max 5 searches per minute (handles heavy load)
    with RATE_LIMIT_LOCK:
        now = time.time()
        if uid not in RATE_LIMIT:
            RATE_LIMIT[uid] = []
        # Clean old timestamps
        RATE_LIMIT[uid] = [t for t in RATE_LIMIT[uid] if now - t < 60]
        if len(RATE_LIMIT[uid]) >= 5:
            return False, "⏳ Max 5 searches per minute. Slow down!"
        # Consume slot only if all checks passed
        RATE_LIMIT[uid].append(now)

    return True, "OK"

# ====================== OSINT SEARCH ======================
async def search_target(target):
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                API_URL,
                params={"key": API_KEY, "id": target},
                headers={"User-Agent": "OSINT-Bot/3.1"}
            )
            response.raise_for_status()
            data = response.json()
            return data.get("result")
    except Exception as e:
        logger.error(f"API error for {target}: {e}")
        return None

async def perform_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    user_id = update.effective_user.id

    # Final safety check
    can, reason = can_search(user_id)
    if not can:
        await update.message.reply_text(reason)
        return

    # Update last search timestamp
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET last_search=? WHERE id=?", (time.time(), str(user_id)))

    loading_msg = await update.message.reply_text(
        "🔎 **GLOBAL DATABASE SCAN**\n\n```▰▰▰▱▱▱▱▱▱```\n*Max 30 seconds...*",
        parse_mode=ParseModeConst.MARKDOWN
    )

    result = await search_target(target)

    if not result:
        current_points = get_user(user_id)['points']
        await loading_msg.edit_text(
            f"❌ **NO RECORDS FOUND**\n\n💰 **Balance:** `{current_points}` points",
            parse_mode=ParseModeConst.MARKDOWN
        )
        return

    # Deduct points ONLY on success
    update_user_points(user_id, -SEARCH_COST)

    # Log search
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO logs(user_id,target,result,timestamp,points_spent) VALUES(?,?,?,?,?)",
            (str(user_id), target, str(result), time.strftime("%Y-%m-%d %H:%M:%S"), SEARCH_COST)
        )

    # Build result
    country_code = result.get('country_code', '')
    phone = result.get('number', 'N/A')
    country = result.get('country', 'Unknown')
    tg_id = result.get('tg_id', 'N/A')
    final_points = get_user(user_id)['points']

    result_text = f"""
🎯 **OSINT INTEL REPORT** 🎯

━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 TG ID: `{tg_id}`
📱 PHONE: {country_code} {phone}
🌍 COUNTRY: {country}
━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ **Balance:** `{final_points}` points
🔄 Ready for next search!
"""
    await loading_msg.edit_text(result_text, parse_mode=ParseModeConst.MARKDOWN)

# ====================== NEW: POINT HISTORY ======================
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = get_user(user.id)

    if not user_data or user_data['approved'] != 1:
        await update.message.reply_text("⛔ Only approved users can view history.")
        return

    uid = str(user.id)

    with get_db_connection() as conn:
        logs = conn.execute(
            "SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,)
        ).fetchall()
        purchases = conn.execute(
            "SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (uid,)
        ).fetchall()

    text = "📜 **YOUR POINT HISTORY** 📜\n\n"

    if purchases:
        text += "**Purchases:**\n"
        for p in purchases:
            text += f"• {p['package']} (+{p['points']}pts) - ₹{p['price']} | {p['timestamp'][:16]}\n"
        text += "\n"

    if logs:
        text += "**Recent Searches:**\n"
        for log in logs:
            text += f"• `{log['target']}` (-{log['points_spent']}pts) | {log['timestamp'][:16]}\n"
    else:
        text += "No searches yet."

    await update.message.reply_text(text, parse_mode=ParseModeConst.MARKDOWN)

# ====================== ADMIN COMMANDS ======================
async def add_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/addpoints <user_id> <points>`", parse_mode=ParseModeConst.MARKDOWN)
        return
    try:
        target_uid = str(context.args[0]).strip()
        points = int(context.args[1])
        if points <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Invalid user_id or points (must be positive integer)")
        return

    update_user_points(target_uid, points)
    new_balance = get_user(target_uid)['points']

    await update.message.reply_text(
        f"✅ **Added {points} points**\n👤 `{target_uid}`\n💰 New Balance: `{new_balance}`",
        parse_mode=ParseModeConst.MARKDOWN
    )
    try:
        await context.bot.send_message(
            target_uid,
            f"🎉 **Admin added {points} points!**\n💰 Balance: `{new_balance}`",
            parse_mode=ParseModeConst.MARKDOWN
        )
    except:
        pass


async def remove_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/removepoints <user_id> <points>`", parse_mode=ParseModeConst.MARKDOWN)
        return
    try:
        target_uid = str(context.args[0]).strip()
        points = int(context.args[1])
        if points <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Invalid user_id or points")
        return

    update_user_points(target_uid, -points)
    new_balance = get_user(target_uid)['points']

    await update.message.reply_text(
        f"✅ **Removed {points} points**\n👤 `{target_uid}`\n💰 New Balance: `{new_balance}`",
        parse_mode=ParseModeConst.MARKDOWN
    )
    try:
        await context.bot.send_message(
            target_uid,
            f"⚠️ **Admin removed {points} points.**\n💰 Balance: `{new_balance}`",
            parse_mode=ParseModeConst.MARKDOWN
        )
    except:
        pass

# ====================== ORIGINAL HANDLERS (FULL - NO REMOVAL) ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        try:
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
                OWNER_CHAT_ID, owner_msg,
                parse_mode=ParseModeConst.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            logger.error(f"Failed to notify owner: {e}")
        return

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


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "request_access":
        await query.edit_message_text("✅ **Request sent to admin!**", parse_mode=ParseModeConst.MARKDOWN)
        return

    if user_id != OWNER_CHAT_ID:
        return

    try:
        if data.startswith("approve_"):
            uid = data.split("_")[1]
            with get_db_connection() as conn:
                conn.execute("UPDATE users SET approved=1 WHERE id=?", (uid,))
            await query.edit_message_text(f"✅ **APPROVED {uid}** ✅")
        elif data.startswith("deny_"):
            uid = data.split("_")[1]
            await query.edit_message_text(f"❌ **DENIED {uid}** ❌")
    except Exception as e:
        logger.error(f"Admin button handler error: {e}")
        await query.edit_message_text("❌ Action failed")


async def buy_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for pkg in PREMIUM_PACKAGES:
        keyboard.append([
            InlineKeyboardButton(
                f"{pkg['name']} - ₹{pkg['price']} ({pkg['points']}pts)",
                callback_data=f"buy_{pkg['id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "💎 CHOOSE PACKAGE:",
        reply_markup=markup,
        parse_mode=ParseModeConst.MARKDOWN
    )


async def buy_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pkg_id = query.data.split("_")[1]
    user_id = update.effective_user.id

    package = next((p for p in PREMIUM_PACKAGES if p['id'] == pkg_id), None)
    if not package:
        await query.answer("❌ Invalid package")
        return

    with PAYMENT_REQUESTS_LOCK:
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


async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with PAYMENT_REQUESTS_LOCK:
        if user_id not in PAYMENT_REQUESTS:
            return

    try:
        proof = update.message.photo[-1].file_id if update.message.photo else None
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

        if proof:
            await context.bot.send_photo(
                OWNER_CHAT_ID,
                proof,
                caption=forward_text,
                parse_mode=ParseModeConst.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await context.bot.send_message(
                OWNER_CHAT_ID,
                forward_text,
                parse_mode=ParseModeConst.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        await update.message.reply_text("✅ **Sent to admin!** ⏳")
    except Exception as e:
        logger.error(f"Payment proof handler error: {e}")


async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id_str = query.data.split("_")[1]
    user_id = int(user_id_str)

    with PAYMENT_REQUESTS_LOCK:
        if user_id not in PAYMENT_REQUESTS:
            await query.answer("❌ No pending payment")
            return
        package = PAYMENT_REQUESTS[user_id]['package']
        del PAYMENT_REQUESTS[user_id]

    try:
        update_user_points(user_id, package['points'])

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO purchases(user_id,package,points,price,timestamp) VALUES(?,?,?,?,?)",
                (str(user_id), package['name'], package['points'], package['price'], time.strftime("%Y-%m-%d %H:%M:%S"))
            )

        await context.bot.send_message(
            user_id,
            f"🎉 **PAYMENT APPROVED!** 🎉\n\n"
            f"💎 **+{package['points']} Points**\n"
            f"💰 **Balance:** `{get_user(user_id)['points']}`\n\n"
            f"🚀 **Start searching!**",
            parse_mode=ParseModeConst.MARKDOWN
        )

        await query.edit_message_text(
            f"✅ **{user_id} APPROVED** ✅\n💎 {package['points']}pts added!"
        )
    except Exception as e:
        logger.error(f"Confirm payment error: {e}")
        await query.answer("❌ Approval failed")


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    stats = f"""
📊 YOUR STATS 📊

👤 {user['first_name']}
💰 Points: {user['points']}
⚡ Searches left: {user['points']//SEARCH_COST}
📅 Joined: {user['created_at'][:10] if user['created_at'] else 'N/A'}

🔥 Ready to hunt!
"""
    await update.message.reply_text(stats, parse_mode=ParseModeConst.MARKDOWN)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "💰 BUY POINTS":
        await buy_points(update, context)
        return
    if text == "📊 STATS":
        await show_stats(update, context)
        return
    if text in ["👥 REFER", "🆘 SUPPORT"]:
        await update.message.reply_text("Feature coming soon! Contact admin for support.")
        return

    if not validate_target(text):
        await update.message.reply_text("❌ Invalid format. Send Telegram ID or @username")
        return

    can, reason = can_search(user_id)
    if not can:
        await update.message.reply_text(reason)
        return

    await perform_lookup(update, context, text)


async def handle_user_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.user_shared:
        target = str(update.message.user_shared.user_id)
        can, reason = can_search(update.effective_user.id)
        if not can:
            await update.message.reply_text(reason)
            return
        await perform_lookup(update, context, target)


# ====================== OWNER GOD MODE ======================
async def god_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            stats = {
                'total_users': cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                'premium_users': cursor.execute("SELECT COUNT(*) FROM users WHERE approved=1").fetchone()[0],
                'total_points': cursor.execute("SELECT SUM(points) FROM users").fetchone()[0] or 0,
                'today_searches': cursor.execute("SELECT COUNT(*) FROM logs WHERE date(timestamp)=date('now')").fetchone()[0],
            }

        with PAYMENT_REQUESTS_LOCK:
            pending = len([v for v in PAYMENT_REQUESTS.values() if v.get('status') == 'pending'])

        dashboard = f"""
👑 GOD MODE v3.1 👑

👥 Total Users: {stats['total_users']}
💎 Premium: {stats['premium_users']}
💰 Points Value: ₹{stats['total_points']//10:,}
🔍 Today Searches: {stats['today_searches']}
⏳ Pending Payments: {pending}

👇 Commands:
/pending - Payment queue
/broadcast MSG - Mass send
/wipeall - NUKE DATABASE ⚠️
/addpoints <id> <pts>
/removepoints <id> <pts>
/history
"""
        await update.message.reply_text(dashboard, parse_mode=ParseModeConst.MARKDOWN)
    except Exception as e:
        logger.error(f"God stats error: {e}")


async def pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    with PAYMENT_REQUESTS_LOCK:
        if not PAYMENT_REQUESTS:
            await update.message.reply_text("✅ **No pending payments**")
            return
        msg = "⏳ **PENDING PAYMENTS:**\n\n"
        count = 0
        for uid, data in PAYMENT_REQUESTS.items():
            if data.get('status') == 'pending':
                pkg = data['package']
                msg += f"• `{uid}` - {pkg['name']} (₹{pkg['price']})\n"
                count += 1
        if count == 0:
            msg = "✅ **No pending payments**"
        else:
            msg += f"\nTotal: {count}"
    await update.message.reply_text(msg, parse_mode=ParseModeConst.MARKDOWN)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message")
        return
    message = " ".join(context.args)
    sent, failed = 0, 0
    try:
        with get_db_connection() as conn:
            users = conn.execute("SELECT id FROM users WHERE approved=1").fetchall()
        for user_row in users:
            try:
                await context.bot.send_message(user_row['id'], message)
                sent += 1
                await asyncio.sleep(0.1)
            except:
                failed += 1
        await update.message.reply_text(f"📢 **Broadcast complete**\n✅ Sent: {sent}\n❌ Failed: {failed}")
    except Exception as e:
        logger.error(f"Broadcast error: {e}")


async def wipe_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM logs")
            conn.execute("DELETE FROM purchases")
        with PAYMENT_REQUESTS_LOCK:
            PAYMENT_REQUESTS.clear()
        await update.message.reply_text("💥 **NUCLEAR WIPE COMPLETE** ⚠️\nDatabase reset!")
    except Exception as e:
        logger.error(f"Wipe error: {e}")


# ====================== HANDLER SETUP ======================
def setup_handlers(application):
    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("godstats", god_stats))
    application.add_handler(CommandHandler("pending", pending_payments))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(CommandHandler("wipeall", wipe_all))
    application.add_handler(CommandHandler("addpoints", add_points))
    application.add_handler(CommandHandler("removepoints", remove_points))
    application.add_handler(CommandHandler("history", show_history))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(buy_package_callback, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(confirm_payment, pattern="^confirm_"))
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(approve_|deny_|request_access|cancel|reject_)"))

    # Message handlers
    application.add_handler(MessageHandler(filters.PHOTO, payment_proof_handler))
    application.add_handler(MessageHandler(filters.StatusUpdate.USER_SHARED, handle_user_share))
    
    # IMPORTANT: This is the fixed line
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


# ====================== FLASK HEALTH (Render free tier) ======================
@app.route("/")
def home():
    return jsonify({
        "status": "🚀 PREMIUM OSINT BOT v3.1 - LIVE (Polling Mode)",
        "memory_optimized": "True (512MB Render free)",
        "uptime": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


# ====================== ASYNC BOT RUN (FULLY FIXED) ======================
telegram_app = None

async def run_bot():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    setup_handlers(telegram_app)

    await telegram_app.initialize()
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Old webhook deleted - now using polling only")
    await telegram_app.start()

    me = await telegram_app.bot.get_me()
    logger.info(f"🤖 Bot @{me.username} started successfully (Polling)")

    await telegram_app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


# ====================== STARTUP (Optimized for Render 512MB) ======================
def run_flask():
    """Run Flask health check in background thread"""
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    print("🔥 Starting Premium OSINT Bot v3.1 (Render Free Optimized)...")

    create_tables()

    # Start Flask health check in background (required for Render web dyno)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Run Telegram bot with pure async polling (no webhook, no event loop conflicts)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("👋 Bot shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
