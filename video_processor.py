import asyncio
import logging
import os
import re
import uuid
from typing import Optional

import instaloader
from aiogram.types import FSInputFile

from config import TEMP_DIRECTORY
from utils.common_utils import safe_edit_message
from utils.cleanup import cleanup_temp_directory

logger = logging.getLogger(__name__)

TELEGRAM_VIDEO_SIZE_LIMIT_MB = 50

PROGRESS_MESSAGES = {
    'downloading': '⬇️ Downloading from {platform}...',
    'sending_video': '📤 Sending video...',
    'done': '✅ Done! ({size:.1f}MB)',
    'too_large': '📦 Too large: {size:.1f}MB (limit: 50MB)',
}

def get_file_size_mb(file_path: str) -> float:
    try:
        return os.path.getsize(file_path) / (1024 * 1024)
    except:
        return 0.0

def extract_shortcode(url: str) -> Optional[str]:
    match = re.search(r'/reel/([^/?]+)|/p/([^/?]+)|/tv/([^/?]+)', url)
    if match:
        return match.group(1) or match.group(2) or match.group(3)
    return None

def download_instagram_video(url: str, output_dir: str) -> Optional[str]:
    shortcode = extract_shortcode(url)
    if not shortcode:
        raise Exception("Не удалось извлечь shortcode из ссылки")

    L = instaloader.Instaloader(
        dirname_pattern=output_dir,
        filename_pattern=f"{shortcode}_%(shortcode)s",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern='',
        quiet=True,
    )

    username = os.getenv('INSTAGRAM_USERNAME')
    password = os.getenv('INSTAGRAM_PASSWORD')
    if username and password:
        try:
            L.login(username, password)
            logger.info("Instagram: logged in successfully")
        except Exception as e:
            logger.warning(f"Instagram login failed: {e}")

    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=output_dir)

    # Ищем скачанный mp4
    for f in os.listdir(output_dir):
        if f.endswith('.mp4'):
            return os.path.join(output_dir, f)

    return None

class SimpleVideoDownloader:
    def __init__(self):
        self.temp_dir = TEMP_DIRECTORY
        os.makedirs(self.temp_dir, exist_ok=True)

    async def download_video(self, url: str, platform_name: str, user_id: int) -> Optional[str]:
        request_id = str(uuid.uuid4())[:8]
        output_dir = os.path.join(self.temp_dir, f"{user_id}_{request_id}")
        os.makedirs(output_dir, exist_ok=True)

        try:
            loop = asyncio.get_event_loop()
            downloaded_path = await loop.run_in_executor(
                None, download_instagram_video, url, output_dir
            )
            return downloaded_path
        except Exception as e:
            logger.error(f"Download error: {e}")
            raise

async def process_social_media_video(message, bot, url, platform_name, progress_msg=None):
    downloader = SimpleVideoDownloader()
    temp_video_path = None
    output_dir = None

    try:
        if progress_msg:
            await safe_edit_message(progress_msg, PROGRESS_MESSAGES['downloading'].format(platform=platform_name))

        temp_video_path = await downloader.download_video(url, platform_name, message.from_user.id)

        if not temp_video_path:
            raise Exception("Не удалось скачать видео")

        output_dir = os.path.dirname(temp_video_path)
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

        try:
            video_file = FSInputFile(temp_video_path)
            await bot.send_video(chat_id=message.chat.id, video=video_file, supports_streaming=True)
        except:
            doc_file = FSInputFile(temp_video_path, filename=f"instagram_video.mp4")
            await bot.send_document(chat_id=message.chat.id, document=doc_file)

        if progress_msg:
            await safe_edit_message(progress_msg, PROGRESS_MESSAGES['done'].format(size=file_size_mb))

    except Exception as e:
        logger.error(f"Error: {e}")
        error_text = "⚠️ Не удалось скачать. Попробуй другую ссылку."
        if "login" in str(e).lower() or "auth" in str(e).lower():
            error_text = "🔒 Instagram требует авторизацию. Обратитесь к администратору."
        if progress_msg:
            await safe_edit_message(progress_msg, error_text)
        else:
            await bot.send_message(message.chat.id, error_text)

    finally:
        # Удаляем временную папку
        if output_dir and os.path.exists(output_dir):
            import shutil
            try:
                shutil.rmtree(output_dir)
            except:
                pass


async def detect_platform_and_process(message, bot, url, progress_msg=None):
    if "instagram.com" in url:
        await process_social_media_video(message, bot, url, "Instagram", progress_msg)
        return True

    msg = "⚠️ Поддерживается только Instagram.\nОтправь ссылку на пост, рилс или сторис."
    if progress_msg:
        await safe_edit_message(progress_msg, msg)
    else:
        await bot.send_message(message.chat.id, msg)
    return False
