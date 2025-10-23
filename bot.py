import os
import json
import asyncio
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote_plus

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaDocument,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden, RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
# ============================================================
# 🔐 Configuration & Security
# ============================================================

# ✅ Read the bot token securely from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable not set. Please define it before running the bot.")

# ✅ Admin usernames (only these can access admin commands)
ADMINS = ["ktb_2", "GlobalAds_admin"]

# ✅ Path to SQLite database
DB_PATH = Path("bot.db")

application = ApplicationBuilder().token(BOT_TOKEN).build()
# ============================================================
# ⚙️ Global Variables
# ============================================================

# Stores the configured signal post id
SIGNAL_POST_ID = None

# Conversation states for new post creation
NP_MAIN, NP_INTRO, NP_TITLE, NP_DESC, NP_CHANNELS = range(5)

# ============================================================
# 🧠 Logging Configuration
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

logger.info("✅ Configuration loaded successfully.")

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        caption TEXT NOT NULL,
        channels TEXT
    )
    """)
    # settings table for persistent small values (admin id, signal id, ...)
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    # new: users table to track bot members
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    # Load persisted signal post ID
    c.execute("SELECT value FROM settings WHERE key = 'signal_post_id'")
    row = c.fetchone()
    global SIGNAL_POST_ID
    if row:
        SIGNAL_POST_ID = row[0]
    conn.close()

init_db()

# settings helpers (persistent small key/value storage)
def get_setting(key, default=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def add_user_to_db(user):
    """Record or update a user in the users table (id + username)."""
    if not user:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        username = getattr(user, "username", "") or ""
        # insert if missing
        cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, username))
        # keep username fresh
        cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user.id))
        conn.commit()
    except Exception:
        logger.exception("Failed to add/update user in DB")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def get_user_count():
    """Return number of distinct users recorded."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        logger.exception("Failed to fetch user count")
        return 0

def get_post_count():
    """Return number of posts recorded."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM posts")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        logger.exception("Failed to fetch post count")
        return 0

async def stats_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats command for admins - shows simple bot stats."""
    if not (update.effective_user and update.effective_user.username in ADMINS):
        try:
            await update.message.reply_text("❌ ✨ Unauthorized. ✨")
        except Exception:
            pass
        return
    users = get_user_count()
    posts = get_post_count()
    signal = SIGNAL_POST_ID or "ندارد"
    try:
        await update.message.reply_text(
            f"📊 آمار ربات:\n\n👥 تعداد اعضا: {users}\n📝 تعداد پست‌ها: {posts}\n⚡️ سیگنال رایگان: {signal}"
        )
    except Exception:
        pass
# 🆕 ثبت سیگنال جدید (توسط ادمین‌ها)
async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"newpost_start triggered by user={update.effective_user.id}")
    try:
        origin_text = update.message.text if update.message and update.message.text else None
        if origin_text:
            context.user_data["post_origin"] = origin_text
    except Exception:
        pass

    await update.message.reply_text(
        "✨ لطفاً فایل اصلی را ارسال کنید (فایل، عکس یا متن)\nبرای لغو از /cancel استفاده کنید. ✨"
    )
    return NP_MAIN


async def newpost_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive main file/text"""
    msg = update.message
    logger.info(f"newpost_main from {update.effective_user.id}")
    if msg.document:
        context.user_data["main_file"] = {"file_id": msg.document.file_id, "type": "document"}
    elif msg.photo:
        context.user_data["main_file"] = {"file_id": msg.photo[-1].file_id, "type": "photo"}
    elif msg.text:
        context.user_data["main_file"] = {"text": msg.text, "type": "text"}

    await update.message.reply_text(
        "✨ حالا فایل معرفی را ارسال کنید (فایل، عکس یا متن)\nاین فایل قبل از دریافت فایل اصلی نمایش داده می‌شود. ✨"
    )
    return NP_INTRO


async def newpost_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    logger.info(f"newpost_intro from {update.effective_user.id}")
    if msg.document:
        context.user_data["intro_file"] = {"file_id": msg.document.file_id, "type": "document"}
    elif msg.photo:
        context.user_data["intro_file"] = {"file_id": msg.photo[-1].file_id, "type": "photo"}
    elif msg.text:
        context.user_data["intro_file"] = {"text": msg.text, "type": "text"}

    await update.message.reply_text("✨ عنوان پست را وارد کنید: ✨")
    return NP_TITLE


async def newpost_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["title"] = update.message.text
    await update.message.reply_text("✨ توضیحات پست را وارد کنید: ✨")
    return NP_DESC


async def newpost_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["description"] = update.message.text
    await update.message.reply_text(
        "✨ آیدی کانال‌های اجباری را وارد کنید (هر کدام در یک خط)\nاگر کانال اجباری ندارید، None بنویسید: ✨"
    )
    return NP_CHANNELS


async def newpost_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parsed = parse_channels_text(text) if text.lower() != "none" else []
    context.user_data["channels"] = json.dumps(parsed, ensure_ascii=False)

    # Save post
    post_id = save_post_db(context.user_data)
    await update.message.reply_text("✅ ✨ پست با موفقیت ذخیره شد. ✨")

    bot_user = await context.bot.get_me()
    bot_username = getattr(bot_user, "username", None) or ""
    deep_link = f"https://t.me/{bot_username}?start=get_{post_id}" if bot_username else f"https://t.me/{post_id}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Receive", url=deep_link)]])
    caption = f"📌 {context.user_data.get('title','Untitled')}\n<a href=\"{deep_link}\">📥 Receive</a>"

    intro = context.user_data.get("intro_file", {})
    try:
        if intro:
            if intro.get("type") == "photo" and intro.get("file_id"):
                await update.message.reply_photo(photo=intro["file_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
            elif intro.get("type") == "document" and intro.get("file_id"):
                await update.message.reply_document(document=intro["file_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
            elif intro.get("type") == "text" and intro.get("text"):
                await update.message.reply_text(intro.get("text"), reply_markup=kb, parse_mode="HTML")
            else:
                await update.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")

    origin = context.user_data.get("post_origin")
    if origin == "📣 سیگنال رایگان":
        try:
            await update.message.reply_text(f"✨ پیش‌نمایش پست {post_id} برای سیگنال رایگان نمایش داده شد. ✨")
        except Exception:
            pass

    context.user_data.clear()
    return ConversationHandler.END




def save_post_db(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    caption = json.dumps({
        "title": data["title"],
        "description": data["description"],
        "main_file": data["main_file"],
        "intro_file": data["intro_file"]
    })
    c.execute("INSERT INTO posts (caption, channels) VALUES (?, ?)", (caption, data["channels"]))
    post_id = c.lastrowid
    conn.commit()
    conn.close()
    return post_id
async def send_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        with open(r"C:\Users\ESHGH ZENDEGI\Desktop\Bestfreesignalbot\intro.txt", "rb") as f:
            await context.bot.send_document(chat_id, f)
    except Exception as e:
        await update.message.reply_text(f"✨ Error sending intro file: {str(e)} ✨")
def get_post_db(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT caption, channels FROM posts WHERE id = ?", (post_id,))
    row = c.fetchone()
    conn.close()
    if row:
        caption, channels = row
        return {"caption": caption, "channels": channels}
    return None

def delete_post_db(post_id):
    global SIGNAL_POST_ID
    # جلوگیری از حذف سیگنال فعال 
    if SIGNAL_POST_ID and str(post_id) == str(SIGNAL_POST_ID):
        # فقط اجازه حذف اگر سیگنال جدید ثبت شده باشد (یعنی SIGNAL_POST_ID تغییر کند)
        return False
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return True

def force_delete_post_db(post_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    return True

async def check_join_status(user_id, channels, context: ContextTypes.DEFAULT_TYPE):
    not_joined = []
    for channel in channels:
        channel = channel.strip()
        if not channel:
            continue
        try:
            # protect each API call with a short timeout to avoid hanging
            # ensure channel is in username form with @ prefix for get_chat_member
            channel_param = channel if channel.startswith("@") else f"@{channel}"
            try:
                member = await asyncio.wait_for(context.bot.get_chat_member(channel_param, user_id), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout while checking membership for {channel} and user {user_id}")
                not_joined.append(channel)
                continue

            if member.status not in ["creator", "administrator", "member"]:
                not_joined.append(channel)
        except Exception:
            logger.exception(f"Error checking membership for channel {channel}")
            not_joined.append(channel)
    return not_joined


def parse_channels_text(channels_text: str):
    """Parse user input where each line contains display name and channel address.
    Accepts lines like:
      My Channel | @mychannel
      Another Channel | https://t.me/mychannel
      SimpleName @mychannel
    Returns list of dicts: [{'name':..., 'username':...}, ...]
    """
    out = []
    if not channels_text:
        return out
    import re
    for raw in channels_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # if user wrote 'None' treat as no channels
        if line.lower() == 'none':
            return []

        # try to find @username or t.me/username or https://t.me/username
        m = re.search(r"@([A-Za-z0-9_]+)", line)
        username = None
        if not m:
            m2 = re.search(r"t\.me/([A-Za-z0-9_]+)", line)
            if m2:
                username = m2.group(1)
        else:
            username = m.group(1)

        if username:
            # remove the username part and separators to get the display name
            name = re.sub(r"(@[A-Za-z0-9_]+|https?://t\.me/[A-Za-z0-9_]+|t\.me/[A-Za-z0-9_]+)", "", line)
            # also remove common separators
            name = name.replace('|', ' ').replace('-', ' ').replace(':', ' ').replace(',', ' ').strip()
            if not name:
                name = username
            out.append({"name": name, "username": username})
            continue

        # if no username found, try splitting by '|'
        if '|' in line:
            parts = [p.strip() for p in line.split('|', 1)]
            if len(parts) == 2:
                name, addr = parts
                # extract possible username from addr
                m3 = re.search(r"([A-Za-z0-9_]+)$", addr)
                username = m3.group(1) if m3 else addr
                out.append({"name": name or username, "username": username})
                continue

        # fallback: treat the whole line as username (and name)
        uname = line.lstrip('@').strip()
        out.append({"name": uname, "username": uname})

    return out



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ثبت کاربر و نمایش منوی اصلی"""
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None

    # ثبت کاربر در دیتابیس
    try:
        if user:
            add_user_to_db(user)
    except Exception:
        pass

    # پیام خوش‌آمد برای کاربر
    welcome_text = (
        "👋 Hi!\n"
        "Welcome to the Free Signals bot.\n"
        "Please choose an option from the menu below 👇"
    )
