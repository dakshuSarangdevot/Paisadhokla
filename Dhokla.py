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
from telegram.error import TelegramError, BadRequest

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance INTEGER DEFAULT 0,
        join_date TEXT
    )
    """)

    conn.commit()
    conn.close()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ENV VARS - Validate on startup
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not all([BOT_TOKEN, OWNER_CHAT_ID, WEBHOOK_URL]):
    raise ValueError("❌ Missing required ENV vars: BOT_TOKEN, OWNER_CHAT_ID, WEBHOOK_URL")

OWNER_CHAT_ID = int(OWNER_CHAT_ID)

API_URL = "https://ayaanmods.site/tg2num.php"
API_KEY = "annonymoustgtonum"
SEARCH_COST = 5

# Premium Packages
PREMIUM_PACKAGES = [
    {"id": "basic", "name": "🟢 BASIC", "points": 50, "price": 500},
    {"id": "pro", "name": "🟡 PRO", "points": 250, "price": 2000},
    {"id": "elite", "name": "🔴 ELITE", "points": 1000, "price": 7000}
]

# Global payment tracking (thread-safe)
PAYMENT_REQUESTS = {}
PAYMENT_REQUESTS_LOCK = threading.Lock()

app = Flask(__name__)

init_db()
telegram_app = None

# Thread-safe Database with context manager
@contextmanager
def get_db_connection():
    conn = sqlite3.connect("database.db", check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def create_tables():
    """Initialize database tables"""
    with get_db_connection() as conn:
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
        logger.info("✅ Database tables created/verified")

#═══════════════════════════════════════════════════════ USER SYSTEM ═══════════════════════════════════════════════════════

def ensure_user(uid, username=None, first_name=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users(id, username, first_name) VALUES(?,?,?)",
            (str(uid), username, first_name)
        )

def get_user(uid):
    ensure_user(uid)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id=?", (str(uid),))
        return cursor.fetchone()

def update_user_points(uid, points_change):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET points = MAX(0, points + ?) WHERE id=?",
            (points_change, str(uid))
        )

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
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET approved=1 WHERE id=?", (str(uid),))

def validate_target(target: str) -> bool:
    """Validate search target"""
    target = target.strip()
    if target.isdigit():
        return 5 <= len(target) <= 15
    if target.startswith('@') and len(target) > 1:
        return 4 <= len(target) <= 32
    return False

#═══════════════════════════════════════════════════════ OSINT ENGINE ═══════════════════════════════════════════════════════

async def search_target(target):
    try:
        async with httpx.AsyncClient(timeout=15.0, limits=httpx.Limits(max_keepalive_connections=5)) as client:
            response = await client.get(
                API_URL,
                params={"key": API_KEY, "id": target},
                headers={
                    "User-Agent": "OSINT-Bot/3.0",
                    "Accept": "application/json"
                }
            )
            response.raise_for_status()
            data = response.json()
            return data.get("result") if "result" in data else None
    except httpx.RequestError as e:
        logger.error(f"API request failed for {target}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error searching {target}: {e}")
        return None

async def perform_lookup(update, context, target):
    user_id = update.effective_user.id

    try:
        # Update last search
        with get_db_connection() as conn:
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
            user_points = get_user(user_id)['points']
            await loading_msg.edit_text(
                "❌ **NO RECORDS FOUND**\n\n"
                f"💰 **Balance:** `{user_points}`\n"
                "👆 Try another ID",
                parse_mode=ParseModeConst.MARKDOWN
            )
            return

        # Log search
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO logs(user_id,target,result,timestamp,points_spent) VALUES(?,?,?,?,?)",
                (str(user_id), target, str(result), time.strftime("%Y-%m-%d %H:%M:%S"), SEARCH_COST)
            )

        # Premium result
        country_code = result.get('country_code', '')
        phone = result.get('number', 'N/A')
        country = result.get('country', 'Unknown')
        tg_id = result.get('tg_id', 'N/A')
        
        result_text = f"""
