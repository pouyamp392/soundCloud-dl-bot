import os
import re
import time
import logging
import asyncio
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import aiohttp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_API_URL = os.getenv(
    "ADMIN_API_URL",
    "https://admin-dl-bot.onrender.com",
).strip().rstrip("/")
API_KEY = os.getenv("API_KEY", "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

if not ADMIN_API_URL:
    raise ValueError("ADMIN_API_URL not found")

if not API_KEY:
    raise ValueError("API_KEY not found")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("yt-dlp version: %s", YTDLP_VERSION)

MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT", "2")))
COOLDOWN_SECONDS = max(0, int(os.getenv("COOLDOWN_SECONDS", "15")))

_last_request: dict[int, float] = {}
DOWNLOAD_SEMAPHORE = None


# ==================== سرور سلامت Render ====================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"SoundCloud bot is alive")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


# ==================== منوی آبی کنار کیبورد (Bot Commands) ====================

async def setup_bot_commands(app: Application):
    commands = [
        BotCommand("start", "🚀 استارت"),
    ]

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeDefault(),
    )

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats(),
    )

    logger.info("Bot commands configured.")


# ==================== منوی شیشه‌ای (Inline Keyboard) جای کیبورد ====================

def get_main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚀 استارت",
                callback_data="menu:start",
            )
        ],
    ])


# ==================== کیبورد پایین (Reply Keyboard) با دکمه استارت ====================

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🚀 استارت")],
    ]
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="لینک ساندکلود را بفرست یا استارت بزن...",
    )


# ==================== جوین اجباری ====================

async def check_membership(user_id: int):
    """
    خروجی:
    (True, [])      کاربر مجاز است
    (False, list)   کاربر عضو همه کانال‌های لازم نیست
    (None, [])      بررسی عضویت ناموفق بوده است
    """

    url = f"{ADMIN_API_URL}/api/check"
    headers = {"X-API-Key": API_KEY}
    params = {"user_id": user_id}
    timeout = aiohttp.ClientTimeout(total=20)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers=headers,
                params=params,
            ) as response:

                if response.status != 200:
                    logger.error(
                        "Membership API returned HTTP %s",
                        response.status,
                    )
                    return None, []

                data = await response.json()

        if not isinstance(data, dict):
            logger.error("Invalid membership API response")
            return None, []

        # اگر جوین اجباری در پنل خاموش باشد، اجازه استفاده داده می‌شود.
        if data.get("enabled") is False:
            return True, []

        missing = data.get("missing", [])

        if not isinstance(missing, list):
            logger.error("Invalid missing channels list")
            return None, []

        # تنها پاسخ صریح و معتبر اجازه عبور می‌دهد.
        if data.get("ok") is True and not missing:
            return True, []

        if data.get("ok") is False:
            return False, missing

        logger.error("Membership API returned an uncertain result")
        return None, []

    except Exception:
        logger.exception("Membership API request failed")
        return None, []


def membership_markup(missing: list) -> InlineKeyboardMarkup:
    rows = []

    for channel in missing:
        if not isinstance(channel, dict):
            continue

        link = channel.get("link")
        title = channel.get("title") or "کانال"

        if isinstance(link, str) and link.startswith(
            ("https://", "http://")
        ):
            rows.append([
                InlineKeyboardButton(
                    text=f"عضویت در {title[:40]}",
                    url=link,
                )
            ])

    rows.append([
        InlineKeyboardButton(
            text="✅ عضو شدم، بررسی مجدد",
            callback_data="join:check",
        )
    ])

    return InlineKeyboardMarkup(rows)


async def require_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:

    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return False

    allowed, missing = await check_membership(user.id)

    if allowed is True:
        return True

    if allowed is None:
        await message.reply_text(
            "⚠️ در حال حاضر امکان بررسی عضویت وجود ندارد.\n"
            "لطفاً کمی بعد دوباره تلاش کن."
        )
        return False

    await message.reply_text(
        "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو.\n\n"
        "بعد از عضویت روی «عضو شدم، بررسی مجدد» بزن.",
        reply_markup=membership_markup(missing),
    )

    return False