# ✨ ارسال پیام خوش‌آمد
    await context.bot.send_message(chat_id=update.effective_chat.id, text=welcome_text)




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش پیام خوش‌آمد در استارت اصلی و حذف آن هنگام دریافت فایل"""
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None

    # ثبت کاربر در دیتابیس
    try:
        if user:
            add_user_to_db(user)
    except Exception:
        pass

    # بررسی آرگومان start
    args = context.args

    # اگر کاربر با لینک get_ وارد شده → یعنی می‌خواهد فایل بگیرد
    if args and args[0].startswith("get_"):
        post_id = args[0].split("get_")[1]
        post = get_post_db(post_id)
        if not post:
            await update.message.reply_text("❌ File not found.")
            return

        try:
            cap = json.loads(post["caption"])
        except Exception:
            cap = {"title": "بدون عنوان", "description": "", "main_file": {}, "intro_file": {}}

        channels_text = post.get("channels", "")
        try:
            channels_parsed = json.loads(channels_text) if channels_text else []
        except Exception:
            channels_parsed = []
            for c in (channels_text or "").splitlines():
                c = c.strip()
                if c:
                    channels_parsed.append({"name": c, "username": c.lstrip('@')})

        usernames_for_check = [item.get("username", "").lstrip('@') for item in channels_parsed if item.get("username")]
        not_joined = await check_join_status(update.effective_user.id, usernames_for_check, context) if usernames_for_check else []
        remaining_channels = [item for item in channels_parsed if item['username'].lstrip('@') in not_joined]

        if remaining_channels:
            caption_intro = f"📌 {cap.get('title','بدون عنوان')}\n✨ Please join the channels below first ✨"
            channel_buttons = [[InlineKeyboardButton(item['name'], url=f"https://t.me/{item['username']}")] for item in remaining_channels]
            membership_button = [InlineKeyboardButton("✅ Check membership", callback_data=f"continue_get_{post_id}")]
            kb = InlineKeyboardMarkup(channel_buttons + [membership_button])
            intro = cap.get("intro_file", {})
            if intro.get("file_id") and intro.get("type") == "photo":
                try:
                    await update.message.reply_photo(photo=intro["file_id"], caption=caption_intro, reply_markup=kb)
                except Exception:
                    await update.message.reply_text(caption_intro, reply_markup=kb)
            else:
                txt = intro.get("text") or caption_intro
                await update.message.reply_text(txt, reply_markup=kb)
            return

        # اگر در همه کانال‌ها عضو بود → فایل را بفرست
        main_file = cap.get("main_file", {})
        title = cap.get("title", "بدون عنوان")
        description = cap.get("description", "") or "بدون توضیحات"
        caption_info = f"📌 عنوان: {title}\n\n📝 توضیحات:\n{description}"
        chat_id = update.effective_chat.id

        if main_file.get("type") == "photo" and main_file.get("file_id"):
            await context.bot.send_photo(chat_id=chat_id, photo=main_file["file_id"], caption=caption_info)
        elif main_file.get("type") == "document" and main_file.get("file_id"):
            await context.bot.send_document(chat_id=chat_id, document=main_file["file_id"], caption=caption_info)
        elif main_file.get("type") == "text" and main_file.get("text"):
            combined = f"📌 {title}\n\n📄 فایل اصلی:\n{main_file['text']}\n\n📝 توضیحات:\n{description}"
            await context.bot.send_message(chat_id=chat_id, text=combined)
        else:
            await context.bot.send_message(chat_id=chat_id, text=caption_info)
        return

    # 👋 اگر کاربر از دکمه‌ی اصلی استارت وارد شده (بدون لینک get_)
    welcome_text = (
        "👋 Hi!\n"
        "Welcome to the Free Signals bot 🌟\n\n"
        "Please choose an option from the menu below 👇"
    )

    # نمایش منوی اصلی
    if user and user.username in ADMINS:
        kb = ReplyKeyboardMarkup(
            [
                ["🆕 پست جدید", "📚 پست ها"],
                ["📢 تبلیغات", "⚙️ تنظیم سیگنال رایگان"],
                ["📊 آمار ربات", "📤 ارسال به همه"]
            ],
            resize_keyboard=True
        )
    else:
        kb = ReplyKeyboardMarkup(
            [
                ["📈 Free Signal", "📱 Popular Posts"],
                ["🆓 Free Ads", "👥 Order Real Members"],
                ["🤖 Buy Bot", "💬 Contact Support"]
            ],
            resize_keyboard=True
        )

    await context.bot.send_message(chat_id=chat_id, text=welcome_text, reply_markup=kb)




async def continue_get_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = query.data.split("continue_get_")[1]
    post = get_post_db(post_id)
    if not post:
        try:
            await query.edit_message_text("❌ File not found!")
        except Exception:
            pass
        return

    try:
        cap = json.loads(post["caption"])
    except Exception:
        cap = {"title": "Untitled", "description": "", "main_file": {}, "intro_file": {}}

    channels_text = post.get("channels", "") or ""
    # parse stored channels (supports JSON or simple lines)
    channels_parsed = []
    try:
        channels_parsed = json.loads(channels_text) if channels_text else []
    except Exception:
        for ln in channels_text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            channels_parsed.append({"name": ln, "username": ln.lstrip('@')})

    usernames_for_check = [item.get("username", "").lstrip('@') for item in channels_parsed if item.get("username")]
    not_joined = await check_join_status(query.from_user.id, usernames_for_check, context) if usernames_for_check else []

    if not_joined:
        # User is missing membership in some channels -> inform and show buttons
        remaining_channels = [item for item in channels_parsed if item.get("username", "").lstrip('@') in not_joined]
        caption_new = f"📌 {cap.get('title','Untitled')}\n\n❌ You are not a member of all required channels yet."
        channel_buttons = [[InlineKeyboardButton(item.get('name') or item.get('username'), url=f"https://t.me/{item.get('username')}")] for item in remaining_channels]
        membership_button = [InlineKeyboardButton("✅ Check membership", callback_data=f"continue_get_{post_id}")]
        kb = InlineKeyboardMarkup(channel_buttons + [membership_button])

        intro = cap.get("intro_file", {})
        try:
            # prefer editing existing message if possible
            if query.message and intro.get("file_id") and intro.get("type") == "photo":
                from telegram import InputMediaPhoto
                await query.message.edit_media(media=InputMediaPhoto(media=intro["file_id"], caption=caption_new))
                await query.message.edit_reply_markup(reply_markup=kb)
            else:
                try:
                    await query.edit_message_text(caption_new, reply_markup=kb)
                except Exception:
                    await query.message.reply_text(caption_new, reply_markup=kb)
        except Exception:
            try:
                await query.message.reply_text(caption_new, reply_markup=kb)
            except Exception:
                pass
        return

    # All required channels joined -> send main file first, then title and description
    main_file = cap.get("main_file", {})
    title = cap.get("title", "بدون عنوان")
    description = cap.get("description", "") or "بدون توضیحات"
    
    try:
        chat_id = query.from_user.id

        # If main file is text -> send single message with title then main text then description
        if main_file.get("type") == "text" and main_file.get("text"):
            parts = [f"📌 عنوان: {title}", f"📄 فایل اصلی:\n{main_file['text']}"]
            if description:
                parts.append(f"📝 توضیحات:\n{description}")
            # join with two newlines for readability
            combined = "\n\n".join(parts)
            await context.bot.send_message(chat_id=chat_id, text=combined)
        else:
            # First send the main file regardless of type (photo/document)
            if main_file.get("type") == "photo" and main_file.get("file_id"):
                await context.bot.send_photo(chat_id=chat_id, photo=main_file["file_id"], caption=f"📌 عنوان: {title}\n\n📝 توضیحات:\n{description}")
            elif main_file.get("type") == "document" and main_file.get("file_id"):
                await context.bot.send_document(chat_id=chat_id, document=main_file["file_id"], caption=f"📌 عنوان: {title}\n\n📝 توضیحات:\n{description}")
            elif main_file.get("type") == "text" and main_file.get("text"):
                # fallback (shouldn't reach because handled above)
                await context.bot.send_message(chat_id=chat_id, text=f"📄 فایل اصلی:\n{main_file['text']}")
                await asyncio.sleep(0.5)
                await context.bot.send_message(chat_id=chat_id, text=f"📌 عنوان: {title}\n\n📝 توضیحات:\n{description}")
            else:
                # اگر هیچ فایلی نبود، فقط عنوان و توضیحات را می‌فرستیم
                await context.bot.send_message(chat_id=chat_id, text=f"📌 عنوان: {title}\n\n📝 توضیحات:\n{description}")

        try:
            await query.edit_message_text("✅ ✨ شما در تمامی کانال‌ها عضو هستید. فایل ارسال شد. ✨")
        except Exception:
            pass
            
    except Exception as e:
        logger.exception("Error sending main file")
        try:
            await context.bot.send_message(chat_id=query.from_user.id, text=f"❌ خطا در ارسال فایل:\n{str(e)}")
        except Exception:
            pass
    return


async def receive_get_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    post_id = query.data.split("receive_get_")[1]
    post = get_post_db(post_id)
    if not post:
        try:
            await query.edit_message_text("❌ File not found!")
        except Exception:
            pass
        return

    # Try to delete the message that had the photo + title + button
    try:
        await query.message.delete()
    except Exception:
        logger.exception("Could not delete preview message")

    try:
        cap = json.loads(post["caption"])
    except Exception:
        cap = {"title": "Untitled", "description": "", "main_file": {}, "intro_file": {}}
    channels_text = post.get("channels", "")
    try:
        channels_parsed = json.loads(channels_text) if channels_text else []
    except Exception:
        channels_parsed = []
        for c in (channels_text or "").splitlines():
            c = c.strip()
            if c:
                channels_parsed.append({"name": c, "username": c.lstrip('@')})

    # build deep link to this post (will survive forwarding)
    bot_user = await context.bot.get_me()
    bot_username = getattr(bot_user, "username", "") or ""
    deep_link = f"https://t.me/{bot_username}?start=get_{post_id}" if bot_username else f"https://t.me/{post_id}"

    # Present intro with channel buttons (user will press Check membership to remove joined channels)
    title = cap.get("title", "Untitled")
    caption_intro = f"📌 {title}\n{deep_link}\nPlease join the channels below first"
    channel_buttons = [[InlineKeyboardButton(item['name'], url=f"https://t.me/{item['username']}")] for item in channels_parsed]
    membership_button = [InlineKeyboardButton("✅ Check membership", callback_data=f"continue_get_{post_id}")]
    kb = InlineKeyboardMarkup(channel_buttons + [membership_button])

    intro = cap.get("intro_file", {}) or {}
    main = cap.get("main_file", {}) or {}

    # If intro is a photo file -> keep existing behavior (photo + caption_intro)
    if intro.get("file_id") and intro.get("type") == "photo":
        try:
            await context.bot.send_photo(chat_id=query.from_user.id, photo=intro["file_id"], caption=caption_intro, reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=query.from_user.id, text=caption_intro, reply_markup=kb)
        return

    # If intro is text -> show title + intro text + deep link (and membership buttons)
    if intro.get("type") == "text" and intro.get("text"):
        txt = f"📌 {title}\n\n{intro.get('text')}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
        try:
            await context.bot.send_message(chat_id=query.from_user.id, text=txt, reply_markup=kb, parse_mode="HTML")
        except Exception:
            try:
                await context.bot.send_message(chat_id=query.from_user.id, text=f"📌 {title}\n\n{intro.get('text')}\n\n{deep_link}", reply_markup=kb)
            except Exception:
                pass
        return

    # NEW: If no intro text but main file is text -> show title + main text + deep link in the same post
    if main.get("type") == "text" and main.get("text"):
        txt = f"📌 {title}\n\n{main.get('text')}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
        try:
            await context.bot.send_message(chat_id=query.from_user.id, text=txt, reply_markup=kb, parse_mode="HTML")
        except Exception:
            try:
                await context.bot.send_message(chat_id=query.from_user.id, text=f"📌 {title}\n\n{main.get('text')}\n\n{deep_link}", reply_markup=kb)
            except Exception:
                pass
        return

    # Fallback: existing behavior (use intro file/document if present, otherwise text caption_intro)
    if intro.get("file_id"):
        try:
            if intro.get("type") == "photo":
                await context.bot.send_photo(chat_id=query.from_user.id, photo=intro["file_id"], caption=caption_intro, reply_markup=kb)
            else:
                await context.bot.send_document(chat_id=query.from_user.id, document=intro["file_id"], caption=caption_intro, reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=query.from_user.id, text=caption_intro, reply_markup=kb)
    else:
        txt = intro.get("text") or caption_intro
        await context.bot.send_message(chat_id=query.from_user.id, text=txt, reply_markup=kb)

# New post conversation handlers
async def newpost_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
	logger.info(f"newpost_start triggered by user={update.effective_user.id}")
	# record origin (if started from a keyboard button like "📣 سیگنال رایگان")
	try:
		origin_text = update.message.text if update.message and update.message.text else None
		if origin_text:
			context.user_data["post_origin"] = origin_text
	except Exception:
		pass

	await update.message.reply_text(
		"✨ لطفاً فایل اصلی را ارسال کنید (فایل، عکس یا متن)\nبرای لغو از /cancel استفاده کنید. ✨"
	)
	return NP_MAIN

async def newpost_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive main file/text"""
    msg = update.message
    logger.info(f"newpost_main received message from {update.effective_user.id}: has_document={bool(msg.document)} has_photo={bool(msg.photo)} has_text={bool(msg.text)}")
    if msg.document:
        context.user_data["main_file"] = {
            "file_id": msg.document.file_id,
            "type": "document"
        }
    elif msg.photo:
        context.user_data["main_file"] = {
            "file_id": msg.photo[-1].file_id,
            "type": "photo"
        }
    elif msg.text:
        context.user_data["main_file"] = {
            "text": msg.text,
            "type": "text"
        }
    
    await update.message.reply_text(
        "✨ حالا فایل معرفی را ارسال کنید (فایل، عکس یا متن)\nاین فایل قبل از دریافت فایل اصلی نمایش داده می‌شود. ✨"
    )
    return NP_INTRO

