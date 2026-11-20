"""Чтение ID3-тегов mp3."""
import logging

from mutagen.id3 import ID3

logger = logging.getLogger(__name__)


def read_tags(path):
    """Возвращает (title, artist_tag). При ошибке — ("", None)."""
    try:
        tags = ID3(path)
        tit2 = tags.get("TIT2")
        tpe1 = tags.get("TPE1")
        title = str(tit2.text[0]).strip() if tit2 and tit2.text else ""
        artist = str(tpe1.text[0]).strip() if tpe1 and tpe1.text else ""
        return title, (artist or None)
    except Exception as e:  # нет тегов, битый файл и т.п.
        logger.error("Не удалось прочитать ID3 (%s): %s", path, e)
        return "", None