async def recheck_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    allowed, missing = await check_membership(
        query.from_user.id
    )

    if allowed is None:
        await query.answer(
            "بررسی عضویت ناموفق بود؛ کمی بعد دوباره تلاش کن.",
            show_alert=True,
        )
        return

    await query.answer()

    if allowed is True:
        try:
            await query.edit_message_text(
                "✅ عضویتت تأیید شد!\n\n"
                "حالا لینک آهنگ ساندکلود رو بفرست."
            )
        except Exception:
            pass
        return

    try:
        await query.edit_message_text(
            "❌ هنوز عضویتت در همه کانال‌های لازم تأیید نشده.\n\n"
            "عضو شو و دوباره بررسی کن.",
            reply_markup=membership_markup(missing),
        )
    except Exception:
        pass


# ==================== بررسی لینک ساندکلود ====================

SOUNDCLOUD_HOSTS = {
    "soundcloud.com",
    "www.soundcloud.com",
    "m.soundcloud.com",
    "on.soundcloud.com",
}

NON_TRACK_SEGMENTS = {
    "sets",
    "likes",
    "tracks",
    "albums",
    "reposts",
    "popular-tracks",
    "following",
    "followers",
    "comments",
}


def extract_url(text: str):
    match = re.search(r"https?://\S+", text)

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}")


def is_soundcloud_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in SOUNDCLOUD_HOSTS
    except Exception:
        return False


def is_track_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        segments = [
            part for part in parsed.path.split("/") if part
        ]

        if host == "on.soundcloud.com":
            return len(segments) >= 1

        if len(segments) < 2:
            return False

        return segments[1].lower() not in NON_TRACK_SEGMENTS

    except Exception:
        return False


# ==================== دستور شروع ====================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await require_membership(update, context):
        return

    # کیبورد پایین با دکمه استارت
    await update.effective_message.reply_text(
        "سلام 👋\n\n"
        "🎧 لینک یک آهنگ ساندکلود رو بفرست تا برات دانلود کنم.\n"
        "فقط لینک تک‌آهنگ پشتیبانی می‌شه؛ نه پروفایل یا پلی‌لیست.\n\n"
        "⚠️ فقط آهنگ‌هایی رو دانلود کن که متعلق به خودت هستن "
        "یا اجازه دانلودشون رو داری.",
        reply_markup=get_main_reply_keyboard(),
    )


# ==================== هندلر دکمه استارت از کیبورد پایین ====================

async def handle_reply_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not message.text:
        return

    if message.text.strip() == "🚀 استارت":
        await start(update, context)


# ==================== هندلر دکمه‌های شیشه‌ای منو ====================

async def menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    if query.data == "menu:start":
        # بررسی عضویت
        allowed, missing = await check_membership(query.from_user.id)

        if allowed is True:
            await query.message.reply_text(
                "سلام 👋\n\n"
                "🎧 لینک یک آهنگ ساندکلود رو بفرست تا برات دانلود کنم.\n"
                "فقط لینک تک‌آهنگ پشتیبانی می‌شه؛ نه پروفایل یا پلی‌لیست.\n\n"
                "⚠️ فقط آهنگ‌هایی رو دانلود کن که متعلق به خودت هستن "
                "یا اجازه دانلودشون رو داری.",
                reply_markup=get_main_reply_keyboard(),
            )
            return

        if allowed is None:
            await query.answer(
                "بررسی عضویت ناموفق بود؛ کمی بعد دوباره تلاش کن.",
                show_alert=True,
            )
            return

        await query.message.reply_text(
            "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو.\n\n"
            "بعد از عضویت روی «عضو شدم، بررسی مجدد» بزن.",
            reply_markup=membership_markup(missing),
        )


