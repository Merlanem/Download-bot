import asyncio
import logging
import os
import uuid
from typing import Optional

import aiohttp
import yt_dlp
from aiogram.types import FSInputFile

from config import TEMP_DIRECTORY, PLATFORM_IDENTIFIERS, COOKIES_FILE, COOKIES_ENABLED
from utils.user_agent_utils import get_random_user_agent
from utils.common_utils import safe_edit_message
from utils.cleanup import cleanup_temp_directory
from extractors import get_extractor

logger = logging.getLogger(__name__)

TELEGRAM_VIDEO_SIZE_LIMIT_MB = 50

FORMAT_ATTEMPTS = [
    'best[ext=mp4][filesize<50M]/best[ext=mp4]/best[filesize<50M]',
    'best/bestvideo+bestaudio',
    'worst',
]

PROGRESS_MESSAGES = {
    'downloading': '⬇️ Downloading from {platform}...',
    'processing': '⚙️ Processing video...',
    'sending_video': '📤 Sending video...',
    'done': '✅ Done! ({size:.1f}MB)',
    'too_large': '📦 Too large: {size:.1f}MB (limit: 50MB)',
}

def get_file_size_mb(file_path: str) -> float:
    try:
        return os.path.getsize(file_path) / (1024 * 1024)
    except:
        return 0.0

def classify_download_error(error: Exception) -> str:
    error_str = str(error).lower()
    if 'geo' in error_str or 'country' in error_str:
        return "🌍 Video not available in your region"
    elif 'age' in error_str:
        return "🔞 Age-restricted"
    elif 'timed out' in error_str or 'timeout' in error_str:
        return "⏳ Timeout — try again"
    elif 'private' in error_str:
        return "🔒 Video is private"
    elif 'not found' in error_str or '404' in error_str:
        return "❌ Video not found"
    else:
        return f"⚠️ Error: {str(error)[:80]}"

class SimpleVideoDownloader:
    def __init__(self):
        self.temp_dir = TEMP_DIRECTORY
        os.makedirs(self.temp_dir, exist_ok=True)

    def get_simple_ytdlp_options(self, output_path: str, format_string: str) -> dict:
        return {
            'outtmpl': output_path,
            'format': format_string,
            'writeinfojson': False,
            'writesubtitles': False,
            'writethumbnail': False,
            'ignoreerrors': False,
            'http_headers': {'User-Agent': get_random_user_agent()},
            'socket_timeout': 30,
            'retries': 5,
            'fragment_retries': 5,
            'file_access_retries': 3,
            'noprogress': True,
            'quiet': True,
            'no_color': True,
            'geo_bypass': True,
            'geo_bypass_country': 'US',
            'sleep_interval': 0.5,
            'extractor_args': {
    'instagram': {},
},
            'cookiefile': COOKIES_FILE if COOKIES_ENABLED else None,
        }

    async def download_video(self, url: str, platform_name: str, user_id: int) -> Optional[str]:
        request_id = str(uuid.uuid4())[:8]
        filename = f"{platform_name.lower()}_{user_id}_{request_id}.%(ext)s"
        output_path = os.path.join(self.temp_dir, filename)
        base_path = output_path.replace('.%(ext)s', '')

        for attempt, format_string in enumerate(FORMAT_ATTEMPTS, 1):
            try:
                options = self.get_simple_ytdlp_options(output_path, format_string)

                def run_download():
                    with yt_dlp.YoutubeDL(options) as ydl:
                        ydl.download([url])
                    for ext in ['.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.m4v']:
                        potential_path = base_path + ext
                        if os.path.exists(potential_path):
                            return potential_path
                    return None

                loop = asyncio.get_event_loop()
                downloaded_path = await loop.run_in_executor(None, run_download)

                if downloaded_path and os.path.exists(downloaded_path):
                    return downloaded_path

            except Exception as e:
                logger.warning(f"Attempt {attempt} failed: {e}")
                continue

        raise Exception("Download failed after all attempts")

async def process_social_media_video(message, bot, url, platform_name, progress_msg=None):
    downloader = SimpleVideoDownloader()
    temp_video_path = None

    try:
        if progress_msg:
            await safe_edit_message(progress_msg, PROGRESS_MESSAGES['downloading'].format(platform=platform_name))

        temp_video_path = await downloader.download_video(url, platform_name, message.from_user.id)

        if not temp_video_path:
            raise Exception("Failed to download")

        file_size_mb = get_file_size_mb(temp_video_path)

        if file_size_mb > TELEGRAM_VIDEO_SIZE_LIMIT_MB:
            msg = PROGRESS_MESSAGES['too_large'].format(size=file_size_mb)
            if progress_msg:
                await safe_edit_message(progress_msg, msg)
            else:
                await bot.send_message(message.chat.id, msg)
            return

        if progress_msg:
            await safe_edit_message(progress_msg, PROGRESS_MESSAGES['sending_video'])

        # Send video
        try:
            video_file = FSInputFile(temp_video_path)
            await bot.send_video(chat_id=message.chat.id, video=video_file, supports_streaming=True)
        except:
            # Fallback: send as document
            doc_file = FSInputFile(temp_video_path, filename=f"{platform_name.lower()}_video.mp4")
            await bot.send_document(chat_id=message.chat.id, document=doc_file)

        if progress_msg:
            await safe_edit_message(progress_msg, PROGRESS_MESSAGES['done'].format(size=file_size_mb))

    except yt_dlp.utils.DownloadError as e:
        error_msg = classify_download_error(e)
        if progress_msg:
            await safe_edit_message(progress_msg, error_msg)
        else:
            await bot.send_message(message.chat.id, error_msg)

    except Exception as e:
        logger.error(f"Error: {e}")
        if progress_msg:
            await safe_edit_message(progress_msg, "⚠️ Something went wrong. Try again.")
        else:
            await bot.send_message(message.chat.id, "⚠️ Something went wrong. Try again.")

    finally:
        if temp_video_path and os.path.exists(temp_video_path):
            try:
                os.unlink(temp_video_path)
            except:
                pass


async def detect_platform_and_process(message, bot, url, progress_msg=None):
    # Проверяем только Instagram
    if "instagram.com" in url:
        await process_social_media_video(message, bot, url, "Instagram", progress_msg)
        return True

    # Всё остальное — игнорируем с сообщением
    msg = "⚠️ Поддерживается только Instagram.\nОтправь ссылку на пост, рилс или сторис."
    if progress_msg:
        await safe_edit_message(progress_msg, msg)
    else:
        await bot.send_message(message.chat.id, msg)
    return False