🎯 OSINT INTEL REPORT 🎯

━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 TG ID: `{tg_id}`
📱 PHONE: {country_code} {phone}
🌍 COUNTRY: {country}
━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ Balance: `{get_user(user_id)['points']}` points
🔄 Next search ready!
"""
        await loading_msg.edit_text(result_text, parse_mode=ParseModeConst.MARKDOWN)

    except TelegramError as e:
        logger.error(f"Telegram error in perform_lookup: {e}")
        try:
            await update.message.reply_text("❌ Search failed. Try again.")
        except:
            pass
    except Exception as e:
        logger.error(f"Unexpected error in perform_lookup: {e}")
        try:
            await update.message.reply_text("❌ System error. Contact support.")
        except:
            pass

#═══════════════════════════════════════════════════════ MAIN HANDLERS ═══════════════════════════════════════════════════════

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

        # Notify owner
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
                OWNER_CHAT_ID,
                owner_msg,
                parse_mode=ParseModeConst.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            logger.error(f"Failed to notify owner: {e}")
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

    # Admin actions
    try:
        if data.startswith("approve_"):
            uid = data.split("_")[1]
            approve_user(uid)
            await query.edit_message_text(f"✅ **APPROVED {uid}** ✅")

        elif data.startswith("deny_"):
            uid = data.split("_")[1]
            await query.edit_message_text(f"❌ **DENIED {uid}** ❌")
    except Exception as e:
        logger.error(f"Admin button handler error: {e}")
        await query.edit_message_text("❌ Action failed")

#═══════════════════════════════════════════════════════ PAYMENT SYSTEM ═══════════════════════════════════════════════════════

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
        proof = (
            update.message.photo[-1].file_id
            if update.message.photo
            else None
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

        # Log purchase
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO purchases(user_id,package,points,price,timestamp) VALUES(?,?,?,?,?)",
                (
                    str(user_id),
                    package['name'],
                    package['points'],
                    package['price'],
                    time.strftime("%Y-%m-%d %H:%M:%S")
                )
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
            f"✅ **{user_id} APPROVED** ✅\n"
            f"💎 {package['points']}pts added!"
        )
    except Exception as e:
        logger.error(f"Confirm payment error: {e}")
        await query.answer("❌ Approval failed")

#═══════════════════════════════════════════════════════ USER FEATURES ═══════════════════════════════════════════════════════

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

    # Search validation
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

#═══════════════════════════════════════════════════════ OWNER GOD MODE ═══════════════════════════════════════════════════════

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
            pending_payments = len([uid for uid, data in PAYMENT_REQUESTS.items() if data['status'] == 'pending'])

        dashboard = f"""
👑 GOD MODE v3.0 👑

