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
    'sending_doc': '📁 Sending as file...',
    'done': '✅ Done! ({size:.1f}MB)',
    'too_large': '📦 Too large: {size:.1f}MB (limit: 50MB)',
}

def get_file_size_mb(file_path: str) -> float:
    try:
        size_bytes = os.path.getsize(file_path)
        return size_bytes / (1024 * 1024)
    except Exception:
        return 0.0

def classify_download_error(error: Exception) -> str:
    error_str = str(error).lower()
    
    if 'private' in error_str:
        return "🔒 This video is private"
    if 'login' in error_str or 'authentication' in error_str or 'cookies' in error_str:
        return "🔑 This platform requires login — try sending a different link or a public video"
    elif 'not found' in error_str or '404' in error_str or 'deleted' in error_str:
        return "❌ Video not found — it may have been deleted"
    elif 'age' in error_str or 'sign in' in error_str or 'confirm your age' in error_str:
        return "🔞 Age-restricted content — can't download"
    elif 'geo' in error_str or 'country' in error_str or 'not available' in error_str or 'blocked' in error_str:
        return "🌍 This video is not available in our region"
    elif 'rate' in error_str or 'too many' in error_str or '429' in error_str:
        return "⏳ Too many requests — try again in a minute"
    elif 'timed out' in error_str or 'timeout' in error_str:
        return "⏳ Timeout — try again in a moment"
    elif 'copyright' in error_str or 'dmca' in error_str:
        return "🚫 This video was removed due to copyright"
    elif 'no video' in error_str or 'no media' in error_str:
        return "📝 This post doesn't contain a video"
    elif 'unsupported' in error_str or 'unable to extract' in error_str:
        return "🚫 This platform blocked the download — try again later"
    else:
        short = str(error)[:100]
        return f"⚠️ Download failed: {short}"

class SimpleVideoDownloader:
    def __init__(self):
        self.temp_dir = TEMP_DIRECTORY
        os.makedirs(self.temp_dir, exist_ok=True)

    def get_simple_ytdlp_options(self, output_path: str, format_string: str) -> dict:
        opts = {
            'outtmpl': output_path,
            'format': format_string,
            'writeinfojson': False,
            'writesubtitles': False,
            'writethumbnail': False,
            'writeautomaticsub': False,
            'ignoreerrors': False,
            'no_warnings': False,
            'extract_flat': False,
            'http_headers': {'User-Agent': get_random_user_agent()},
            'socket_timeout': 30,
            'retries': 5,  # ✅ УЛУЧШЕНО: больше попыток
            'fragment_retries': 5,  # ✅ УЛУЧШЕНО
            'file_access_retries': 3,
            'extractor_retries': 3,
            'concurrent_fragment_downloads': 4,
            'noprogress': True,
            'quiet': True,
            'no_color': True,
            'geo_bypass': True,
            'geo_bypass_country': 'US',  # ✅ NEW: Эмулируем США
            'nocheckcertificate': True,
            'sleep_interval': 0.5,  # ✅ NEW: Пауза между запросами
            'sleep_interval_requests': 0.5,  # ✅ NEW
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'web', 'mweb'],  # ✅ NEW: mweb
                    'skip': ['hls', 'dash'],
                },
                'tiktok': {
                    'api_hostname': [
                        'api22-normal-c-useast2a.tiktokv.com',
                        'api19-normal-c-useast1a.tiktokv.com',
                        'api16-normal-c-useast1a.tiktokv.com',
                    ],
                },
            },
        }

        if COOKIES_ENABLED:
            opts['cookiefile'] = COOKIES_FILE

        return opts

    async def download_video(self, url: str, platform_name: str, user_id: int) -> Optional[str]:
        request_id = str(uuid.uuid4())[:8]
        filename = f"{platform_name.lower()}_{user_id}_{request_id}.%(ext)s"
        output_path = os.path.join(self.temp_dir, filename)
        base_path = output_path.replace('.%(ext)s', '')
        last_error = None

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
                last_error = e

        if last_error:
            raise last_error
        raise Exception("Download failed")

async def detect_platform_and_process(message, bot, url, progress_msg=None):
    for domain, platform_name in PLATFORM_IDENTIFIERS.items():
        if domain in url:
            downloader = SimpleVideoDownloader()
            await downloader.download_video(url, platform_name, message.from_user.id)
            return True
    return False
