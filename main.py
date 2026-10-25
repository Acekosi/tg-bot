import os
import re
import sqlite3
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from fastapi import FastAPI
from telegram import Update, MessageEntity, ChatPermissions
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---

MAX_STRIKES = 3
MUTE_DURATION_HOURS = 24
DB_PATH = "strikes.db"

URL_REGEX = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|wa\.me/\S+|bit\.ly/\S+|(?<!\w)[a-zA-Z0-9-]+\.(com|org|net|io|xyz|me|co|ng|app|info)(/\S*)?)",
    re.IGNORECASE,
)

AD_KEYWORDS = [
    "join my channel",
    "dm me for",
    "inbox me for",
    "whatsapp me",
    "investment plan",
    "guaranteed profit",
    "account management",
    "flip your money",
    "click the link in bio",
    "airdrop link",
    "free signals",
    "promo price",
]

WHITELISTED_DOMAINS = [
    "github.com",
    "stackoverflow.com",
]


# --- DATABASE FUNCTIONS ---

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_strikes (
                chat_id INTEGER,
                user_id INTEGER,
                strikes INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.commit()


def add_strike(chat_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_strikes (chat_id, user_id, strikes)
            VALUES (?, ?, 1)
            ON CONFLICT(chat_id, user_id)
            DO UPDATE SET strikes = strikes + 1
            RETURNING strikes
            """,
            (chat_id, user_id),
        )
        new_strikes = cursor.fetchone()[0]
        conn.commit()
        return new_strikes


def reset_strikes(chat_id: int, user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM user_strikes WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        conn.commit()


# --- HELPER FUNCTIONS ---

async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message.sender_chat and message.sender_chat.id == chat.id:
        return True

    if not user:
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False


def contains_unapproved_link(text: str, entities: tuple) -> bool:
    for entity in entities:
        if entity.type in (MessageEntity.URL, MessageEntity.TEXT_LINK):
            url = (
                entity.url
                if entity.type == MessageEntity.TEXT_LINK
                else text[entity.offset : entity.offset + entity.length]
            )
            if not any(domain in url.lower() for domain in WHITELISTED_DOMAINS):
                return True

    matches = URL_REGEX.findall(text)
    for match in matches:
        full_match = match[0] if isinstance(match, tuple) else match
        if not any(domain in full_match.lower() for domain in WHITELISTED_DOMAINS):
            return True

    return False


def contains_ad_content(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in AD_KEYWORDS)


async def auto_delete_message(message, delay: int = 10):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


# --- BOT HANDLERS ---

async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_user_admin(update, context):
        return

    message = update.effective_message
    chat = update.effective_chat

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply_text("Reply to a user's message with /unwarn to clear their strikes.")
        asyncio.create_task(auto_delete_message(reply, delay=6))
        return

    target_user = message.reply_to_message.from_user
    reset_strikes(chat.id, target_user.id)

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception as e:
        logger.warning(f"Could not unrestrict user: {e}")

    name = f"@{target_user.username}" if target_user.username else target_user.first_name
    confirmation = await message.reply_text(f"✅ Cleared strikes and restored permissions for {name}.")
    asyncio.create_task(auto_delete_message(confirmation, delay=8))


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if await is_user_admin(update, context):
        return

    text_content = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or ()

    reason = None

    if message.forward_origin and message.forward_origin.type == "channel":
        reason = "forwarding channel posts"
    elif contains_unapproved_link(text_content, entities):
        reason = "posting unauthorized links"
    elif contains_ad_content(text_content):
        reason = "advertising / promotional content"

    if not reason:
        return

    raw_mention = f"@{user.username}" if user.username else user.first_name
    safe_mention = escape_markdown(raw_mention, version=1)

    try:
        await message.delete()
        current_strikes = add_strike(chat.id, user.id)
        logger.info(f"Strike {current_strikes}/{MAX_STRIKES} for {raw_mention} in {chat.title} ({reason}).")

        if current_strikes >= MAX_STRIKES:
            until_time = datetime.now(timezone.utc) + timedelta(hours=MUTE_DURATION_HOURS)

            await context.bot.restrict_chat_member(
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_time,
            )

            reset_strikes(chat.id, user.id)

            mute_notice = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"🔇 {raw_mention} reached {MAX_STRIKES}/{MAX_STRIKES} strikes "
                    f"for {reason} and has been muted for {MUTE_DURATION_HOURS} hours."
                ),
            )
            asyncio.create_task(auto_delete_message(mute_notice, delay=15))

        else:
            warning = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"⚠️ {safe_mention}, your message was removed ({reason} is not allowed).\n"
                    f"Strike *{current_strikes}/{MAX_STRIKES}* — {MAX_STRIKES} strikes results in a 24-hour mute."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            asyncio.create_task(auto_delete_message(warning, delay=10))

    except Exception as e:
        logger.error(f"Moderation error in {chat.title}: {e}")


# --- FASTAPI LIFESPAN & HEALTH CHECK ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment variables.")

    init_db()

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("unwarn", unwarn_command, filters=filters.ChatType.GROUPS))
    group_filter = filters.ChatType.GROUPS & (~filters.StatusUpdate.ALL)
    bot_app.add_handler(MessageHandler(group_filter, moderate_message))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot is live and polling in the background...")

    yield

    logger.info("Shutting down bot...")
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health_check():
    return {"status": "online", "message": "Telegram Moderation Bot is active"}