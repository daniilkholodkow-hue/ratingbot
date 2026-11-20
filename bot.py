"""Music Rating Bot — точка входа и хендлеры."""
import functools
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
from datetime import datetime

import telebot
from telebot import types

import achievements
import config
import database as db
import keyboards as kb
import utils
from audio_tags import read_tags

# ---------- Логирование ----------
os.makedirs(config.SUBMISSIONS_DIR, exist_ok=True)
os.makedirs(config.TEMP_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("bot")

if not config.BOT_TOKEN:
    sys.exit("BOT_TOKEN не задан. Заполни .env (см. .env.example).")

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode=None)

# Временные загрузки: {temp_id: {...}}, TTL 1 час
temp_uploads = {}
temp_lock = threading.Lock()

STATUS_TEXT = {"pending": "⏳ В очереди", "skipped": "❌ Отклонен"}


# ---------- Общие помощники ----------

def is_admin(user_id):
    return bool(config.ADMIN_ID) and user_id == config.ADMIN_ID


def guarded(fn):
    """Ловит исключения хендлера, логирует и сообщает пользователю."""

    @functools.wraps(fn)
    def wrapper(obj, *args, **kwargs):
        try:
            return fn(obj, *args, **kwargs)
        except Exception:
            logger.error("Ошибка в %s", fn.__name__, exc_info=True)
            try:
                target = getattr(obj, "message", None) or obj
                bot.send_message(target.chat.id, "⚠️ Что-то пошло не так. Попробуй ещё раз позже.")
            except Exception:
                pass

    return wrapper


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.error("Не удалось отправить сообщение chat_id=%s", chat_id, exc_info=True)
        return None


def reg_user(user):
    """Регистрирует/обновляет юзера. True — новый."""
    return db.ensure_user(user.id, user.username)


def ask(chat_id, text, handler, *args):
    """Задать вопрос и ждать ответ (FSM через register_next_step_handler)."""
    msg = bot.send_message(chat_id, text)
    bot.register_next_step_handler(msg, handler, *args)


def parse_step_text(message, max_len, min_len=1, allow_skip=False):
    """Разбор текстового шага FSM -> ('ok'|'skip'|'cancel'|'bad', text)."""
    text = (message.text or "").strip() if message.content_type == "text" else ""
    low = text.lower()
    if low == "/cancel":
        return "cancel", None
    if allow_skip and low == "/skip":
        return "skip", None
    if not text or text.startswith("/") or not (min_len <= len(text) <= max_len):
        return "bad", None
    return "ok", text


def id_from(data, prefix):
    try:
        return int(data[len(prefix):])
    except ValueError:
        return None


def drop_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        logger.error("Не удалось удалить файл %s", path, exc_info=True)


# ---------- Временные загрузки ----------

def _cleanup_locked():
    now = time.time()
    expired = [k for k, v in temp_uploads.items() if now - v["created"] > config.TEMP_TTL]
    return [temp_uploads.pop(k)["path"] for k in expired]


def cleanup_temp():
    with temp_lock:
        paths = _cleanup_locked()
    for p in paths:
        drop_file(p)
    if paths:
        logger.info("Очищено просроченных загрузок: %d", len(paths))


def cleanup_loop():
    while True:
        time.sleep(600)
        try:
            cleanup_temp()
        except Exception:
            logger.error("Ошибка фоновой очистки", exc_info=True)


def temp_put(data):
    temp_id = secrets.token_hex(4)
    with temp_lock:
        paths = _cleanup_locked()
        temp_uploads[temp_id] = {**data, "created": time.time()}
    for p in paths:
        drop_file(p)
    return temp_id


def temp_get(temp_id):
    with temp_lock:
        paths = _cleanup_locked()
        entry = temp_uploads.get(temp_id)
    for p in paths:
        drop_file(p)
    return entry


def temp_pop(temp_id):
    with temp_lock:
        return temp_uploads.pop(temp_id, None)


def temp_update(temp_id, **kwargs):
    with temp_lock:
        if temp_id in temp_uploads:
            temp_uploads[temp_id].update(kwargs)


# ---------- Проверки приёма треков ----------

def check_submission_allowed(user_id):
    """Общие проверки a–d. Возвращает (user, error_text, markup)."""
    user = db.get_user(user_id)
    if not user:
        return None, "Нажми /start", None
    if user["is_banned"]:
        return None, "🚫 Ты заблокирован и не можешь отправлять треки.", None
    if not user["pseudonym"]:
        return None, "Сначала заполни профиль.", kb.fill_profile_kb()
    if not db.get_active_stream():
        return None, "Стрим не активен. Дождись начала.", None
    limit = config.STREAM_TRACK_LIMIT + user["bonus_limit"]
    if user["stream_track_count"] >= limit:
        return None, f"Лимит: {utils.plural_tracks(limit)} за стрим. Жди следующий стрим.", None
    return user, None, None