async def newpost_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive intro file/text"""
    msg = update.message
    logger.info(f"newpost_intro received message from {update.effective_user.id}: has_document={bool(msg.document)} has_photo={bool(msg.photo)} has_text={bool(msg.text)}")
    if msg.document:
        context.user_data["intro_file"] = {
            "file_id": msg.document.file_id,
            "type": "document"
        }
    elif msg.photo:
        context.user_data["intro_file"] = {
            "file_id": msg.photo[-1].file_id,
            "type": "photo"
        }
    elif msg.text:
        context.user_data["intro_file"] = {
            "text": msg.text,
            "type": "text"
        }
    
    await update.message.reply_text("✨ عنوان پست را وارد کنید: ✨")
    return NP_TITLE

async def newpost_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive post title"""
    logger.info(f"newpost_title from {update.effective_user.id}: text={update.message.text}")
    context.user_data["title"] = update.message.text
    await update.message.reply_text("✨ توضیحات پست را وارد کنید: ✨")
    return NP_DESC

async def newpost_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive post description"""
    logger.info(f"newpost_desc from {update.effective_user.id}: text_len={len(update.message.text) if update.message.text else 0}")
    context.user_data["description"] = update.message.text
    await update.message.reply_text("✨ آیدی کانال‌های اجباری را وارد کنید (هر کدام در یک خط)\nاگر کانال اجباری ندارید، None بنویسید: ✨")
    return NP_CHANNELS

async def newpost_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
	"""Receive required channels and save post"""
	logger.info(f"newpost_channels triggered by {update.effective_user.id}")
	text = update.message.text.strip()
	# Parse channels entered by admin: each line can contain display name and address
	parsed = parse_channels_text(text) if text.lower() != "none" else []
	# store channels as JSON string in DB for later use
	context.user_data["channels"] = json.dumps(parsed, ensure_ascii=False)

	# Save post to database
	post_id = save_post_db(context.user_data)
	await update.message.reply_text("✅ ✨ پست با موفقیت ذخیره شد. ✨")
	
	# build deep link to bot: https://t.me/<bot_username>?start=get_<post_id>
	bot_user = await context.bot.get_me()
	bot_username = getattr(bot_user, "username", None) or ""
	deep_link = f"https://t.me/{bot_username}?start=get_{post_id}" if bot_username else f"https://t.me/{post_id}"
	# شکلی از دکمه شیشه‌ای (inline) برای دریافت فایل
	kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Receive", url=deep_link)]])
	# caption لینک پنهان (HTML)
	title = context.user_data.get('title','Untitled')
	# default caption for media/text messages (title + hidden link)
	caption = f"📌 {title}\n<a href=\"{deep_link}\">📥 Receive</a>"

	# NEW: همیشه فایل معرفی را نمایش بده؛ اگر فایل معرفی متنی بود، متن را بعد از عنوان در همان پیام قرار بده
	intro = context.user_data.get("intro_file", {})

	try:
		if intro:
			# Photo intro
			if intro.get("type") == "photo" and intro.get("file_id"):
				await update.message.reply_photo(photo=intro["file_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
			# Document intro
			elif intro.get("type") == "document" and intro.get("file_id"):
				await update.message.reply_document(document=intro["file_id"], caption=caption, reply_markup=kb, parse_mode="HTML")
			# Text intro -> include title, intro text, then hidden link (all in one message)
			elif intro.get("type") == "text" and intro.get("text"):
				full_text = f"📌 {title}\n\n{intro.get('text')}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
				await update.message.reply_text(full_text, reply_markup=kb, parse_mode="HTML")
			else:
				# fallback: send caption text with glass button
				await update.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")
		else:
			# no intro provided -> still show title + hidden link + button
			await update.message.reply_text(caption, reply_markup=kb, parse_mode="HTML")
	except Exception:
		# final fallback: safe text reply
		try:
			await update.message.reply_text(f"📌 {title}\n📥 {deep_link}", reply_markup=kb)
		except Exception:
			pass

	# If conversation was started via "📣 سیگنال رایگان", keep origin info and explicitly show preview (already above),
	# you may extend behavior here (e.g., post to a channel) if needed in future.
	origin = context.user_data.get("post_origin")
	if origin == "📣 سیگنال رایگان":
		# optionally send a small confirmation / preview marker for free-signal flow (keeps admin aware)
		try:
			await update.message.reply_text(f"✨ پیش‌نمایش پست {post_id} برای سیگنال رایگان نمایش داده شد. ✨")
		except Exception:
			pass

	# clear editing creation data
	context.user_data.clear()
	return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ ✨ عملیات لغو شد. ✨")
    context.user_data.clear()
    return ConversationHandler.END

# Admin commands
async def list_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username not in ADMINS:
        await update.message.reply_text("❌ ✨ Unauthorized. ✨")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, caption FROM posts ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("✨ No posts found. ✨")
        return
    msg = "🌟 📚 Posts list: 📚 🌟\n"
    for row in rows:
        post_id = row[0]
        try:
            cap = json.loads(row[1])
            title = cap.get("title", "Untitled")
        except Exception:
            title = "Untitled"
        msg += f"ID: {post_id} - {title}\n"
    # Remove the three button keyboard and simply send the message
    await update.message.reply_text(msg)

# ⚙️ انتخاب پست از لیست برای تنظیم سیگنال رایگان
async def set_signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        post_id = int(query.data.replace("set_signal_", ""))
    except Exception:
        await query.edit_message_text("❌ شناسه پست نامعتبر است.")
        return

    post = get_post_db(post_id)
    if not post:
        await query.edit_message_text("❌ پست مورد نظر یافت نشد.")
        return

    # ذخیره در تنظیمات
    set_setting("signal_post_id", post_id)
    global SIGNAL_POST_ID
    SIGNAL_POST_ID = post_id

    await query.edit_message_text(f"✅ پست شماره {post_id} به عنوان سیگنال فعال تنظیم شد.")


# ⚙️ انتخاب پست به عنوان سیگنال رایگان
async def set_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets a selected post as the active free signal."""
    if update.effective_user.username not in ADMINS:
        await update.message.reply_text("❌ فقط ادمین می‌تواند سیگنال تنظیم کند.")
        return

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ لطفاً فقط عدد ID پست را ارسال کنید.")
        return

    post_id = int(text)
    post = get_post_db(post_id)
    if not post:
        await update.message.reply_text("❌ پست یافت نشد.")
        return

    # ذخیره در جدول settings
    set_setting("signal_post_id", post_id)
    global SIGNAL_POST_ID
    SIGNAL_POST_ID = post_id

    await update.message.reply_text(f"✅ سیگنال رایگان با شناسه {post_id} تنظیم شد.")


