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

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# جلوگیری از چاپ توکن ربات توی لاگ (httpx آدرس کامل درخواست را لاگ می‌کند)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("yt-dlp version: %s", YTDLP_VERSION)

# سقف ارسال با Bot API معمولی
MAX_FILE_SIZE = 49 * 1024 * 1024

# حداکثر تعداد دانلود همزمان (سرور رایگان منابع کمی دارد)
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

# فاصله‌ی حداقل بین دو درخواست یک کاربر (ثانیه)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "15"))
_last_request: dict[int, float] = {}


# ---------------- Health Server ----------------

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


# ---------------- URL Validation ----------------

SOUNDCLOUD_HOSTS = {
    "soundcloud.com",
    "www.soundcloud.com",
    "m.soundcloud.com",
    "on.soundcloud.com",
}

# بخش‌هایی از آدرس که تک‌آهنگ نیستند (پروفایل، لیست‌ها و ...)
NON_TRACK_SEGMENTS = {
    "sets", "likes", "tracks", "albums", "reposts",
    "popular-tracks", "following", "followers", "comments",
}


def extract_url(text: str):
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


def is_soundcloud_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in SOUNDCLOUD_HOSTS
    except Exception:
        return False


def is_track_url(url: str) -> bool:
    """فقط لینک تک‌آهنگ: soundcloud.com/artist/track یا لینک کوتاه on.soundcloud.com/xxx"""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        segments = [x for x in parsed.path.split("/") if x]

        if host == "on.soundcloud.com":
            return len(segments) >= 1

        if len(segments) < 2:
            return False

        return segments[1].lower() not in NON_TRACK_SEGMENTS
    except Exception:
        return False


# ---------------- Bot Commands ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "سلام 👋\n\n"
        "لینک یک آهنگ ساندکلود را بفرست تا برایت بفرستم. 🎧\n"
        "فقط لینک تک‌آهنگ پشتیبانی می‌شود (نه پروفایل یا پلی‌لیست).\n"
        "فقط آهنگ‌هایی را دانلود کن که مال خودت هستند یا اجازه‌ی دانلودشان را داری."
    )


async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    url = extract_url(message.text)

    if not url or not is_soundcloud_url(url):
        await message.reply_text(
            "❌ لطفاً یک لینک معتبر ساندکلود بفرست."
        )
        return

    if not is_track_url(url):
        await message.reply_text(
            "❌ فقط لینک تک‌آهنگ پشتیبانی می‌شود (نه پروفایل یا پلی‌لیست).\n"
            "نمونه: soundcloud.com/artist/track"
        )
        return

    # محدودیت فاصله‌ی زمانی برای هر کاربر
    user_id = update.effective_user.id if update.effective_user else 0
    now = time.monotonic()
    last = _last_request.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last)) + 1
        await message.reply_text(f"⏳ لطفاً {wait} ثانیه‌ی دیگر دوباره تلاش کن.")
        return
    _last_request[user_id] = now

    status = await message.reply_text("⏳ در حال آماده‌سازی آهنگ...")

    try:
        async with DOWNLOAD_SEMAPHORE:
            with TemporaryDirectory() as tmpdir:
                opts = {
                    "outtmpl": str(Path(tmpdir) / "audio.%(ext)s"),
                    "quiet": False,
                    "no_warnings": False,
                    "noplaylist": True,
                    # اگر لینک اشتباهی پلی‌لیست بود، فقط اولین آهنگ
                    "playlist_items": "1",
                    "socket_timeout": 30,
                    # mp3 مستقیم (بدون نیاز به ffmpeg)، وگرنه بهترین صدا
                    "format": "http_mp3_128/bestaudio[protocol^=http]/bestaudio",
                }

                def run():
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(url, download=True)

                info = await asyncio.to_thread(run)

                # اگر نتیجه پلی‌لیست بود، اولین آهنگ
                entries = info.get("entries")
                if entries:
                    entries = [e for e in entries if e]
                    if entries:
                        info = entries[0]

                audio_exts = {
                    ".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".flac", ".webm",
                }
                files = [
                    p for p in Path(tmpdir).iterdir()
                    if p.is_file()
                    and p.suffix.lower() in audio_exts
                    and p.stat().st_size > 0
                ]

                if not files:
                    raise FileNotFoundError("Downloaded audio was not found")

                filename = max(files, key=lambda p: p.stat().st_size)

                if filename.stat().st_size > MAX_FILE_SIZE:
                    await status.edit_text(
                        "❌ حجم این آهنگ از سقف ارسال ربات (حدود ۵۰ مگابایت) بیشتر است."
                    )
                    return

                title = info.get("title") or "audio"
                performer = info.get("uploader") or ""
                safe_name = (
                    re.sub(r'[\\/:*?"<>|]+', "_", title)[:80].strip() or "audio"
                )

                await status.edit_text("📤 آهنگ آماده است؛ در حال ارسال...")

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

    except Exception as e:
        logger.exception("SoundCloud download failed")
        await status.edit_text(f"❌ خطا:\n{str(e)[:700]}")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    # خطاهای شبکه‌ی گذرا (مثل Conflict هنگام دیپلوی) را فقط کوتاه لاگ می‌کنیم
    logger.warning("Bot error: %s", context.error)


# ---------------- Main ----------------

def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )
    app.add_error_handler(on_error)

    logger.info("SoundCloud bot is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