def forward_to_admin(track_id, user, title, artist_tag, url=None, file_id=None, file_kind=None):
    """Пересылка трека стримеру с кнопками."""
    text = (
        f"#{track_id} | {utils.format_artist(user['pseudonym'], artist_tag)} | {title} | "
        f"{utils.fmt_user(user['username'], user['user_id'])}"
    )
    if url:
        text += f"\n🔗 {url}"
    markup = kb.admin_track_kb(track_id)
    try:
        if file_id and file_kind == "audio":
            bot.send_audio(config.ADMIN_ID, file_id, caption=text, reply_markup=markup)
        elif file_id:
            bot.send_document(config.ADMIN_ID, file_id, caption=text, reply_markup=markup)
        else:
            bot.send_message(config.ADMIN_ID, text, reply_markup=markup)
    except Exception:
        logger.error("Не удалось переслать трек #%s админу", track_id, exc_info=True)
        safe_send(config.ADMIN_ID, text, reply_markup=markup)


def finalize_track(chat_id, user, title, artist_tag, source, file_id=None, file_path=None,
                   url=None, file_kind=None):
    """Запись трека в БД, ответ зрителю, пересылка админу, ачивки."""
    track_id = db.add_track(
        user["user_id"], user["pseudonym"], title, artist_tag, source, file_id, file_path, url
    )
    logger.info("Принят трек #%s (%s) от user_id=%s", track_id, source, user["user_id"])
    pos = db.get_queue_position(track_id)
    bot.send_message(chat_id, f"Трек #{track_id} «{title}» принят! Позиция: {pos}.")
    forward_to_admin(track_id, user, title, artist_tag, url, file_id, file_kind)
    achievements.check_achievements(user["user_id"], "active_10")
    achievements.check_achievements(user["user_id"], "legend_25")
    return track_id


# ---------- /start и меню ----------

HOWTO_TEXT = (
    "ℹ️ Как скинуть трек:\n"
    "1. Заполни профиль (/profile).\n"
    "2. Дождись начала стрима.\n"
    "3. Отправь mp3-файл (до 20 МБ) или ссылку на SoundCloud.\n"
    "4. Подтверди название — трек попадёт в очередь.\n\n"
    "Лимит треков за стрим растёт за приглашённых друзей: /referral"
)


@bot.message_handler(commands=["start"])
@guarded
def cmd_start(message):
    user = message.from_user
    is_new = reg_user(user)
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].startswith("ref_") and is_new:
        try:
            ref_id = int(parts[1][4:])
        except ValueError:
            ref_id = None
        # Самоприглашение игнорируем; реферер должен существовать
        if ref_id and ref_id != user.id and db.get_user(ref_id):
            if db.add_referral(ref_id, user.id):
                logger.info("Реферал: %s -> %s", ref_id, user.id)
                safe_send(
                    ref_id,
                    f"🤝 По твоей ссылке пришел {utils.fmt_user(user.username, user.id)}! "
                    "Он получит лимит после заполнения профиля.",
                )
    bot.send_message(
        message.chat.id,
        "🎧 Привет! Это бот для оценки музыки стримера.\n"
        "Отправляй свои треки (mp3 или ссылку SoundCloud), получай оценки по 5 шкалам, "
        "собирай ачивки и попадай в топ!",
        reply_markup=kb.start_kb(),
    )


@bot.callback_query_handler(func=lambda c: c.data in ("menu_profile", "menu_top", "menu_howto",
                                                        "menu_referral", "fill_profile"))