👥 Total Users: {stats['total_users']}
💎 Premium: {stats['premium_users']}
💰 Points Value: ₹{stats['total_points']//10:,}
🔍 Today Searches: {stats['today_searches']}
⏳ Pending Payments: {pending_payments}

👇 Commands:
/pending - Payment queue
/broadcast MSG - Mass send
/wipeall - NUKE DATABASE ⚠️
"""
        await update.message.reply_text(dashboard, parse_mode=ParseModeConst.MARKDOWN)
    except Exception as e:
        logger.error(f"God stats error: {e}")
        await update.message.reply_text("❌ Stats error")

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
            if data['status'] == 'pending':
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
        await update.message.reply_text("Usage: /broadcast Your message", parse_mode=ParseModeConst.MARKDOWN)
        return

    message = " ".join(context.args)
    sent = 0
    failed = 0

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            users = cursor.execute("SELECT id FROM users WHERE approved=1").fetchall()

        for user_row in users:
            try:
                await context.bot.send_message(user_row['id'], message)
                sent += 1
                await asyncio.sleep(0.05)  # Rate limit
            except:
                failed += 1

        await update.message.reply_text(f"📢 **Broadcast complete**\n✅ Sent: {sent}\n❌ Failed: {failed}")
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        await update.message.reply_text("❌ Broadcast failed")

async def wipe_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users")
            cursor.execute("DELETE FROM logs")
            cursor.execute("DELETE FROM purchases")
        
        # Clear payment requests
        with PAYMENT_REQUESTS_LOCK:
            PAYMENT_REQUESTS.clear()

        await update.message.reply_text("💥 **NUCLEAR WIPE COMPLETE** ⚠️\nDatabase reset!")
    except Exception as e:
        logger.error(f"Wipe error: {e}")
        await update.message.reply_text("❌ Wipe failed")

#═══════════════════════════════════════════════════════ WEBHOOK & FLASK ROUTES ═══════════════════════════════════════════════════════

# Initialize handlers
def setup_handlers():
    # COMMAND HANDLERS
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("godstats", god_stats))
    telegram_app.add_handler(CommandHandler("pending", pending_payments))
    telegram_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    telegram_app.add_handler(CommandHandler("wipeall", wipe_all))

    # CALLBACK QUERY HANDLERS (specific → general)
    telegram_app.add_handler(CallbackQueryHandler(buy_package_callback, pattern="^buy_"))
    telegram_app.add_handler(CallbackQueryHandler(confirm_payment, pattern="^confirm_"))
    telegram_app.add_handler(CallbackQueryHandler(button_handler, pattern="^(approve_|deny_|request_access|cancel|reject_)"))

    # MESSAGE HANDLERS (specific → general)
    telegram_app.add_handler(MessageHandler(filters.PHOTO, payment_proof_handler))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.USER_SHARED, handle_user_share))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

@app.route("/")
def home():
    return jsonify({
        "status": "🚀 PREMIUM OSINT BOT v3.0 - LIVE 💎",
        "webhook": f"{WEBHOOK_URL}/{BOT_TOKEN}",
        "uptime": time.strftime("%Y-%m-%d %H:%M:%S")
    })

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Main webhook endpoint - thread-safe"""

    global telegram_app

    try:
        # Initialize bot if not started (needed for Gunicorn)
        if telegram_app is None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def lazy_init():
                global telegram_app
                telegram_app = Application.builder().token(BOT_TOKEN).build()
                setup_handlers()
                await telegram_app.initialize()

            loop.run_until_complete(lazy_init())

        json_data = request.get_json(force=True)
        if not json_data:
            return "Invalid JSON", 400

        update = Update.de_json(json_data, telegram_app.bot)

        if update:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(telegram_app.process_update(update))
            loop.close()

        return "OK"

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "ERROR", 500

#═══════════════════════════════════════════════════════ STARTUP ═══════════════════════════════════════════════════════

async def init_bot():
    """Initialize bot and set webhook"""
    global telegram_app
    
    try:
        # Create application
        init_db()
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        setup_handlers()
        
        # Initialize
        await telegram_app.initialize()
        
        # Test bot
        me = await telegram_app.bot.get_me()
        logger.info(f"🤖 Bot initialized: @{me.username}")
        
        # Set webhook
        webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await telegram_app.bot.set_webhook(webhook_url)
        
        # Verify webhook
        webhook_info = await telegram_app.bot.get_webhook_info()
        if webhook_info.url == webhook_url:
            logger.info(f"✅ Webhook set: {webhook_url}")
            print("✅ Bot ready! Deployed on:", WEBHOOK_URL)
        else:
            logger.error(f"❌ Webhook failed: {webhook_info.url}")
            
    except Exception as e:
        logger.error(f"Bot init failed: {e}")
        raise

if __name__ == "__main__":
    # Startup sequence
    print("🔥 Starting Premium OSINT Bot v3.0...")
    
    # Initialize database
    create_tables()
    
    # Initialize bot (async)
    try:
        asyncio.run(init_bot())
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        exit(0)
    except Exception as e:
        logger.error(f"Fatal startup error: {e}")
        print(f"❌ Failed to start: {e}")
        exit(1)
    
    # Start Flask server
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    
    print(f"🌐 Flask server starting on port {port}...")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug,
        threaded=True
        )
