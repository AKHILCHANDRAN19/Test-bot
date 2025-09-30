import os
import sys
import time
import logging
import traceback
import asyncio
import psutil
from functools import partial

# --- Pyrogram Imports ---
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram.errors import MessageNotModified

# --- Third-party Imports ---
import yt_dlp
from yt_dlp.utils import DownloadError
import ffmpeg_static  # <-- NEW ADDITION 1 of 2: Import the static ffmpeg library

# --- LOGGING SETUP ---
logging.basicConfig(format="%(asctime)s — %(name)s — %(levelname)s — %(message)s", level=logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- CONFIGURATION & VALIDATION ---
required_vars = ["API_ID", "API_HASH", "BOT_TOKEN"]
missing_vars = [var for var in required_vars if not os.environ.get(var)]

if missing_vars:
    logger.critical(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

DOWNLOAD_DIR = "downloads"
REPO_STICKER_ID = "CAACAgIAAxkBAAE7p09o1ilNV72lFmr4Z4_r6mkRg9L_twACTAADJHFiGkVXuTkHH0tVNgQ"

# --- BOT INSTANCE & START TIME ---
bot_start_time = time.time()
app = Client("yt_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_settings = {}

# --- BROWSER HEADERS FOR FALLBACK ---
HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36','Accept-Language': 'en-US,en;q=0.5'}

# --- BOT HELPER FUNCTIONS ---
progress_status = {}

def humanbytes(size):
    if not size: return "0B"
    power = 1024; n = 0; labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power: size /= power; n += 1
    return f"{size:.2f} {labels[n]}B"

def time_formatter(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60); hours, minutes = divmod(minutes, 60); days, hours = divmod(hours, 24)
    tmp = ((f"{days}d, ") if days else "") + ((f"{hours}h, ") if hours else "") + ((f"{minutes}m, ") if minutes else "") + ((f"{seconds}s") if seconds else "")
    return tmp.strip(', ') or "0s"

async def edit_message_helper(client, chat_id, message_id, text):
    try:
        await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"Could not edit progress message: {e}")

def progress_hook(d, client: Client, chat_id: int, message_id: int, loop: asyncio.AbstractEventLoop):
    if d['status'] == 'downloading':
        now = time.time()
        key = f"{chat_id}-{message_id}"
        if now - progress_status.get(key, 0) < 2.5: return
        progress_status[key] = now
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        pct = (d.get('downloaded_bytes', 0) / total * 100) if total else 0
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        eta = int(d.get('eta') or 0)
        text = (f"<b>Downloading...</b>\n\n<code>{bar}</code> {pct:.1f}%\n\n"
                f"<b>Size:</b> {humanbytes(d.get('downloaded_bytes', 0))} / {humanbytes(total)}\n"
                f"<b>Speed:</b> {humanbytes(d.get('speed', 0))}/s | <b>ETA:</b> {eta}s")
        coro = edit_message_helper(client, chat_id, message_id, text)
        asyncio.run_coroutine_threadsafe(coro, loop)

def blocking_download(url, hook_with_args, quality, send_format):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    format_string = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
    
    ydl_opts_base = {
        "format": format_string,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "progress_hooks": [hook_with_args],
        "nocheckcertificate": True,
        "quiet": True,
        "postprocessors": [],
        "ffmpeg_location": ffmpeg_static.get_ffmpeg_path(),  # <-- NEW ADDITION 2 of 2: Tell yt-dlp where to find ffmpeg
    }

    if send_format == 'audio':
        ydl_opts_base['postprocessors'].append({'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'})
        ydl_opts_base['format'] = 'bestaudio/best'
    else:
        ydl_opts_base['postprocessors'].append({'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'})

    try:
        logger.info("Attempting download with browser impersonation...")
        ydl_opts_impersonate = ydl_opts_base.copy()
        ydl_opts_impersonate["impersonate"] = "chrome120"
        with yt_dlp.YoutubeDL(ydl_opts_impersonate) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        logger.info("Browser impersonation successful!")
    except Exception:
        logger.warning(f"Browser impersonation failed. Falling back to simple headers.")
        ydl_opts_fallback = ydl_opts_base.copy()
        ydl_opts_fallback["http_headers"] = HTTP_HEADERS
        with yt_dlp.YoutubeDL(ydl_opts_fallback) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        logger.info("Fallback with simple headers successful!")

    if send_format == 'audio':
        base, _ = os.path.splitext(filename); filename = base + ".mp3"
    else:
        base, _ = os.path.splitext(filename); filename = base + ".mp4"
    return filename, info

# --- BOT HANDLERS (No changes below this line) ---
@app.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    keyboard = [[
        InlineKeyboardButton("😎 Owner", url="https://t.me/FILMWORLDOFFICIA"),
        InlineKeyboardButton("🤩 Repo", callback_data="repo_button")
    ]]
    start_message = "<b>Welcome! I am a YouTube Downloader Bot.</b>\n\n<b>Send me any YouTube video URL and I will download and send it back to you.</b>"
    await message.reply_text(start_message, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.command("stats"))
async def stats_handler(_, message: Message):
    cpu = psutil.cpu_percent(interval=0.5); ram = psutil.virtual_memory().percent; disk = psutil.disk_usage("/").percent
    bot_uptime = time_formatter(time.time() - bot_start_time)
    stats_text = (f"⌬─────「 <b>Bot Stats</b> 」─────⌬\n\n<b>CPU Usage:</b> <code>{cpu}%</code>\n<b>RAM Usage:</b> <code>{ram}%</code>\n"
                  f"<b>DISK Usage:</b> <code>{disk}%</code>\n\n<b>Bot Uptime:</b> <code>{bot_uptime}</code>")
    await message.reply_text(stats_text)

@app.on_message(filters.command("settings"))
async def settings_handler(_, message: Message):
    chat_id = message.chat.id; user_settings.setdefault(chat_id, {'quality': '720', 'format': 'video'})
    quality = user_settings[chat_id]['quality']; format_type = user_settings[chat_id]['format']
    keyboard = [[
        InlineKeyboardButton(f"✅ Document" if format_type == 'document' else "📄 Document", callback_data="settings_format_document"),
        InlineKeyboardButton(f"✅ Video" if format_type == 'video' else "🎬 Video", callback_data="settings_format_video"),
        InlineKeyboardButton(f"✅ Audio" if format_type == 'audio' else "🎵 Audio", callback_data="settings_format_audio")],
       [InlineKeyboardButton(f"✅ 1080p" if quality == '1080' else "🔼 1080p", callback_data="settings_quality_1080"),
        InlineKeyboardButton(f"✅ 720p" if quality == '720' else "▶️ 720p", callback_data="settings_quality_720"),
        InlineKeyboardButton(f"✅ 480p" if quality == '480' else "🔽 480p", callback_data="settings_quality_480")]]
    settings_text = f"<b>Configure your download settings:</b>\n\n<b>Current Quality:</b> <code>{quality}p</code>\n<b>Current Format:</b> <code>{format_type}</code>"
    await message.reply_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_callback_query(filters.regex("^repo_button$"))
async def repo_callback_handler(_, query):
    await query.answer(); await query.message.reply_sticker(sticker=REPO_STICKER_ID)

@app.on_callback_query(filters.regex("^settings_"))
async def settings_callback_handler(_, query):
    chat_id = query.message.chat.id; _, category, value = query.data.split('_')
    user_settings.setdefault(chat_id, {'quality': '720', 'format': 'video'})[category] = value
    await query.answer(f"Set {category} to {value}")
    
    quality = user_settings[chat_id]['quality']; format_type = user_settings[chat_id]['format']
    keyboard = [[
        InlineKeyboardButton(f"✅ Document" if format_type == 'document' else "📄 Document", callback_data="settings_format_document"),
        InlineKeyboardButton(f"✅ Video" if format_type == 'video' else "🎬 Video", callback_data="settings_format_video"),
        InlineKeyboardButton(f"✅ Audio" if format_type == 'audio' else "🎵 Audio", callback_data="settings_format_audio")],
       [InlineKeyboardButton(f"✅ 1080p" if quality == '1080' else "🔼 1080p", callback_data="settings_quality_1080"),
        InlineKeyboardButton(f"✅ 720p" if quality == '720' else "▶️ 720p", callback_data="settings_quality_720"),
        InlineKeyboardButton(f"✅ 480p" if quality == '480' else "🔽 480p", callback_data="settings_quality_480")]]
    settings_text = f"<b>Configure your download settings:</b>\n\n<b>Current Quality:</b> <code>{quality}p</code>\n<b>Current Format:</b> <code>{format_type}</code>"
    try: await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))
    except MessageNotModified: pass