@guarded
def cb_menu(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    uid = call.from_user.id
    reg_user(call.from_user)
    if call.data == "menu_howto":
        bot.send_message(chat_id, HOWTO_TEXT)
    elif call.data == "menu_top":
        send_top(chat_id)
    elif call.data == "fill_profile":
        start_profile_fill(chat_id)
    elif call.data == "menu_profile":
        if not utils.is_flooding(uid):
            send_profile(chat_id, uid)
    elif call.data == "menu_referral":
        if not utils.is_flooding(uid):
            send_referral(chat_id, uid)


# ---------- Профиль ----------

def send_profile(chat_id, viewer_id, target_username=None):
    own = target_username is None
    target = db.get_user(viewer_id) if own else db.get_user_by_username(target_username)
    if not target:
        bot.send_message(chat_id, "Профиль не найден.")
        return
    if not target["pseudonym"]:
        if own:
            bot.send_message(chat_id, "Профиль не заполнен.", reply_markup=kb.fill_profile_kb())
        else:
            bot.send_message(chat_id, "Пользователь ещё не заполнил профиль.")
        return
    st = db.calculate_user_stats(target["user_id"])
    emojis = " ".join(achievements.earned_emojis(target["user_id"])) or "пока нет"
    lines = [
        f"👤 {target['pseudonym']}",
        f"📍 {target['city'] or '—'}",
        f"📝 {target['bio'] or '—'}",
        f"🎵 Треков: {st['total_tracks']} | ⭐ Средний балл: {utils.fmt_avg(st['avg_score'])} "
        f"| 🔥 Раз в топ-1: {st['top1_count']}",
        f"🏅 Ачивки: {emojis}",
    ]
    markup = None
    if own:
        lines.append(f"👥 Приглашено: {st['invited']}")
        lines.append(f"🎚 Лимит на стрим: {config.STREAM_TRACK_LIMIT + target['bonus_limit']}")
        markup = kb.fill_profile_kb("✏️ Изменить профиль")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)


@bot.message_handler(commands=["profile"])
@guarded
def cmd_profile(message):
    reg_user(message.from_user)
    if utils.is_flooding(message.from_user.id):
        return
    parts = message.text.split()
    target = parts[1].lstrip("@") if len(parts) > 1 else None
    send_profile(message.chat.id, message.from_user.id, target)


def start_profile_fill(chat_id):
    ask(chat_id, "✏️ Шаг 1/3. Придумай псевдоним (2–40 символов).\n/cancel — отмена", step_pseudonym)


@guarded
def step_pseudonym(message):
    st, text = parse_step_text(message, 40, 2)
    if st == "cancel":
        bot.send_message(message.chat.id, "Отменено.")
    elif st == "ok":
        ask(message.chat.id, "Шаг 2/3. Твой город (/skip — пропустить).", step_city, text)
    else:
        ask(message.chat.id, "Псевдоним: 2–40 символов, без команд. Попробуй ещё раз.", step_pseudonym)


@guarded
def step_city(message, pseudonym):
    st, text = parse_step_text(message, 60, allow_skip=True)
    if st == "cancel":
        bot.send_message(message.chat.id, "Отменено.")
    elif st in ("ok", "skip"):
        ask(message.chat.id, "Шаг 3/3. Коротко о себе (до 300 символов, /skip — пропустить).",
            step_bio, pseudonym, text)
    else:
        ask(message.chat.id, "Город: до 60 символов (или /skip).", step_city, pseudonym)


@guarded
def step_bio(message, pseudonym, city):
    st, text = parse_step_text(message, 300, allow_skip=True)
    if st == "cancel":
        bot.send_message(message.chat.id, "Отменено.")
        return
    if st == "bad":
        ask(message.chat.id, "О себе: до 300 символов (или /skip).", step_bio, pseudonym, city)
        return
    uid = message.from_user.id
    db.set_profile(uid, pseudonym, city, text)
    logger.info("Профиль заполнен user_id=%s", uid)
    bot.send_message(message.chat.id, "✅ Профиль сохранён!")
    # Подтверждение реферала: бонус рефереру
    referrer_id = db.confirm_referral(uid)
    if referrer_id:
        who = utils.fmt_user(message.from_user.username, uid)
        if db.add_referral_bonus(referrer_id):
            safe_send(referrer_id, f"🤝 {who} заполнил профиль! +1 трек к лимиту навсегда.")
        else:
            safe_send(referrer_id,
                      f"🤝 {who} заполнил профиль! Максимум бонусных треков уже достигнут.")
    send_profile(message.chat.id, uid)


# ---------- Статистика, рефералка, ачивки ----------

@bot.message_handler(commands=["my_stats"])
@guarded
def cmd_my_stats(message):
    reg_user(message.from_user)
    uid = message.from_user.id
    if utils.is_flooding(uid):
        return
    st = db.calculate_user_stats(uid)
    best = f"«{st['best_title']}» — {st['best_score']}/50" if st["best_title"] else "—"
    rank = f"#{st['rank']} из {st['ranked_total']}" if st["rank"] else "—"
    text = (
        "📊 Твоя статистика:\n"
        f"🎵 Треков отправлено: {st['total_tracks']}\n"
        f"⭐ Средний балл: {utils.fmt_avg(st['avg_score'])}\n"
        f"🏆 Лучший трек: {best}\n"
        f"📈 Место в общем зачёте: {rank}\n"
        f"🔥 Раз в топ-1: {st['top1_count']}\n"
        f"👥 Приглашено друзей: {st['invited']}\n"
        f"🎚 Бонусных треков: +{st['bonus_limit']}\n\n"
        "🏅 Ачивки:\n" + "\n".join(achievements.get_progress_lines(uid))
    )
    bot.send_message(message.chat.id, text)


