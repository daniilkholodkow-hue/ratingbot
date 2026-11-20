"""Логика ачивок."""
import logging

import database as db
from utils import plural_tracks

logger = logging.getLogger(__name__)

_bot = None


def set_bot(bot):
    """Передаём экземпляр бота для уведомлений."""
    global _bot
    _bot = bot


def _stable_streak(user_id):
    """Сколько последних (до 3) оценённых треков подряд имеют >= 35."""
    streak = 0
    for s in db.get_last_rated_scores(user_id, 3):
        if s is not None and s >= 35:
            streak += 1
        else:
            break
    return streak


def _condition(user_id, code):
    u = db.get_user(user_id)
    if not u:
        return False
    if code == "top1":
        return u["top1_count"] >= 1
    if code == "champion_3":
        return u["top1_count"] >= 3
    if code == "score_45":
        return db.has_score_at_least(user_id, 45)
    if code == "stable_3":
        return _stable_streak(user_id) >= 3
    if code == "active_10":
        return u["total_tracks"] >= 10
    if code == "legend_25":
        return u["total_tracks"] >= 25
    return False


def check_achievements(user_id, code):
    """Выдаёт ачивку, если условие выполнено и её ещё нет. True — выдана."""
    try:
        if db.has_achievement(user_id, code):
            return False
        if not _condition(user_id, code):
            return False
        if not db.grant_achievement(user_id, code):
            return False
        info = db.get_achievement(code)
        logger.info("Ачивка %s выдана user_id=%s", code, user_id)
        if info and _bot:
            try:
                _bot.send_message(
                    user_id,
                    f"🏅 Новая ачивка: {info['emoji']} {info['title']}!\n{info['description']}",
                )
            except Exception:
                logger.error("Не удалось уведомить об ачивке user_id=%s", user_id, exc_info=True)
        return True
    except Exception:
        logger.error("Ошибка check_achievements(%s, %s)", user_id, code, exc_info=True)
        return False


def earned_emojis(user_id):
    codes = db.get_user_achievement_codes(user_id)
    return [a["emoji"] for a in db.get_catalog() if a["code"] in codes]


def _hint(code, u):
    total, top1 = u["total_tracks"], u["top1_count"]
    if code == "top1":
        return "стань #1 в топе стрима"
    if code == "score_45":
        return "получи оценку 45+ за трек"
    if code == "stable_3":
        return f"3 трека подряд с оценкой 35+ (серия: {_stable_streak(u['user_id'])}/3)"
    if code == "active_10":
        return f"осталось {plural_tracks(max(0, 10 - total))}"
    if code == "legend_25":
        return f"осталось {plural_tracks(max(0, 25 - total))}"
    if code == "champion_3":
        return f"осталось раз в топ-1: {max(0, 3 - top1)}"
    return ""


def get_progress_lines(user_id, detailed=False):
    """Строки '✅ ...' / '❌ ... — подсказка' по всем ачивкам."""
    u = db.get_user(user_id)
    if not u:
        return []
    have = db.get_user_achievement_codes(user_id)
    lines = []
    for a in db.get_catalog():
        base = f"{a['emoji']} {a['title']}"
        if a["code"] in have:
            lines.append(f"✅ {base}" + (f" — {a['description']}" if detailed else ""))
        else:
            lines.append(f"❌ {base} — {_hint(a['code'], u)}")
    return lines