@app.on_message(filters.text & ~filters.command(["start", "stats", "settings"]))
async def download_handler(client: Client, message: Message):
    url = message.text.strip()
    if not url.startswith(("http://", "https")): await message.reply_text("<b>Please send a valid URL.</b>"); return
    
    chat_id = message.chat.id
    user_settings.setdefault(chat_id, {'quality': '720', 'format': 'video'})
    quality, send_format = user_settings[chat_id]['quality'], user_settings[chat_id]['format']

    sent_message = await message.reply_text("<b>Preparing download...</b>")
    
    loop = asyncio.get_running_loop()
    hook = partial(progress_hook, client=client, chat_id=sent_message.chat.id, message_id=sent_message.id, loop=loop)
    filename = None
    try:
        filename, info = await loop.run_in_executor(None, blocking_download, url, hook, quality, send_format)
        
        await sent_message.edit_text("<b>Download complete. Uploading...</b>")
        video_title = info.get("title", "Untitled Video")
        if send_format == 'audio': await client.send_audio(chat_id=chat_id, audio=filename, title=video_title)
        elif send_format == 'document': await client.send_document(chat_id=chat_id, document=filename, caption=video_title)
        else: await client.send_video(chat_id=chat_id, video=filename, supports_streaming=True, caption=video_title)
        await sent_message.delete()
    
    except Exception as e:
        tb_str = traceback.format_exc()
        logger.error(f"A critical exception occurred for URL {url}:\n{tb_str}")
        error_text = f"<b>A critical error occurred.</b>\n\n<b>TYPE:</b> <code>{type(e).__name__}</code>\n"
        if str(e): error_text += f"<b>MESSAGE:</b> <code>{str(e).splitlines()[-1]}</code>"
        try: await sent_message.edit_text(error_text)
        except Exception: pass
    finally:
        if filename and os.path.exists(filename):
            try: os.remove(filename)
            except OSError as e: logger.error(f"Error deleting file {filename}: {e}")

# --- SCRIPT EXECUTION ---
if __name__ == "__main__":
    logger.info("Starting bot...")
    app.run()
    logger.info("Bot has stopped.")