def send_referral(chat_id, uid):
    u = db.get_user(uid)
    if not u:
        return
    if not config.BOT_USERNAME:
        bot.send_message(chat_id, "Реферальная система временно недоступна.")
        return
    bot.send_message(
        chat_id,
        "👥 Твоя реферальная ссылка:\n"
        f"t.me/{config.BOT_USERNAME}?start=ref_{uid}\n\n"
        "За каждого друга, который заполнит профиль:\n"
        f"+1 трек к лимиту НАВСЕГДА (макс. +{config.REFERRAL_BONUS_LIMIT})\n\n"
        f"Приглашено: {db.count_referrals(uid)}\n"
        f"Бонусных треков: +{u['bonus_limit']}",
    )


@bot.message_handler(commands=["referral"])
@guarded
def cmd_referral(message):
    reg_user(message.from_user)
    if utils.is_flooding(message.from_user.id):
        return
    send_referral(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["achievements"])
@guarded
def cmd_achievements(message):
    reg_user(message.from_user)
    uid = message.from_user.id
    if utils.is_flooding(uid):
        return
    bot.send_message(message.chat.id,
                     "🏅 Твои ачивки:\n\n" + "\n".join(achievements.get_progress_lines(uid, True)))


@bot.message_handler(commands=["my_tracks"])
@guarded
def cmd_my_tracks(message):
    reg_user(message.from_user)
    tracks = db.get_user_tracks(message.from_user.id)
    if not tracks:
        bot.send_message(message.chat.id, "Ты ещё не отправлял треки.")
        return
    lines = []
    for t in tracks:
        if t["status"] == "rated":
            st = f"{t['total_score']}/50 ({utils.fmt_scores_short(utils.track_scores(t))})"
        else:
            st = STATUS_TEXT.get(t["status"], t["status"])
        lines.append(f"#{t['id']} — {t['title']} — {st}")
    utils.send_long(bot, message.chat.id, "\n".join(lines))


# ---------- Приём mp3 ----------

def is_mp3(name, mime):
    name = (name or "").lower()
    if name:
        return name.endswith(".mp3")
    return (mime or "").lower() in ("audio/mpeg", "audio/mp3")


def show_confirm_card(chat_id, temp_id):
    e = temp_get(temp_id)
    if not e:
        bot.send_message(chat_id, "Время вышло, отправь файл заново.")
        return
    bot.send_message(
        chat_id,
        "📻 Считано из файла:\n"
        f"Название: {e['title']}\n"
        f"Исполнитель: {utils.format_artist(e['pseudonym'], e['artist_tag'])}\n"
        "Всё верно?",
        reply_markup=kb.confirm_kb(temp_id),
    )


@bot.message_handler(content_types=["audio", "document"])
@guarded
def on_file(message):
    uid = message.from_user.id
    reg_user(message.from_user)
    if utils.is_flooding(uid):
        return
    user, err, markup = check_submission_allowed(uid)
    if err:
        bot.send_message(message.chat.id, err, reply_markup=markup)
        return
    kind = "audio" if message.content_type == "audio" else "document"
    obj = message.audio if kind == "audio" else message.document
    if (obj.file_size or 0) > config.MAX_FILE_SIZE:
        bot.send_message(message.chat.id, "Файл слишком большой (максимум 20 МБ).")
        return
    if not is_mp3(obj.file_name, obj.mime_type):
        bot.send_message(message.chat.id, "Принимаю только mp3 🎧")
        return

    path = os.path.join(config.TEMP_DIR, f"{uid}_{int(time.time())}.mp3")
    try:
        data = bot.download_file(bot.get_file(obj.file_id).file_path)
        if len(data) > config.MAX_FILE_SIZE:
            bot.send_message(message.chat.id, "Файл слишком большой (максимум 20 МБ).")
            return
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        logger.error("Ошибка скачивания файла user_id=%s", uid, exc_info=True)
        drop_file(path)
        bot.send_message(message.chat.id, "Не удалось скачать файл. Попробуй ещё раз.")
        return

    title, artist_tag = read_tags(path)
    temp_id = temp_put({
        "user_id": uid, "path": path, "title": title, "artist_tag": artist_tag,
        "pseudonym": user["pseudonym"], "file_id": obj.file_id, "file_kind": kind,
    })
    if title:
        show_confirm_card(message.chat.id, temp_id)
    else:
        ask(message.chat.id,
            "Не смог прочитать название. Напиши название трека одной строкой.\n/cancel — отмена",
            file_title_step, temp_id)