# ==================== دانلود آهنگ ====================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    # عضویت قبل از دسترسی به قابلیت‌های ربات بررسی می‌شود.
    if not await require_membership(update, context):
        return

    url = extract_url(message.text)

    if not url or not is_soundcloud_url(url):
        await message.reply_text(
            "❌ لطفاً یک لینک معتبر ساندکلود بفرست."
        )
        return

    if not is_track_url(url):
        await message.reply_text(
            "❌ فقط لینک تک‌آهنگ پشتیبانی می‌شه، نه پروفایل یا پلی‌لیست.\n"
            "نمونه: soundcloud.com/artist/track"
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0

    now = time.monotonic()
    last = _last_request.get(user_id, 0)

    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last)) + 1
        await message.reply_text(
            f"⏳ لطفاً {wait} ثانیه دیگه دوباره تلاش کن."
        )
        return

    _last_request[user_id] = now

    status = await message.reply_text(
        "⏳ لینک دریافت شد؛ در حال آماده‌سازی آهنگ..."
    )

    try:
        async with DOWNLOAD_SEMAPHORE:
            with TemporaryDirectory() as tmpdir:
                opts = {
                    "outtmpl": str(
                        Path(tmpdir) / "audio.%(ext)s"
                    ),
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "socket_timeout": 30,
                    "format": (
                        "http_mp3_128/"
                        "bestaudio[protocol^=http]/bestaudio"
                    ),
                }

                def download():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(
                            url,
                            download=True,
                        )

                info = await asyncio.to_thread(download)

                entries = info.get("entries")
                if entries:
                    entries = [entry for entry in entries if entry]
                    if entries:
                        info = entries[0]

                audio_exts = {
                    ".mp3", ".m4a", ".opus", ".ogg",
                    ".aac", ".wav", ".flac", ".webm",
                }

                files = [
                    path
                    for path in Path(tmpdir).iterdir()
                    if path.is_file()
                    and path.suffix.lower() in audio_exts
                    and path.stat().st_size > 0
                ]

                if not files:
                    raise FileNotFoundError(
                        "Downloaded audio was not found"
                    )

                filename = max(
                    files,
                    key=lambda path: path.stat().st_size,
                )

                if filename.stat().st_size > MAX_FILE_SIZE:
                    await status.edit_text(
                        "❌ حجم این آهنگ از سقف ارسال ربات "
                        "(حدود ۵۰ مگابایت) بیشتره."
                    )
                    return

                title = info.get("title") or "audio"
                performer = info.get("uploader") or ""

                safe_name = re.sub(
                    r'[\\/:*?"<>|]+',
                    "_",
                    title,
                )[:80].strip() or "audio"

                await status.edit_text(
                    "📤 آهنگ آماده شد؛ در حال ارسال..."
                )

                with filename.open("rb") as audio:
                    await context.bot.send_audio(
                        chat_id=message.chat_id,
                        audio=audio,
                        filename=f"{safe_name}{filename.suffix}",
                        title=title[:200],
                        performer=performer[:200] or None,
                        duration=int(info.get("duration") or 0) or None,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

                await status.delete()

    except Exception:
        logger.exception("SoundCloud download failed")

        try:
            await status.edit_text(
                "❌ دانلود انجام نشد.\n"
                "ممکنه لینک خصوصی یا غیرقابل دانلود باشه؛ "
                "لطفاً دوباره تلاش کن."
            )
        except Exception:
            pass


# ==================== مدیریت خطاها ====================

async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error("Bot error: %s", context.error)


# ==================== اجرای ربات ====================

async def post_init(app: Application):
    global DOWNLOAD_SEMAPHORE
    DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)
    await setup_bot_commands(app)


def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        CallbackQueryHandler(
            recheck_membership,
            pattern=r"^join:check$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^menu:",
        )
    )

    # هندلر دکمه استارت از کیبورد پایین (قبل از handle_url)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"^🚀 استارت$"),
            handle_reply_keyboard,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    app.add_error_handler(on_error)

    logger.info("SoundCloud bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
