"""Парсинг ссылок, форматирование, антифлуд."""
import json
import logging
import re
import threading
import time
import urllib.request

import config
import database

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s]+")

# Антифлуд в памяти: {user_id: last_action_time}
_flood = {}
_flood_lock = threading.Lock()


def extract_url(text):
    """Первая http(s)-ссылка из текста или None."""
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)»\"'")


def format_artist(pseudonym, artist_tag):
    """'{pseudonym} / {tag}' или '{pseudonym}'."""
    tag = (artist_tag or "").strip()
    if tag and tag.lower() != (pseudonym or "").strip().lower():
        return f"{pseudonym} / {tag}"
    return pseudonym or ""


def is_flooding(user_id):
    """True — действие надо тихо проигнорировать. Админ не подпадает."""
    if user_id == config.ADMIN_ID:
        return False
    now = time.time()
    with _flood_lock:
        last = _flood.get(user_id)
        if last is not None and now - last < config.ANTIFLOOD_SECONDS:
            return True
        _flood[user_id] = now
    try:
        database.touch_user(user_id)
    except Exception:
        logger.error("Не удалось обновить last_action_at", exc_info=True)
    return False


def parse_scores(parts):
    """5 целых чисел 1..10 -> список. Иначе ValueError."""
    if len(parts) != 5:
        raise ValueError("нужно 5 оценок")
    scores = [int(p) for p in parts]
    if any(s < 1 or s > 10 for s in scores):
        raise ValueError("оценки должны быть 1..10")
    return scores


def fmt_scores_line(scores):
    b, s, st, d, v = scores
    return f"Бит: {b} | Структура: {s} | Стиль: {st} | Подача: {d} | Вайб: {v}"


def fmt_scores_short(scores):
    b, s, st, d, v = scores
    return f"Б:{b} С:{s} Ст:{st} П:{d} В:{v}"


def track_scores(t):
    return [t["beat"], t["structure"], t["style"], t["delivery"], t["vibe"]]


def fmt_avg(avg):
    return f"{avg:.1f}/50" if avg is not None else "—"


def fmt_user(username, user_id):
    return f"@{username}" if username else f"id{user_id}"


def plural_tracks(n):
    if n % 10 == 1 and n % 100 != 11:
        word = "трек"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "трека"
    else:
        word = "треков"
    return f"{n} {word}"


def format_top_lines(top_data):
    return "\n".join(
        f"{i['place']}. {i['total_score']}/50 — {i['title']} — {i['pseudonym']}" for i in top_data
    )


def format_channel_post(top3, date_str):
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[idx]} {i['total_score']}/50 — «{i['title']}» — {i['pseudonym']}"
        for idx, i in enumerate(top3)
    ]
    return (
        f"🏆 ТОП-3 ТРЕКА ЗА СТРИМ ({date_str})\n\n"
        + "\n".join(lines)
        + "\n\nОценки: бит, структура, стиль, подача, вайб"
    )


def send_long(bot, chat_id, text):
    """Отправка длинного текста частями (лимит Telegram 4096)."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 4000:
            bot.send_message(chat_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        bot.send_message(chat_id, chunk)


def post_webhook(payload):
    """POST JSON на WEBHOOK_URL в фоне (если задан)."""
    if not config.WEBHOOK_URL:
        return

    def _send():
        try:
            req = urllib.request.Request(
                config.WEBHOOK_URL,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).close()
            logger.info("Webhook отправлен (трек #%s)", payload.get("track_id"))
        except Exception:
            logger.error("Ошибка отправки webhook", exc_info=True)

    threading.Thread(target=_send, daemon=True).start()