@guarded
def file_title_step(message, temp_id):
    st, text = parse_step_text(message, 150)
    if st == "cancel":
        e = temp_pop(temp_id)
        if e:
            drop_file(e["path"])
        bot.send_message(message.chat.id, "Отменено.")
    elif st == "ok" and not utils.extract_url(text):
        if not temp_get(temp_id):
            bot.send_message(message.chat.id, "Время вышло, отправь файл заново.")
            return
        temp_update(temp_id, title=text)
        show_confirm_card(message.chat.id, temp_id)
    else:
        ask(message.chat.id, "Название: до 150 символов, одной строкой, без команд и ссылок.",
            file_title_step, temp_id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("edit_title_"))
@guarded
def cb_edit_title(call):
    temp_id = call.data[len("edit_title_"):]
    e = temp_get(temp_id)
    if not e:
        bot.answer_callback_query(call.id, "Время вышло, отправь файл заново.", show_alert=True)
        return
    if e["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "Это не твой трек.")
        return
    bot.answer_callback_query(call.id)
    ask(call.message.chat.id, "Напиши название трека одной строкой.\n/cancel — отмена",
        file_title_step, temp_id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("cancel_"))
@guarded
def cb_cancel(call):
    temp_id = call.data[len("cancel_"):]
    e = temp_get(temp_id)
    if e and e["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "Это не твой трек.")
        return
    e = temp_pop(temp_id)
    if e:
        drop_file(e["path"])
    bot.answer_callback_query(call.id, "Отменено")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("confirm_"))
@guarded
def cb_confirm(call):
    temp_id = call.data[len("confirm_"):]
    e = temp_get(temp_id)
    if not e:
        bot.answer_callback_query(call.id, "Время вышло, отправь файл заново.", show_alert=True)
        return
    if e["user_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, "Это не твой трек.")
        return
    e = temp_pop(temp_id)  # защита от двойного нажатия
    if not e:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    # Повторная проверка лимита/стрима на момент подтверждения
    user, err, markup = check_submission_allowed(e["user_id"])
    if err:
        drop_file(e["path"])
        bot.send_message(call.message.chat.id, err, reply_markup=markup)
        return
    final_path = os.path.join(
        config.SUBMISSIONS_DIR, f"{e['user_id']}_{int(time.time())}_{secrets.token_hex(2)}.mp3"
    )
    try:
        shutil.move(e["path"], final_path)
    except Exception:
        logger.error("Не удалось переместить файл %s", e["path"], exc_info=True)
        bot.send_message(call.message.chat.id, "Не удалось сохранить файл. Отправь его заново.")
        return
    finalize_track(call.message.chat.id, user, e["title"], e["artist_tag"], "file",
                   file_id=e["file_id"], file_path=final_path, file_kind=e["file_kind"])


# ---------- Админ: оценка, скип, бан (callbacks) ----------

def apply_rating(track_id, scores, admin_id, rerate=False):
    """Оценка/переоценка трека. Возвращает текст ответа админу."""
    t = db.get_track(track_id)
    if not t:
        return f"Трек #{track_id} не найден."
    if rerate:
        if t["status"] != "rated":
            return f"Трек #{track_id} не оценён — переоценка невозможна."
        ok = db.rerate_track(track_id, scores, admin_id)
    else:
        if t["status"] != "pending":
            return f"Трек #{track_id} не в очереди (статус: {t['status']})."
        ok = db.rate_track(track_id, scores)
    if not ok:
        return "Не удалось сохранить оценку."
    total = sum(scores)
    logger.info("Трек #%s %s: %s", track_id, "переоценён" if rerate else "оценён", total)
    head = "🔁 Оценка твоего трека" if rerate else "🎧 Твой трек"
    tail = "изменена!" if rerate else "оценён!"
    safe_send(
        t["user_id"],
        f"{head} #{track_id} «{t['title']}» {tail}\n"
        f"{utils.fmt_scores_line(scores)}\n"
        f"🏆 ИТОГ: {total}/50",
    )
    if total >= 45:
        achievements.check_achievements(t["user_id"], "score_45")
    achievements.check_achievements(t["user_id"], "stable_3")
    utils.post_webhook({
        "event": "track_rerated" if rerate else "track_rated",
        "track_id": track_id, "user_id": t["user_id"], "username": t["username"],
        "title": t["title"], "pseudonym": t["pseudonym"],
        "scores": dict(zip(db.SCORE_KEYS, scores)), "total_score": total,
    })
    return f"✅ Трек #{track_id} «{t['title']}»: {total}/50"


def do_skip(track_id):
    t = db.get_track(track_id)
    if not t:
        return f"Трек #{track_id} не найден."
    if not db.skip_track(track_id):
        return f"Трек #{track_id} уже обработан."
    logger.info("Трек #%s скипнут", track_id)
    safe_send(t["user_id"], f"⏭ Твой трек #{track_id} «{t['title']}» пропущен стримером.")
    return f"⏭ Трек #{track_id} скипнут."


def do_ban(user_id):
    if not db.get_user(user_id):
        return f"Пользователь {user_id} не найден."
    n = db.ban_user(user_id)
    logger.info("Забанен user_id=%s (скипнуто треков: %d)", user_id, n)
    safe_send(user_id, "🚫 Ты заблокирован стримером.")
    return f"🚫 Пользователь {user_id} забанен. Скипнуто треков: {n}."


def strip_markup(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rate_"))
@guarded
def cb_rate(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return
    track_id = id_from(call.data, "rate_")
    t = db.get_track(track_id) if track_id else None
    if not t or t["status"] != "pending":
        bot.answer_callback_query(call.id, "Трек уже обработан или не найден.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    ask(call.message.chat.id,
        f"Оценки для #{track_id} «{t['title']}»: пять чисел 1–10 через пробел\n"
        "(бит структура стиль подача вайб). /cancel — отмена",
        rate_step, track_id)


@guarded
def rate_step(message, track_id):
    text = (message.text or "").strip() if message.content_type == "text" else ""
    if text.lower() == "/cancel":
        bot.send_message(message.chat.id, "Отменено.")
        return
    try:
        scores = utils.parse_scores(text.split())
    except ValueError:
        ask(message.chat.id, "Нужно ровно 5 целых чисел 1–10 через пробел. Ещё раз (или /cancel):",
            rate_step, track_id)
        return
    bot.send_message(message.chat.id, apply_rating(track_id, scores, message.from_user.id))


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("skip_"))
@guarded
def cb_skip(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return
    track_id = id_from(call.data, "skip_")
    res = do_skip(track_id) if track_id else "Некорректный id."
    bot.answer_callback_query(call.id, res)
    strip_markup(call)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ban_"))
@guarded
def cb_ban(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return
    track_id = id_from(call.data, "ban_")
    t = db.get_track(track_id) if track_id else None
    if not t:
        bot.answer_callback_query(call.id, "Трек не найден.", show_alert=True)
        return
    bot.answer_callback_query(call.id, do_ban(t["user_id"]))
    strip_markup(call)


# ---------- Админ: команды ----------

def admin_only(fn):
    """Команда доступна только админу; остальным — тихо игнор."""

    @functools.wraps(fn)
    def wrapper(message):
        if not is_admin(message.from_user.id):
            return
        return fn(message)

    return wrapper


def cmd_args(message):
    return (message.text or "").split()[1:]


@bot.message_handler(commands=["start_stream"])
@admin_only
@guarded
def cmd_start_stream(message):
    sid = db.start_stream()
    logger.info("Стрим #%s начат", sid)
    bot.send_message(
        message.chat.id,
        f"🎬 Стрим начат. Лимит: {utils.plural_tracks(config.STREAM_TRACK_LIMIT)} (+бонусы).",
    )


@bot.message_handler(commands=["end_stream"])
@admin_only
@guarded
def cmd_end_stream(message):
    if db.end_stream():
        logger.info("Стрим завершён")
        bot.send_message(message.chat.id, "🎬 Стрим завершен.")
    else:
        bot.send_message(message.chat.id, "Активного стрима нет.")


@bot.message_handler(commands=["rate", "rerate"])
@admin_only
@guarded
def cmd_rate(message):
    cmd = message.text.split()[0].split("@")[0].lstrip("/").lower()
    args = cmd_args(message)
    if len(args) != 6:
        bot.send_message(message.chat.id,
                         f"Формат: /{cmd} <track_id> <бит> <структура> <стиль> <подача> <вайб>")
        return
    try:
        track_id = int(args[0])
        scores = utils.parse_scores(args[1:])
    except ValueError:
        bot.send_message(message.chat.id, "id и оценки — целые числа, оценки 1..10.")
        return
    bot.send_message(message.chat.id,
                     apply_rating(track_id, scores, message.from_user.id, rerate=(cmd == "rerate")))


@bot.message_handler(commands=["skip"])
@admin_only
@guarded
def cmd_skip(message):
    args = cmd_args(message)
    if len(args) != 1 or not args[0].isdigit():
        bot.send_message(message.chat.id, "Формат: /skip <track_id>")
        return
    bot.send_message(message.chat.id, do_skip(int(args[0])))


@bot.message_handler(commands=["ban"])
@admin_only
@guarded
def cmd_ban(message):
    args = cmd_args(message)
    if len(args) != 1 or not args[0].isdigit():
        bot.send_message(message.chat.id, "Формат: /ban <user_id>")
        return
    bot.send_message(message.chat.id, do_ban(int(args[0])))


@bot.message_handler(commands=["unban"])
@admin_only
@guarded
def cmd_unban(message):
    args = cmd_args(message)
    if len(args) != 1 or not args[0].isdigit():
        bot.send_message(message.chat.id, "Формат: /unban <user_id>")
        return
    if db.unban_user(int(args[0])):
        logger.info("Разбанен user_id=%s", args[0])
        bot.send_message(message.chat.id, f"✅ Пользователь {args[0]} разбанен.")
    else:
        bot.send_message(message.chat.id, "Пользователь не найден.")


@bot.message_handler(commands=["queue"])
@admin_only
@guarded
def cmd_queue(message):
    rows = db.get_queue()
    if not rows:
        bot.send_message(message.chat.id, "Очередь пуста.")
        return
    lines = []
    for r in rows:
        icon = "💎" if r["is_donate"] else ("📁" if r["source"] == "file" else "🔗")
        lines.append(f"#{r['id']} | {icon} | {r['pseudonym']} | {r['title']} | {r['t']}")
    utils.send_long(bot, message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["stats"])
@admin_only
@guarded
def cmd_stats(message):
    s = db.stats_today()
    bot.send_message(
        message.chat.id,
        "📊 Статистика за сегодня:\n"
        f"Принято: {s['accepted']}\n"
        f"Оценено: {s['rated']}\n"
        f"Скипнуто: {s['skipped']}\n"
        f"Средний балл: {utils.fmt_avg(s['avg'])}\n"
        f"👥 Новых зрителей: {s['new_users']}\n"
        f"🤝 Рефералов: {s['refs']}",
    )


def post_top_to_channel(top_data):
    """Автопост топ-3 в канал. True — успешно."""
    if not config.CHANNEL_ID:
        return False
    text = utils.format_channel_post(top_data[:3], datetime.now().strftime("%d.%m.%Y"))
    try:
        bot.send_message(config.CHANNEL_ID, text)
        logger.info("Топ-3 опубликован в канале")
        return True
    except Exception:
        logger.error("Ошибка автопоста в канал %s", config.CHANNEL_ID, exc_info=True)
        safe_send(config.ADMIN_ID,
                  "⚠️ Не удалось опубликовать топ в канал. Проверь CHANNEL_ID и что бот — админ канала.")
        return False


@bot.message_handler(commands=["fix_top"])
@admin_only
@guarded
def cmd_fix_top(message):
    rows = db.get_top_rated(20)
    if not rows:
        bot.send_message(message.chat.id, "Нет оценённых треков.")
        return
    top_data = [
        {"place": i + 1, "id": r["id"], "user_id": r["user_id"], "title": r["title"],
         "pseudonym": r["pseudonym"], "total_score": r["total_score"]}
        for i, r in enumerate(rows)
    ]
    snap_id = db.save_top_snapshot(top_data)
    logger.info("Зафиксирован топ (снимок #%s)", snap_id)

    winner = top_data[0]["user_id"]
    db.increment_top1(winner)
    achievements.check_achievements(winner, "top1")
    achievements.check_achievements(winner, "champion_3")

    bot.send_message(message.chat.id, "🏆 Топ зафиксирован:\n\n" + utils.format_top_lines(top_data))
    if config.CHANNEL_ID:
        if post_top_to_channel(top_data):
            db.mark_snapshot_posted(snap_id)
            bot.send_message(message.chat.id, "📢 Топ-3 опубликован в канале.")


@bot.message_handler(commands=["history_top"])
@admin_only
@guarded
def cmd_history_top(message):
    snaps = db.get_snapshots(30)
    if not snaps:
        bot.send_message(message.chat.id, "Снимков топа пока нет.")
        return
    lines = []
    for s in snaps:
        data = json.loads(s["payload"])
        first = f"{data[0]['total_score']}/50 «{data[0]['title']}»" if data else "—"
        mark = "📢" if s["posted_to_channel"] else "—"
        lines.append(f"#{s['id']} — {s['d']} — {len(data)} треков — 🥇 {first} — {mark}")
    utils.send_long(bot, message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["add_donate"])
@admin_only
@guarded
def cmd_add_donate(message):
    args = cmd_args(message)
    if len(args) != 1 or not args[0].isdigit():
        bot.send_message(message.chat.id, "Формат: /add_donate <track_id>")
        return
    track_id = int(args[0])
    t = db.get_track(track_id)
    if not t or not db.set_donate(track_id):
        bot.send_message(message.chat.id, f"Трек #{track_id} не найден.")
        return
    logger.info("Трек #%s помечен донатным", track_id)
    safe_send(t["user_id"], "💎 Твой трек получил донат-приоритет!")
    bot.send_message(message.chat.id, f"Трек #{track_id} помечен как донатный. Поднят в очереди.")


# ---------- Топ и карточка трека ----------

def send_top(chat_id):
    snap = db.get_last_snapshot()
    if not snap:
        bot.send_message(chat_id, "Топ еще не фиксировался. Стример зафиксирует в конце стрима.")
        return
    data = json.loads(snap["payload"])
    utils.send_long(bot, chat_id, f"🏆 Топ треков ({snap['d']}):\n\n" + utils.format_top_lines(data))


@bot.message_handler(commands=["top"])
@guarded
def cmd_top(message):
    reg_user(message.from_user)
    send_top(message.chat.id)


@bot.message_handler(func=lambda m: bool(m.text) and re.match(r"^/track_\d+(@\w+)?$", m.text.strip()))
@guarded
def cmd_track(message):
    track_id = int(re.match(r"^/track_(\d+)", message.text.strip()).group(1))
    t = db.get_track(track_id)
    if not t:
        bot.send_message(message.chat.id, f"Трек #{track_id} не найден.")
        return
    lines = [
        f"🎵 #{t['id']} — {t['title']}",
        f"👤 {utils.format_artist(t['pseudonym'], t['artist_tag'])} "
        f"({utils.fmt_user(t['username'], t['user_id'])})",
    ]
    if t["url"]:
        lines.append(f"🔗 {t['url']}")
    lines.append(f"📅 {t['created_local']}")
    if t["status"] == "rated":
        b, s, st, d, v = utils.track_scores(t)
        lines += [
            "⭐ Оценки:",
            f"  Бит: {b}/10",
            f"  Структура: {s}/10",
            f"  Реализация стиля: {st}/10",
            f"  Подача: {d}/10",
            f"  Общий вайб: {v}/10",
            f"🏆 ИТОГ: {t['total_score']}/50",
        ]
    else:
        lines.append(STATUS_TEXT.get(t["status"], t["status"]))
    bot.send_message(message.chat.id, "\n".join(lines))


# ---------- Приём ссылок (должен быть последним text-хендлером) ----------

@bot.message_handler(content_types=["text"])
@guarded
def on_text(message):
    text = message.text or ""
    if text.startswith("/"):
        return
    url = utils.extract_url(text)
    if not url:
        return  # обычный текст игнорируем
    uid = message.from_user.id
    reg_user(message.from_user)
    if utils.is_flooding(uid):
        return
    user, err, markup = check_submission_allowed(uid)
    if err:
        bot.send_message(message.chat.id, err, reply_markup=markup)
        return
    ask(message.chat.id, "🔗 Принял ссылку. Напиши название трека одной строкой.\n/cancel — отмена",
        link_title_step, url)


@guarded
def link_title_step(message, url):
    st, text = parse_step_text(message, 150)
    if st == "cancel":
        bot.send_message(message.chat.id, "Отменено.")
        return
    if st != "ok" or utils.extract_url(text):
        ask(message.chat.id, "Название: до 150 символов, одной строкой, без команд и ссылок.",
            link_title_step, url)
        return
    user, err, markup = check_submission_allowed(message.from_user.id)
    if err:
        bot.send_message(message.chat.id, err, reply_markup=markup)
        return
    finalize_track(message.chat.id, user, text, None, "link", url=url)


# ---------- Запуск ----------

def main():
    if not config.ADMIN_ID:
        logger.warning("ADMIN_ID не задан — админ-команды недоступны")
    db.init_db()
    achievements.set_bot(bot)
    # Файлы из прошлых запусков в temp_uploads уже не привязаны к памяти — чистим
    for name in os.listdir(config.TEMP_DIR):
        drop_file(os.path.join(config.TEMP_DIR, name))
    threading.Thread(target=cleanup_loop, daemon=True).start()
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Начать"),
            types.BotCommand("profile", "Мой профиль"),
            types.BotCommand("my_stats", "Моя статистика"),
            types.BotCommand("my_tracks", "Мои треки"),
            types.BotCommand("achievements", "Ачивки"),
            types.BotCommand("referral", "Реферальная ссылка"),
            types.BotCommand("top", "Топ треков"),
        ])
    except Exception:
        logger.error("Не удалось установить список команд", exc_info=True)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    logger.info("Бот запущен")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