async def delete_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username not in ADMINS:
        await update.message.reply_text("❌ ✨ Unauthorized. ✨")
        return
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("✨ Usage: /deletepost <id> ✨")
        return
    try:
        post_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("✨ Invalid post id. ✨")
        return
    
    # جلوگیری از حذف سیگنال فعال
    if SIGNAL_POST_ID and str(post_id) == str(SIGNAL_POST_ID):
        await update.message.reply_text("❌ این پست به عنوان سیگنال رایگان قفل شده و قابل حذف نیست. ابتدا سیگنال جدید ثبت کنید.")
        return
        
    result = delete_post_db(post_id)
    if not result:
        await update.message.reply_text("❌ این پست به عنوان سیگنال رایگان قفل شده و قابل حذف نیست. ابتدا سیگنال جدید ثبت کنید.")
        return
    await update.message.reply_text(f"✨ Post {post_id} deleted. ✨")

async def order_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    support = get_setting("support_id", None)
    chat_id = update.effective_chat.id if update.effective_chat else (
        update.callback_query.message.chat_id if getattr(update, "callback_query", None) and update.callback_query.message else None
    )
    if not support:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="❌ Support ID is not configured.")
        return
    admin_username = support if support.startswith("@") else f"@{support}"
    url = f"https://t.me/{admin_username.lstrip('@')}?text=I%20want%20to%20order%20real%20members."
    keyboard = [
        [InlineKeyboardButton("Order Real Members", url=url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text="To order real members, click the button below 👇", reply_markup=reply_markup)

async def free_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    support = get_setting("support_id", None)
    chat_id = update.effective_chat.id if update.effective_chat else (
        update.callback_query.message.chat_id if getattr(update, "callback_query", None) and update.callback_query.message else None
    )
    if not support:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="❌ Support ID is not configured.")
        return
    admin_username = support if support.startswith("@") else f"@{support}"
    url = f"https://t.me/{admin_username.lstrip('@')}?text=Hello,%20I%20want%20to%20submit%20a%20free%20ad."
    keyboard = [
        [InlineKeyboardButton("Submit Free Ad", url=url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text="To submit a free ad, click the button below 👇", reply_markup=reply_markup)

async def contact_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    support = get_setting("support_id", None)
    chat_id = update.effective_chat.id if update.effective_chat else (
        update.callback_query.message.chat_id if getattr(update, "callback_query", None) and update.callback_query.message else None
    )
    if not support:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="❌ Support ID is not configured.")
        return
    admin_username = support if support.startswith("@") else f"@{support}"
    url = f"https://t.me/{admin_username.lstrip('@')}?text=Hello,%20I%20need%20support."
    keyboard = [
        [InlineKeyboardButton("Contact Support", url=url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text="To contact support, click the button below 👇", reply_markup=reply_markup)

async def buy_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    support = get_setting("support_id", None)
    chat_id = update.effective_chat.id if update.effective_chat else (
        update.callback_query.message.chat_id if getattr(update, "callback_query", None) and update.callback_query.message else None
    )
    if not support:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="❌ Support ID is not configured.")
        return
    admin_username = support if support.startswith("@") else f"@{support}"
    url_this = f"https://t.me/{admin_username.lstrip('@')}?text=Hello,%20I%20want%20to%20buy%20this%20bot."
    url_other = f"https://t.me/{admin_username.lstrip('@')}?text=Hello,%20I%20want%20to%20buy%20another%20bot."
    keyboard = [
        [InlineKeyboardButton("Buy this bot", url=url_this)],
        [InlineKeyboardButton("Buy another bot", url=url_other)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text="To buy a bot, choose an option below 👇", reply_markup=reply_markup)

# ...existing code...

async def menu_callback(update, context):
    query = update.callback_query
    if query:
        await query.answer()
    data = query.data if query else (update.message.text if update.message else "")

    # Ensure chat_id exists immediately (keep this early)
    chat_id = None
    try:
        if update and getattr(update, "effective_chat", None):
            chat_id = update.effective_chat.id
    except Exception:
        chat_id = None
    if chat_id is None and query and query.message:
        chat_id = query.message.chat_id

    # NEW: handle inline selection of a signal post (callback_data "signal_post_<id>")
    if isinstance(data, str) and data.startswith("signal_post_"):
        try:
            post_id = int(data.split("signal_post_")[1])
        except Exception:
            try:
                if query and query.message:
                    await query.edit_message_text("❌ شناسه پست نامعتبر است.")
            except Exception:
                pass
            return

        # persist selection
        try:
            set_setting("signal_post_id", post_id)
            global SIGNAL_POST_ID
            SIGNAL_POST_ID = post_id
        except Exception:
            logger.exception("Failed to persist chosen signal post")

        # reply success (prefer editing the inline message)
        try:
            if query and query.message:
                await query.edit_message_text(f"✅ پست شماره {post_id} به عنوان سیگنال فعال تنظیم شد.")
            elif chat_id:
                await context.bot.send_message(chat_id=chat_id, text=f"✅ پست شماره {post_id} به عنوان سیگنال فعال تنظیم شد.")
        except Exception:
            pass
        return

    if not query and isinstance(data, str):
        txt = data.strip()

        # تنظیم سیگنال رایگان (map main menu button to admin submenu)
        if txt in ("⚙️ تنظیم سیگنال رایگان", "تنظیم سیگنال رایگان", "Set Free Signal", "Free Signal"):
            data = "admin_signal_menu"

        # پذیرش چند واریانت برای دکمه "دیدن سیگنال"
        elif txt in (
            "دیدن سیگنال",
            "👁 دیدن سیگنال",
            "👁️ دیدن سیگنال",
            "👁️️ دیدن سیگنال",
            "View Signal",
            "See Signal"
        ):
            data = "دیدن سیگنال"

        # برگشت به منوی قبلی
        elif txt in ("🔙 برگشت", "برگشت"):
            prev = context.user_data.get("prev_menu")
            if prev == "posts_menu":
                data = "show_posts_menu"
            elif prev == "signal_menu":
                data = "admin_signal_menu"
            else:
                data = "back_to_main"

        # آمار ربات (support both with and without emoji)
        elif txt in ("آمار ربات", "📊 آمار ربات"):
            await stats_bot(update, context)
            return

        # ارسال به همه (support both with and without emoji)
        elif txt in ("ارسال به همه", "📤 ارسال به همه"):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو ارسال به همه", callback_data="cancel_broadcast")]
            ])
            await update.message.reply_text(
                "📨 لطفاً پیامی که می‌خواهید برای همه اعضا ارسال شود را بفرستید.",
                reply_markup=kb
            )
            context.user_data["awaiting_broadcast_text"] = True
            return

        # posts submenu mappings
        elif txt in ("📚 پست ها", "پست ها"):
            data = "show_posts_menu"
            context.user_data["prev_menu"] = "main_menu"

        # ads submenu mappings
        elif txt in ("📢 تبلیغات", "تبلیغات"):
            data = "admin_ads_menu"
            context.user_data["prev_menu"] = "main_menu"

        elif txt in ("ℹ️ اطلاعات و ویرایش", "اطلاعات و ویرایش"):
            data = "admin_listposts"
            context.user_data["prev_menu"] = "posts_menu"
        elif txt in ("📤 پست های ارسالی", "پست های ارسالی"):
            data = "admin_post_sent"
            context.user_data["prev_menu"] = "posts_menu"
        # new: ads submenu mappings
        elif txt in ("تبلیغات",):
            data = "admin_ads_menu"
            context.user_data["prev_menu"] = "main_menu"
        elif txt in ("آیدی ادمین", "آیدی ادمین تنظیم تبلیغات"):
            data = "ads_admin_id"
            context.user_data["prev_menu"] = context.user_data.get("prev_menu", "main_menu")
        elif txt in ("تنظیم تبلیغات", "تنظیم تبلیغات دکمه"):
            data = "ads_set"
            context.user_data["prev_menu"] = "ads_menu"
        elif txt in ("سفارش ممبر واقعی", "👥 سفارش ممبر واقعی", "👥 Order Real Members", "Order Real Members"):
            await order_member(update, context)
            return
        elif txt in ("تبلیغات رایگان", "🆓 تبلیغات رایگان", "🆓 Free Ads", "Free Ads"):
            await free_ads(update, context)
            return
        elif txt in ("صحبت با پشتیبان", "💬 صحبت با پشتیبان", "Contact Support", "💬 Contact Support"):
            await contact_support(update, context)
            return
    # 🛒 دکمه خرید ربات — پشتیبانی از چند نوع نوشته
        elif txt in (
            "خرید این ربات یا ربات دیگر",
            "🤖 خرید این ربات یا ربات دیگر",
            "خرید ربات",
            "🤖 خرید ربات",
            "Buy this bot",
            "Buy Bot",
            "🤖 Buy Bot"
        ):
            await buy_bot(update, context)
            return

        # keep "ثبت سیگنال" and "دیدن سیگنال" as-is (they are checked directly later)

    # Ensure chat_id exists immediately
    chat_id = None
    try:
        if update and getattr(update, "effective_chat", None):
            chat_id = update.effective_chat.id
    except Exception:
        chat_id = None
    if chat_id is None and query and query.message:
        chat_id = query.message.chat_id


    # NEW: explicit handler for "پست های ارسالی" (admin) - active for both callback and plain message
    if data == "admin_post_sent" or (update.message and update.message.text == "📤 پست های ارسالی"):
        # only admins
        user = update.effective_user if update else None
        if not user or getattr(user, "username", None) not in ADMINS:
            try:
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text="❌ فقط ادمین می‌تواند از این منو استفاده کند.")
            except Exception:
                pass
            return

        try:
            # get bot username for deep links
            bot_user = await context.bot.get_me()
            bot_username = getattr(bot_user, "username", "") or ""

            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, caption FROM posts ORDER BY id DESC")
            rows = cur.fetchall()
            conn.close()

            if not rows:
                try:
                    await context.bot.send_message(chat_id=chat_id, text="✨ هیچ پستی یافت نشد.")
                except Exception:
                    pass
                return

            for row in rows:
                post_id = row[0]
                try:
                    cap = json.loads(row[1]) if row[1] else {}
                except Exception:
                    cap = {"title": "بدون عنوان", "intro_file": {}, "main_file": {}}

                title = cap.get("title", "بدون عنوان")
                intro = cap.get("intro_file", {}) or {}
                main = cap.get("main_file", {}) or {}

                # ساخت لینک دریافت فایل + دکمه شیشه‌ای
                deep_link = f"https://t.me/{bot_username}?start=get_{post_id}" if bot_username else f"https://t.me/{post_id}"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Receive", url=deep_link)]])
                caption_html = f"📌 {title}\n\n<a href=\"{deep_link}\">📥 Receive</a>"

                try:
                    # اگر intro وجود دارد و عکس/فایل است -> ارسال با کپشن شامل عنوان+لینک
                    if intro.get("file_id"):
                        if intro.get("type") == "photo":
                            await context.bot.send_photo(chat_id=chat_id, photo=intro["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                        else:
                            await context.bot.send_document(chat_id=chat_id, document=intro["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                    # اگر intro از نوع متن بود -> یک پیام شامل عنوان + متن معرفی + لینک پنهان
                    elif intro.get("type") == "text" and intro.get("text"):
                        full_text = f"📌 {title}\n\n{intro.get('text')}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
                        await context.bot.send_message(chat_id=chat_id, text=full_text, reply_markup=kb, parse_mode="HTML")
                    # اگر intro وجود ندارد ولی فایل اصلی هست -> از فایل اصلی استفاده کن (با کپشن)
                    elif main.get("file_id"):
                        if main.get("type") == "photo":
                            await context.bot.send_photo(chat_id=chat_id, photo=main["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                        else:
                            await context.bot.send_document(chat_id=chat_id, document=main["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                    # در غیر این صورت فقط عنوان + لینک را به‌صورت متن ارسال کن
                    else:
                        await context.bot.send_message(chat_id=chat_id, text=caption_html, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    logger.exception(f"Error sending post {post_id}")
                    continue

        except Exception as e:
            logger.exception("Error in admin_post_sent handler")
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ خطا در نمایش پست‌ها: {str(e)}")
            except Exception:
                pass
        return

       # NEW: Ads admin submenu and actions
    if data == "admin_ads_menu" or (
        update.message and update.message.text in ("📢 تبلیغات", "تبلیغات")
    ):
        user = update.effective_user if update else None
        if not user or getattr(user, "username", None) not in ADMINS:
            try:
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text="❌ فقط ادمین می‌تواند از این منو استفاده کند.")
            except Exception:
                pass
            return
        kb = ReplyKeyboardMarkup(
            [
                ["👤 آیدی پشتیبان", "⚙️ تنظیم تبلیغات"],
                ["🔙 برگشت"]
            ],
            resize_keyboard=True
        )
        context.user_data["prev_menu"] = "ads_menu"
        try:
            await context.bot.send_message(chat_id=chat_id, text="📢 منوی تبلیغات: یکی از گزینه‌ها را انتخاب کنید.", reply_markup=kb)
        except Exception:
            pass
        return

    if data in ("آیدی پشتیبان", "👤 آیدی پشتیبان"):
        kb = ReplyKeyboardMarkup(
            [
                ["✏️ تنظیم آیدی پشتیبان", "👁️ دیدن آیدی پشتیبان"],
                ["🔙 برگشت"]
            ],
            resize_keyboard=True
        )
        context.user_data["prev_menu"] = "support_menu"
        await context.bot.send_message(chat_id=chat_id, text="👤 مدیریت آیدی پشتیبان:", reply_markup=kb)
        return

    # Handle setting new support admin ID
    elif data in ("تنظیم آیدی پشتیبان", "✏️ تنظیم آیدی پشتیبان"):
        try:
            user = update.effective_user if update else None
            if not user or getattr(user, "username", None) not in ADMINS:
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text="❌ فقط ادمین می‌تواند آیدی پشتیبان را تنظیم کند.")
                return
            context.user_data["awaiting_support_id"] = True

            # add an inline "cancel" button so admin can cancel without sending text
            kb_inline = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ لغو تنظیم آیدی پشتیبان", callback_data="cancel_support_id")]
            ])

            await context.bot.send_message(chat_id=chat_id, text="✍️ لطفاً آیدی پشتیبان (مثلاً @username) را ارسال کنید یا برای لغو، دکمه ❌ را بزنید.", reply_markup=kb_inline)
        except Exception:
            pass
        return

    # Handle canceling support ID setting
    elif data in ("❌ لغو تنظیم آیدی پشتیبان", "لغو تنظیم آیدی پشتیبان"):
        await cancel_support_id(update, context)
        return

    # Handle showing current support admin ID
    elif data in ("دیدن آیدی پشتیبان", "👁️ دیدن آیدی پشتیبان"):
        try:
            support = get_setting("support_id", None)
            if not support:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ هنوز آیدی پشتیبان تنظیم نشده است.")
            else:
                display = f"@{support}" if not support.startswith("@") and not support.isdigit() else support
                await context.bot.send_message(chat_id=chat_id, text=f"🔹 آیدی پشتیبان فعلی: {display}")
        except Exception:
            pass
        return


    # If admin just sent support id (we use awaiting_support_id flag), save it

    if update.message and context.user_data.get("awaiting_support_id") and update.message.text:
        try:
            user = update.effective_user if update else None
            if not user or getattr(user, "username", None) not in ADMINS:
                return
            val = update.message.text.strip()
            stored = val.lstrip('@')
            set_setting("support_id", stored)
            context.user_data.pop("awaiting_support_id", None)
            await context.bot.send_message(chat_id=chat_id, text=f"✅ آیدی پشتیبان ذخیره شد: @{stored}")
        except Exception:
            logger.exception("Failed to save support id")
        return

    # ✅ اگر ادمین متنی فرستاد و در حالت ارسال به همه است
    if update.message and context.user_data.get("awaiting_broadcast_text"):
        text = update.message.text
        context.user_data.pop("awaiting_broadcast_text", None)
        context.user_data["broadcast_message"] = update.message

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ بله، ارسال کن", callback_data="broadcast_confirm"),
             InlineKeyboardButton("❌ خیر، لغو شود", callback_data="broadcast_cancel")]
        ])
        await update.message.reply_text(
            f"آیا مطمئن هستید که این پیام برای همه ارسال شود؟\n\n{text}",
            reply_markup=kb
        )
        return


    # Admin: show signal settings submenu (ثبت سیگنال / دیدن سیگنال / برگشت)
    # Admin: show posts submenu (پست های ارسالی / اطلاعات و ویرایش / برگشت)
    if data == "show_posts_menu":
        kb = ReplyKeyboardMarkup(
            [
                ["📤 پست های ارسالی", "ℹ️ اطلاعات و ویرایش"],
                ["🔙 برگشت"]
            ],
            resize_keyboard=True
        )
        context.user_data["prev_menu"] = "main_menu"
        try:
            await context.bot.send_message(chat_id=chat_id, text="📚 منوی پست‌ها: یکی از گزینه‌ها را انتخاب کنید.", reply_markup=kb)
        except Exception:
            pass
        return

    if data == "admin_signal_menu":
        kb = ReplyKeyboardMarkup(
            [
                ["📝 ثبت سیگنال", "👁️ دیدن سیگنال"],  # use emojis here
                ["🔙 برگشت"]
            ],
            resize_keyboard=True
        )
        context.user_data["prev_menu"] = "main_menu"
        try:
            await context.bot.send_message(chat_id=chat_id, text="⚙️ تنظیم سیگنال رایگان: یکی از گزینه‌ها را انتخاب کنید.", reply_markup=kb)
        except Exception:
            pass
        return

    if data == "back_to_main":
        # return the full admin main keyboard (same as in /start for admins)
        kb = ReplyKeyboardMarkup(
            [
                ["🆕 پست جدید", "📚 پست ها"],
                ["📢 تبلیغات", "⚙️ تنظیم سیگنال رایگان"],
                ["📊 آمار ربات", "📤 ارسال به همه"]
            ],
            resize_keyboard=True
        )
        context.user_data.pop("prev_menu", None) # پاک کردن منوی قبلی
        await context.bot.send_message(chat_id=chat_id, text="✨ 📋 منوی اصلی: ✨", reply_markup=kb)
        return

    # Admin: start registering a signal (expects admin to forward a message containing get_<id>)
    if data in ("ثبت سیگنال", "📝 ثبت سیگنال"):
        try:
            user = update.effective_user
            if not user or getattr(user, "username", None) not in ADMINS:
                await context.bot.send_message(chat_id=chat_id, text="❌ فقط ادمین می‌تواند سیگنال را ثبت کند.")
                return

            # fetch recent posts from DB
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT id, caption FROM posts ORDER BY id DESC LIMIT 50")
                rows = cur.fetchall()
                conn.close()
            except Exception:
                rows = []

            if not rows:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ هیچ پستی یافت نشد تا به عنوان سیگنال انتخاب شود.")
                return

            # build inline keyboard: one button per post with title — #id
            kb_rows = []
            for r in rows:
                pid = r[0]
                try:
                    meta = json.loads(r[1]) if r[1] else {}
                    title = (meta.get("title") or "").strip() or "بدون عنوان"
                except Exception:
                    title = "بدون عنوان"
                label = f"{title} — #{pid}"
                kb_rows.append([InlineKeyboardButton(label, callback_data=f"signal_post_{pid}")])

            # add a cancel button row
            kb_rows.append([InlineKeyboardButton("❌ انصراف", callback_data=f"cancel_signal_0")])
            kb = InlineKeyboardMarkup(kb_rows)
            await context.bot.send_message(chat_id=chat_id, text="📌 برای ثبت سیگنال، یکی از پست‌ها را انتخاب کنید:", reply_markup=kb)
        except Exception:
            logger.exception("Error showing posts for signal registration")
        return

    # Admin: show current signal
    if data == "دیدن سیگنال":
        try:
            if not SIGNAL_POST_ID:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ هنوز سیگنال رایگانی تنظیم نشده است.")
                return

            # load signal post from DB
            post = get_post_db(SIGNAL_POST_ID)
            if not post:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ پست سیگنال پیدا نشد.")
                return

            try:
                cap = json.loads(post.get("caption") or "{}")
            except Exception:
                cap = {}

            title = cap.get("title", "بدون عنوان")
            desc = cap.get("description", "") or ""
            intro = cap.get("intro_file", {})

            # build caption text to show: include post id, title and description
            if desc:
                send_caption = f"📌 سیگنال رایگان — #{SIGNAL_POST_ID}\n\n<b>{title}</b>\n\n{desc}"
            else:
                send_caption = f"📌 سیگنال رایگان — #{SIGNAL_POST_ID}\n\n<b>{title}</b>"

            # prefer sending intro file (photo/document/text), fallback to main file, else plain text
            try:
                if intro.get("file_id"):
                    if intro.get("type") == "photo":
                        await context.bot.send_photo(chat_id=chat_id, photo=intro["file_id"], caption=send_caption, parse_mode="HTML")
                    else:
                        await context.bot.send_document(chat_id=chat_id, document=intro["file_id"], caption=send_caption, parse_mode="HTML")
                elif intro.get("text"):
                    await context.bot.send_message(chat_id=chat_id, text=f"{send_caption}\n\n{intro.get('text')}", parse_mode="HTML")
                else:
                    # no intro -> try main file
                    if main.get("file_id"):
                        if main.get("type") == "photo":
                            await context.bot.send_photo(chat_id=chat_id, photo=main["file_id"], caption=send_caption, parse_mode="HTML")
                        else:
                            await context.bot.send_document(chat_id=chat_id, document=main["file_id"], caption=send_caption, parse_mode="HTML")
                    else:
                        await context.bot.send_message(chat_id=chat_id, text=send_caption, parse_mode="HTML")
            except Exception:
                # final fallback: send plain text
                try:
                    await context.bot.send_message(chat_id=chat_id, text=send_caption)
                except Exception:
                    pass

        except Exception:
            logger.exception("Error while handling 'دیدن سیگنال'")
        return

    # Handle "سیگنال رایگان" button
    if (data and data == "📈 سیگنال رایگان") or (update.message and update.message.text in ("📈 سیگنال رایگان", "📈 Free Signal", "Free Signal")):
        # show only: intro (media or text), title, hidden deep-link in caption/text and a glass inline button
        if not SIGNAL_POST_ID:
            try:
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text="❌ No free signal has been selected yet.")
            except Exception:
                pass
            return

        post = get_post_db(SIGNAL_POST_ID)
        if not post:
            try:
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text="❌ سیگنال فعلی موجود نیست.")
            except Exception:
                pass
            return

        try:
            cap = json.loads(post.get("caption") or "{}")
        except Exception:
            cap = {}

        bot_user = await context.bot.get_me()
        bot_username = getattr(bot_user, "username", "") or ""
        deep_link = f"https://t.me/{bot_username}?start=get_{SIGNAL_POST_ID}" if bot_username else f"https://t.me/{SIGNAL_POST_ID}"

        title = cap.get("title", "بدون عنوان")
        intro = cap.get("intro_file", {}) or {}
        caption_html = f"📌 {title}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Receive", url=deep_link)]])

        try:
            # If intro is media -> send intro media with caption (title + hidden link) and glass button beneath
            if intro.get("file_id"):
                if intro.get("type") == "photo":
                    await context.bot.send_photo(chat_id=chat_id, photo=intro["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                else:
                    await context.bot.send_document(chat_id=chat_id, document=intro["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
            # If intro is text -> send one message with title + intro text + hidden link (glass button beneath)
            elif intro.get("type") == "text" and intro.get("text"):
                txt = f"📌 {title}\n\n{intro.get('text')}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
                await context.bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb, parse_mode="HTML")
            else:
                # no intro -> only title + hidden link + glass button
                await context.bot.send_message(chat_id=chat_id, text=caption_html, reply_markup=kb, parse_mode="HTML")
        except Exception:

            try:
                await context.bot.send_message(chat_id=chat_id, text=caption_html, reply_markup=kb)
            except Exception:
                pass
        return


    # 📢 ارسال همگانی (با گزارش نهایی)
    if data == "broadcast_confirm":
        try:
            user = update.effective_user
            if not user or getattr(user, "username", None) not in ADMINS:
                await context.bot.send_message(chat_id=chat_id, text="❌ فقط ادمین می‌تواند پیام ارسال کند.")
                return

            text = context.user_data.get("broadcast_text")
            if not text:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ هیچ پیامی برای ارسال وجود ندارد.")
                return

            # حذف حالت انتظار تا دوباره اشتباه وارد نشود
            context.user_data.pop("broadcast_text", None)

            # دریافت لیست کاربران ثبت‌شده در دیتابیس
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users")
            user_ids = [row[0] for row in cur.fetchall()]
            conn.close()

            total = len(user_ids)
            sent = 0
            failed = 0

            status_msg = await context.bot.send_message(chat_id=chat_id, text=f"⏳ در حال ارسال پیام به {total} کاربر...")

            for i, uid in enumerate(user_ids, start=1):
                try:
                    await context.bot.send_message(chat_id=uid, text=text)
                    sent += 1
                except (Forbidden, RetryAfter, TimedOut):
                    failed += 1
                except Exception:
                    failed += 1
                    continue

                if i % 50 == 0:
                    try:
                        await status_msg.edit_text(f"📨 در حال ارسال... {i}/{total}\n✅ موفق: {sent} | 🚫 خطا: {failed}")
                    except Exception:
                        pass

                await asyncio.sleep(0.08)  # تاخیر کوچک برای جلوگیری از flood

            # گزارش نهایی
            report = (
                f"✅ گزارش نهایی:\n\n"
                f"📨 ارسال موفق: {sent}\n"
                f"🚫 ناموفق: {failed}\n"
                f"👥 کل کاربران: {total}"
            )
            await context.bot.send_message(chat_id=query.from_user.id, text=report)
        except Exception as e:
            await context.bot.send_message(chat_id=query.from_user.id, text=f"❌ خطا در ارسال همگانی:\n{str(e)}")
    # 🔹 لغو ارسال به همه (درست و هم‌سطح با try:)
    if data == "cancel_broadcast":
        context.user_data.pop("awaiting_broadcast_text", None)
        await context.bot.send_message(chat_id=chat_id, text="❌ ارسال به همه لغو شد.")
        return

    # Do NOT delete preview when handling edit/delete flows
    # keep preview when handling any edit/delete/confirm/cancel flows so "خیر" (cancel_delete_) won't remove the post
    if data and not (
        data.startswith("delete_post_")
        or data.startswith("edit_post_")
        or data.startswith("edit_field_")
        or data.startswith("confirm_delete_")
        or data.startswith("cancel_delete_")
    ):
         try:
             if query and getattr(query, "message", None):
                 await query.message.delete()
         except Exception:
             pass

    # handle edit-field callbacks (admin clicked one of the 5 edit buttons)
    if data and data.startswith("edit_field_"):
        try:
            payload = data.split("edit_field_")[1]
            post_part, field = payload.split("_", 1)
            post_id = int(post_part)
        except Exception:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text="❌ شناسه یا فیلد نامعتبر.")
            return

        labels = {
            "main_file": "فایل اصلی",
            "intro_file": "فایل معرفی",
            "title": "عنوان",
            "description": "کپشن",
            "channels": "کانال‌های جوین"
        }
        label = labels.get(field, field)

        if field in ("main_file", "intro_file"):
            try:
                if chat_id:
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ بخش «{label}» فعلاً غیرفعال است.")
            except Exception:
                pass
            return

        # set editing state for next message
        context.user_data["editing_post_id"] = post_id
        context.user_data["editing_field"] = field
        try:
            if query.message:
                context.user_data["editing_preview_msg_id"] = query.message.message_id
                context.user_data["editing_preview_chat_id"] = query.message.chat_id
                context.user_data["editing_preview_reply_markup"] = query.message.reply_markup
        except Exception:
            pass
        try:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=f"✏️ لطفاً مقدار جدید برای «{label}» پست {post_id} را ارسال کنید.\nبرای انصراف /cancel استفاده کنید.")
        except Exception:
            pass
        return

    # handle receiving new value for editing fields (title/description/channels)
    if update.message and context.user_data.get("editing_post_id") and context.user_data.get("editing_field"):
        post_id = context.user_data["editing_post_id"]
        field = context.user_data["editing_field"]
        new_value = update.message.text.strip()
        # update DB
        post = get_post_db(post_id)
        if not post:
            await update.message.reply_text("❌ پست یافت نشد.")
            context.user_data.pop("editing_post_id", None)
            context.user_data.pop("editing_field", None)
            return
        try:
            cap = json.loads(post["caption"])
        except Exception:
            cap = {}

        # update the field
        if field == "title":
            cap["title"] = new_value
        elif field == "description":
            cap["description"] = new_value
        elif field == "channels":
            # parse and store as JSON string
            parsed = parse_channels_text(new_value)
            channels_json = json.dumps(parsed, ensure_ascii=False)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("UPDATE posts SET channels = ? WHERE id = ?", (channels_json, post_id))
            conn.commit()
            conn.close()
            # update caption in DB as well (for consistency)
            cur = sqlite3.connect(DB_PATH).cursor()
            cur.execute("UPDATE posts SET caption = ? WHERE id = ?", (json.dumps(cap, ensure_ascii=False), post_id))
            cur.connection.commit()
            cur.connection.close()
        else:
            # update caption only
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("UPDATE posts SET caption = ? WHERE id = ?", (json.dumps(cap, ensure_ascii=False), post_id))
            conn.commit()
            conn.close()

        if field != "channels":
            # update caption only
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("UPDATE posts SET caption = ? WHERE id = ?", (json.dumps(cap, ensure_ascii=False), post_id))
            conn.commit()
            conn.close()

        await update.message.reply_text("✅ مقدار جدید ذخیره شد.")

        # clear editing state
        context.user_data.pop("editing_post_id", None)
        context.user_data.pop("editing_field", None)
        context.user_data.pop("editing_preview_msg_id", None)
        context.user_data.pop("editing_preview_chat_id", None)
        context.user_data.pop("editing_preview_reply_markup", None)
        return

    # handle delete button callback -> show confirmation
    if data and data.startswith("delete_post_"):
        try:
            post_id = int(data.split("delete_post_")[1])
        except Exception:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text="❌ شناسه نامعتبر.")
            return

        # ساخت منوی تأیید حذف با دکمه‌های بله/خیر
        delete_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ آیا از حذف این پست مطمئن هستید؟", callback_data="dummy")],
            [
                InlineKeyboardButton("بله، حذف شود ✅", callback_data=f"confirm_delete_{post_id}:0"),
                InlineKeyboardButton("خیر ❌", callback_data=f"cancel_delete_{post_id}:0")
            ]
        ])
        
        try:
            if query and query.message:
                # فقط دکمه‌ها را عوض می‌کنیم، متن پیام را دست نمی‌زنیم
                await query.message.edit_reply_markup(reply_markup=delete_kb)
        except Exception:
            logger.exception("Could not edit delete confirmation buttons")
        return

    # handle confirmation -> actually delete
    if data and data.startswith("confirm_delete_"):
        try:
            payload = data.split("confirm_delete_")[1]
            post_part, preview_part = payload.split(":", 1)
            post_id = int(post_part)
            preview_msg_id = int(preview_part)
        except Exception:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text="❌ شناسه نامعتبر.")
            return

        # Instead of editing reply_markup, completely delete the message with the post and its buttons
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass

        # try to remove any local files referenced in the stored caption (safe best-effort)
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT caption, channels FROM posts WHERE id = ?", (post_id,))
            row = cur.fetchone()
            conn.close()
        except Exception:
            row = None

        if row:
            try:
                cap_json = json.loads(row[0])
            except Exception:
                cap_json = {}
            for key in ("main_file", "intro_file"):
                fobj = cap_json.get(key, {}) if isinstance(cap_json, dict) else {}
                local_path = fobj.get("path")
                if local_path:
                    try:
                        p = Path(local_path)
                        if p.exists():
                            p.unlink()
                    except Exception:
                        logger.exception(f"Failed to remove local file for post {post_id}: {local_path}")

        # delete DB entry
        try:
            delete_post_db(post_id)
        except Exception:
            logger.exception(f"Failed to delete post {post_id} from DB")
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ خطا در حذف پست {post_id} از منبع.")
            return

        # Remove any confirmation messages if present (optional)
        try:
            if preview_msg_id and chat_id:
                await context.bot.delete_message(chat_id=chat_id, message_id=preview_msg_id)
        except Exception:
            logger.exception(f"Failed to delete preview message for post {post_id}")

        # ارسال پیام تایید حذف جداگانه
        try:
            if chat_id:
                confirm = await context.bot.send_message(chat_id=chat_id, text=f"✅ پست {post_id} با موفقیت حذف شد.")
                await asyncio.sleep(3)
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=confirm.message_id)
                except Exception:
                    pass
        except Exception:
            pass
        return

    # handle cancel deletion -> restore original edit/delete buttons (do NOT delete the message)
    if data and data.startswith("cancel_delete_"):
        try:
            payload = data.split("cancel_delete_")[1]
            post_part, _ = payload.split(":", 1)
            post_id = int(post_part)
            # بازگرداندن دکمه‌های اصلی (ویرایش و حذف)
            original_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_post_{post_id}"),
                 InlineKeyboardButton("❌ حذف", callback_data=f"delete_post_{post_id}")]
            ])
            if query and query.message:
                await query.message.edit_reply_markup(reply_markup=original_kb)
       
        except Exception:
            logger.exception("Could not restore original buttons")
        return

    # handle edit_post_ etc.
    if data and data.startswith("edit_post_"):
        try:
            post_id = int(data.split("edit_post_")[1])
        except Exception:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text="❌ شناسه نامعتبر.")
            return
        post = get_post_db(post_id)
        if not post:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text="❌ پست یافت نشد.")
            return

        # ساخت منوی ویرایش جدید (دکمه‌های ویرایش فیلدها و حذف، بدون دکمه ویرایش/حذف اصلی)
        edit_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 فایل اصلی", callback_data=f"edit_field_{post_id}_main_file")],
            [InlineKeyboardButton("📎 فایل معرفی", callback_data=f"edit_field_{post_id}_intro_file")],
            [InlineKeyboardButton("📝 عنوان", callback_data=f"edit_field_{post_id}_title")],
            [InlineKeyboardButton("🖋️ کپشن", callback_data=f"edit_field_{post_id}_description")],
            [InlineKeyboardButton("🔗 کانال‌های جوین", callback_data=f"edit_field_{post_id}_channels")],
            [InlineKeyboardButton("❌ حذف", callback_data=f"delete_post_{post_id}")]
        ])

        # فقط reply_markup را ویرایش کن تا منوی ویرایش زیر همان پست باز شود و دکمه‌های قبلی حذف شوند
        try:
            if query.message:
                await query.message.edit_reply_markup(reply_markup=edit_kb)
                return
        except Exception:
            logger.exception(f"Could not edit preview message for post {post_id}; sending edit menu separately.")

        # اگر نشد، منوی ویرایش را جداگانه ارسال کن
        try:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text=f"✏️ انتخاب بخش برای ویرایش پست {post_id}:", reply_markup=edit_kb)
        except Exception:
            pass
        return

    # handle info/edit menu callback (show post details + edit buttons)
    if data == "admin_listposts":
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id, caption, channels FROM posts ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
        conn.close()

        if not rows:
            kb = ReplyKeyboardMarkup([["برگشت"]], resize_keyboard=True)
            await context.bot.send_message(chat_id=chat_id, text="✨ هیچ پستی یافت نشد. ✨", reply_markup=kb)
            return

        # Send each post as a separate message with full details
        for row in rows:
            post_id = row[0]
            try:
                cap = json.loads(row[1]) if row[1] else {}
                channels_text = row[2] if row[2] else ""
                
                # Parse channels for display
                channels = []
                try:
                    channels = json.loads(channels_text) if channels_text else []
                except:
                    channels = [{"name": c.strip(), "username": c.strip().lstrip('@')} 
                              for c in channels_text.splitlines() if c.strip()]

                # Build channels display text
                channels_display = "\n".join(f"• {ch['name']} — @{ch['username']}" 
                                          for ch in channels) if channels else "بدون کانال"

                title = cap.get("title", "بدون عنوان")
                desc = cap.get("description", "") or "بدون توضیحات"
                intro = cap.get("intro_file", {})

                # Build full post info text
                caption = f"📌 #{post_id} — {title}\n\n"
                caption += f"📝 توضیحات:\n{desc}\n\n"
                caption += f"🔗 کانال‌های جوین:\n{channels_display}"

                # Create edit/delete buttons
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_post_{post_id}"),
                     InlineKeyboardButton("❌ حذف", callback_data=f"delete_post_{post_id}")]
                ])

                # Send with intro file if exists, otherwise just text
                if intro.get("file_id"):
                    if intro.get("type") == "photo":
                        await context.bot.send_photo(
                            chat_id=chat_id,
                            photo=intro["file_id"],
                            caption=caption,
                            reply_markup=kb
                        )
                    else:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=intro["file_id"],
                            caption=caption,
                            reply_markup=kb
                        )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        reply_markup=kb
                    )
                
                # Add small delay between posts
                await asyncio.sleep(0.3)

            except Exception as e:
                logger.exception(f"Error displaying post {post_id}")
                continue

        return

    # Public: show "پست های جذاب" — support both callback.data == text or plain message text forwarded here
    if (data and data in ("📱 پست های پرطرفدار", "پست های پرطرفدار", "📱 Popular Posts", "Popular Posts")) or (update.message and update.message.text and update.message.text in ("📱 پست های پرطرفدار", "پست های پرطرفدار", "📱 Popular Posts", "Popular Posts")):
        # treat as admin "پست های ارسالی" preview so public sees the same posts/layout
        try:
            bot_user = await context.bot.get_me()
            bot_username = getattr(bot_user, "username", "") or ""
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, caption FROM posts ORDER BY id DESC")
            rows = cur.fetchall()
            conn.close()
        except Exception:
            rows = []

        if not rows:
            if chat_id:
                await context.bot.send_message(chat_id=chat_id, text="✨ هیچ پستی یافت نشد.")
            return

        for row in rows:
            post_id = row[0]
            try:
                cap = json.loads(row[1]) if row[1] else {}
            except Exception:
                cap = {"title": "بدون عنوان", "intro_file": {}, "main_file": {}}

            title = cap.get("title", "بدون عنوان")
            intro = cap.get("intro_file", {}) or {}
            main = cap.get("main_file", {}) or {}

            deep_link = f"https://t.me/{bot_username}?start=get_{post_id}" if bot_username else f"https://t.me/{post_id}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Receive", url=deep_link)]])
            caption_html = f"📌 {title}\n\n<a href=\"{deep_link}\">📥 Receive</a>"

            try:
                if intro.get("file_id"):
                    if intro.get("type") == "photo":
                        await context.bot.send_photo(chat_id=chat_id, photo=intro["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                    else:
                        await context.bot.send_document(chat_id=chat_id, document=intro["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                elif intro.get("type") == "text" and intro.get("text"):
                    full_text = f"📌 {title}\n\n{intro.get('text')}\n\n<a href=\"{deep_link}\">📥 Receive</a>"
                    await context.bot.send_message(chat_id=chat_id, text=full_text, reply_markup=kb, parse_mode="HTML")
                elif main.get("file_id"):
                    if main.get("type") == "photo":
                        await context.bot.send_photo(chat_id=chat_id, photo=main["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                    else:
                        await context.bot.send_document(chat_id=chat_id, document=main["file_id"], caption=caption_html, reply_markup=kb, parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=caption_html, reply_markup=kb, parse_mode="HTML")
            except Exception:
                logger.exception(f"Error sending post {post_id}")
                try:
                    await context.bot.send_message(chat_id=chat_id, text=caption_html)
                except Exception:
                    pass
            await asyncio.sleep(0.25)
        return

    # Admin: show signal settings submenu (ثبت سیگنال / دیدن سیگنال / برگشت)
    if data == "admin_signal_menu":
        kb = ReplyKeyboardMarkup(
            [
                ["ثبت سیگنال", "دیدن سیگنال"],
                ["🔙 برگشت"]
            ],
                       resize_keyboard=True
        )
        try:
            await context.bot.send_message(chat_id=chat_id, text="⚙️ تنظیم سیگنال رایگان: یکی از گزینه‌ها را انتخاب کنید.", reply_markup=kb)
        except Exception:
            pass
        return
async def cancel_support_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لغو تنظیم آیدی پشتیبان — supports both message-based and callback-based cancel."""
    try:
        # If triggered by callback query (inline button)
        if getattr(update, "callback_query", None):
            cq = update.callback_query
            await cq.answer()
            # clear awaiting flag
            if context.user_data.get("awaiting_support_id"):
                context.user_data.pop("awaiting_support_id", None)
            # try to edit original message to reflect cancellation (best UX)
            try:
                if getattr(cq, "message", None):
                    await cq.message.edit_text("❌ تنظیم آیدی پشتیبان لغو شد.")
                    return
            except Exception:
                pass
            # fallback: send a small confirmation message to the user
            try:
                await context.bot.send_message(chat_id=cq.from_user.id, text="❌ تنظیم آیدی پشتیبان لغو شد.")
            except Exception:
                logger.exception("Failed to notify user about cancelling support id via callback")
            return

        # If triggered by plain message (text command)
        if context.user_data.get("awaiting_support_id"):
            context.user_data.pop("awaiting_support_id", None)
            if getattr(update, "message", None):
                await update.message.reply_text("❌ تنظیم آیدی پشتیبان لغو شد.")
            return

        # nothing to cancel
        if getattr(update, "message", None):
            await update.message.reply_text("⚠️ در حال حاضر چیزی برای لغو وجود ندارد.")
    except Exception:
        logger.exception("Error in cancel_support_id")

# ===============================
# ✅ Menu handler
# ===============================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_callback(update, context)


# ===============================
# ✅ فعال‌سازی منو برای دکمه‌های متنی (ReplyKeyboard)
# ===============================
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تمام دکمه‌های متنی منو را مدیریت می‌کند"""
    await menu_callback(update, context)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    ...

    # basic commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("intro", send_intro))
    app.add_handler(CommandHandler("stats", stats_bot))

    # Conversation for creating new posts (moved into main)
    newpost_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newpost", newpost_start),
            MessageHandler(filters.Regex(r"^🆕 پست جدید$"), newpost_start),
            MessageHandler(filters.Regex(r"^📣 سیگنال رایگان$"), newpost_start),
        ],
        states={
            NP_MAIN: [MessageHandler(filters.ALL & ~filters.COMMAND, newpost_main)],
            NP_INTRO: [MessageHandler(filters.ALL & ~filters.COMMAND, newpost_intro)],
            NP_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, newpost_title)],
            NP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, newpost_desc)],
            NP_CHANNELS: [MessageHandler(filters.TEXT & ~filters.COMMAND, newpost_channels)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=False,
        per_user=True,
    )
    app.add_handler(newpost_conv)

    # admin and utility commands
    app.add_handler(CommandHandler("listposts", list_posts))
    app.add_handler(CommandHandler("deletepost", delete_post))
    app.add_handler(CommandHandler("order_member", order_member))
   
    # ===============================
    # ✅ Callback Query Handlers
    # ===============================
    app.add_handler(CallbackQueryHandler(broadcast_confirm_handler, pattern=r"^broadcast_confirm$"))
    app.add_handler(CallbackQueryHandler(broadcast_cancel_handler, pattern=r"^broadcast_cancel$"))
    app.add_handler(CallbackQueryHandler(receive_get_callback, pattern=r"^receive_get_"))
    app.add_handler(CallbackQueryHandler(continue_get_callback, pattern=r"^continue_get_"))

    # add specific cancel_support_id callback handler (must be registered before generic handlers)
    app.add_handler(CallbackQueryHandler(cancel_support_id, pattern=r"^cancel_support_id$"))

    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_callback))

    # ===============================
    # ✅ Broadcast Handlers
    # ===============================
    app.add_handler(MessageHandler(filters.Regex("^ارسال به همه$"), broadcast_start))
    app.add_handler(MessageHandler(filters.Regex("^❌ لغو ارسال به همه$"), broadcast_cancel_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_menu))

    # ===============================
      # ===============================
    # ✅ ReplyKeyboard / Menu Handler
    # ===============================
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_handler(CallbackQueryHandler(continue_get_callback, pattern=r"^continue_get_"))
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🆕 ثبت سیگنال$"), newpost_start)],
        states={
            NP_MAIN: [MessageHandler(filters.ALL, newpost_main)],
            NP_INTRO: [MessageHandler(filters.ALL, newpost_intro)],
            NP_TITLE: [MessageHandler(filters.TEXT, newpost_title)],
            NP_DESC: [MessageHandler(filters.TEXT, newpost_desc)],
            NP_CHANNELS: [MessageHandler(filters.ALL, newpost_channels)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))


    app.add_handler(MessageHandler(filters.Regex(r"^\d+$"), set_signal))
    app.add_handler(CallbackQueryHandler(set_signal_callback, pattern=r"^set_signal_"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^signal_post_"))

    # ===============================
    # ✅ Run bot
    # ===============================
    app.run_polling()

# ===============================
# 📢 Broadcast handlers
# ===============================
# ============================================================
# ✅ نسخه جدید و تست‌شده برای ارسال به همه (Broadcast)
# ============================================================

def get_all_users():
    """لیست تمام کاربران از دیتابیس را می‌گیرد"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        users = [row[0] for row in cur.fetchall()]
        conn.close()
        return users
    except Exception as e:
        logger.exception("get_all_users failed")
        return []


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع ارسال همگانی برای ادمین"""
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not user or getattr(user, "username", None) not in ADMINS:
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="❌ فقط ادمین اجازه ارسال دارد.")
        return

    await context.bot.send_message(chat_id=chat_id, text="📨 لطفاً پیام مورد نظر خود را بفرستید تا به همه ارسال شود.")
    context.user_data["awaiting_broadcast_text"] = True
    return


async def broadcast_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت هر نوع پیام از ادمین برای ارسال به همه (متن، عکس، ویدیو، فایل و ...)"""
    if not context.user_data.get("awaiting_broadcast_text"):
        return

    chat_id = update.effective_chat.id
    message = update.message

    # ذخیره نوع پیام
    context.user_data["broadcast_message"] = message
    context.user_data.pop("awaiting_broadcast_text", None)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، ارسال کن", callback_data="broadcast_confirm"),
         InlineKeyboardButton("❌ لغو شود", callback_data="broadcast_cancel")]
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text="آیا مطمئن هستید که می‌خواهید این پیام را برای همه ارسال کنید؟",
        reply_markup=kb
    )



async def broadcast_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ارسال پیام (هر نوعی) به همه کاربران و نمایش گزارش"""
    query = update.callback_query
    await query.answer()

    message = context.user_data.get("broadcast_message")
    if not message:
        await query.edit_message_text("❌ هیچ پیامی برای ارسال وجود ندارد.")
        return

    users = get_all_users()
    success = 0
    failed = 0
    total = len(users)

    status_msg = await query.edit_message_text(f"📨 در حال ارسال پیام به {total} کاربر...")

    for i, uid in enumerate(users, start=1):
        try:
            if message.text:
                await context.bot.send_message(chat_id=uid, text=message.text)
            elif message.photo:
                await context.bot.send_photo(chat_id=uid, photo=message.photo[-1].file_id, caption=message.caption or "")
            elif message.video:
                await context.bot.send_video(chat_id=uid, video=message.video.file_id, caption=message.caption or "")
            elif message.document:
                await context.bot.send_document(chat_id=uid, document=message.document.file_id, caption=message.caption or "")
            elif message.audio:
                await context.bot.send_audio(chat_id=uid, audio=message.audio.file_id, caption=message.caption or "")
            elif message.voice:
                await context.bot.send_voice(chat_id=uid, voice=message.voice.file_id, caption=message.caption or "")
            elif message.sticker:
                await context.bot.send_sticker(chat_id=uid, sticker=message.sticker.file_id)
            else:
                failed += 1
                continue

            success += 1
        except Exception:
            failed += 1
            continue

        if i % 50 == 0:
            try:
                await status_msg.edit_text(f"📨 در حال ارسال... {i}/{total}\n✅ موفق: {sent} | 🚫 خطا: {failed}")
            except Exception:
                pass

        await asyncio.sleep(0.05)

    report = f"✅ گزارش نهایی:\n\n📨 موفق: {success}\n🚫 ناموفق: {failed}\n👥 کل کاربران: {total}"
    await context.bot.send_message(chat_id=query.from_user.id, text=report)
    context.user_data.pop("broadcast_message", None)


async def broadcast_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لغو ارسال همگانی"""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    await context.bot.send_message(chat_id=chat_id, text="❌ ارسال پیام به همه لغو شد.")
    context.user_data.pop("broadcast_message", None)
    context.user_data.pop("awaiting_broadcast_text", None)

# ============================================================
import os
import asyncio
from aiohttp import web

# -------------------------------
# اجرای async bot و web server
# -------------------------------
async def run_bot():
    print("🚀 Starting Telegram Bot polling...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("✅ Bot is now polling.")
    await application.updater.idle()

async def handle(request):
    return web.Response(text="✅ Bot is running on Render (Free Plan)")



# ==============================
#  Telegram Bot Section
# ==============================

async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ ربات فعاله!")

def run_bot():
    token = os.getenv("BOT_TOKEN")  # از Environment Variables بگیر
    if not token:
        print("❌ BOT_TOKEN تعریف نشده! لطفاً در Render > Environment Variables اضافه کن.")
        return
    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", start))

    print("🚀 Bot is polling now...")
    application.run_polling(drop_pending_updates=True)

# ==============================
#  Web Server Section
# ==============================

async def handle(request):
    return web.Response(text="✅ Bot and web server running successfully on Render!")

def run_web():
    app = web.Application()
    app.router.add_get("/", handle)
    port = int(os.getenv("PORT", 10000))
    print(f"🌐 Web server running on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)

# ==============================
#  Main Run
# ==============================

if __name__ == "__main__":
    print("⚡ Starting bot and web server on Render...")

    # اجرای ربات در Thread جداگانه
    threading.Thread(target=run_bot, daemon=True).start()

    # اجرای وب سرور (اصلی برای Render)
    run_web()
