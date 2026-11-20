"""Конфигурация бота: всё берётся из .env."""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _int(name, default):
    """Безопасное чтение int из окружения."""
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").strip().lstrip("@")
ADMIN_ID = _int("ADMIN_ID", 0)

_channel = (os.getenv("CHANNEL_ID") or "").strip()
# Числовой id канала приводим к int, @username оставляем строкой
CHANNEL_ID = int(_channel) if _channel.lstrip("-").isdigit() else _channel

STREAM_TRACK_LIMIT = _int("STREAM_TRACK_LIMIT", 2)
REFERRAL_BONUS_LIMIT = _int("REFERRAL_BONUS_LIMIT", 5)
ANTIFLOOD_SECONDS = _int("ANTIFLOOD_SECONDS", 10)
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or "").strip()

# Пути и технические лимиты
DB_PATH = os.path.join(BASE_DIR, "music_bot.db")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")
SUBMISSIONS_DIR = os.path.join(BASE_DIR, "submissions")
TEMP_DIR = os.path.join(BASE_DIR, "temp_uploads")
MAX_FILE_SIZE = _int("MAX_FILE_MB", 20) * 1024 * 1024
TEMP_TTL = 3600  # время жизни неподтверждённой загрузки, сек
