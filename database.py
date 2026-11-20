"""Все SQL-запросы (sqlite3, без ORM, параметризованные)."""
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager

import config

logger = logging.getLogger(__name__)

SCORE_KEYS = ("beat", "structure", "style", "delivery", "vibe")

# (code, emoji, title, description) — каталог ачивок
ACHIEVEMENTS = [
    ("top1", "🔥", "Топ-1 стрима", "Занял #1 при фиксации топа."),
    ("score_45", "⭐", "Оценка 45+", "Трек получил 45 баллов или больше."),
    ("stable_3", "🎯", "Стабильность", "3 трека подряд с оценкой 35+."),
    ("active_10", "🚀", "Активный", "Отправлено 10 треков."),
    ("legend_25", "👑", "Легенда", "Отправлено 25 треков."),
    ("champion_3", "🏆", "Чемпион", "3 раза занял #1 при фиксации топа."),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    pseudonym TEXT,
    city TEXT,
    bio TEXT,
    referral_code TEXT UNIQUE,
    referrer_id INTEGER,
    bonus_limit INTEGER DEFAULT 0,
    stream_track_count INTEGER DEFAULT 0,
    total_tracks INTEGER DEFAULT 0,
    top1_count INTEGER DEFAULT 0,
    is_banned INTEGER DEFAULT 0,
    last_action_at TIMESTAMP,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    pseudonym TEXT,
    title TEXT,
    artist_tag TEXT,
    source TEXT,
    file_id TEXT,
    file_path TEXT,
    url TEXT,
    status TEXT DEFAULT 'pending',
    is_donate INTEGER DEFAULT 0,
    beat INTEGER, structure INTEGER, style INTEGER,
    delivery INTEGER, vibe INTEGER,
    total_score INTEGER,
    rated_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stream_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    is_active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    code TEXT,
    earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, code)
);
CREATE TABLE IF NOT EXISTS achievements_catalog (
    code TEXT PRIMARY KEY,
    title TEXT,
    emoji TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER,
    referred_id INTEGER,
    confirmed INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(referred_id)
);
CREATE TABLE IF NOT EXISTS top_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    payload TEXT,
    posted_to_channel INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ratings_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER, admin_id INTEGER,
    old_scores TEXT, new_scores TEXT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id);
"""


@contextmanager
def db():
    """Короткое соединение на операцию (бот многопоточный)."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.error("Ошибка SQL", exc_info=True)
        raise
    finally:
        conn.close()


def _one(c, sql, params=()):
    row = c.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(c, sql, params=()):
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def init_db():
    """Создаёт таблицы и заполняет каталог ачивок."""
    with db() as c:
        c.executescript(SCHEMA)
        c.executemany(
            "INSERT OR REPLACE INTO achievements_catalog(code, emoji, title, description) "
            "VALUES (?, ?, ?, ?)",
            ACHIEVEMENTS,
        )
    logger.info("БД инициализирована")


# ---------- Пользователи ----------

def _new_ref_code(c):
    while True:
        code = secrets.token_hex(4)
        if not c.execute("SELECT 1 FROM users WHERE referral_code=?", (code,)).fetchone():
            return code


def ensure_user(user_id, username):
    """Создаёт юзера при первом контакте. True — если юзер новый."""
    with db() as c:
        row = c.execute("SELECT username FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            if (row["username"] or None) != (username or None):
                c.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            return False
        c.execute(
            "INSERT INTO users(user_id, username, referral_code) VALUES (?, ?, ?)",
            (user_id, username, _new_ref_code(c)),
        )
        return True


def get_user(user_id):
    with db() as c:
        return _one(c, "SELECT * FROM users WHERE user_id=?", (user_id,))


def get_user_by_username(username):
    with db() as c:
        return _one(
            c, "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username.lstrip("@"),)
        )


def touch_user(user_id):
    with db() as c:
        c.execute("UPDATE users SET last_action_at=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))


def set_profile(user_id, pseudonym, city, bio):
    with db() as c:
        c.execute(
            "UPDATE users SET pseudonym=?, city=?, bio=? WHERE user_id=?",
            (pseudonym, city, bio, user_id),
        )


def ban_user(user_id):
    """Баним юзера, его pending-треки -> skipped. Возвращает число скипнутых."""
    with db() as c:
        c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
        cur = c.execute(
            "UPDATE tracks SET status='skipped' WHERE user_id=? AND status='pending'", (user_id,)
        )
        return cur.rowcount


def unban_user(user_id):
    with db() as c:
        cur = c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
        return cur.rowcount > 0


def increment_top1(user_id):
    with db() as c:
        c.execute("UPDATE users SET top1_count=top1_count+1 WHERE user_id=?", (user_id,))
        return c.execute("SELECT top1_count FROM users WHERE user_id=?", (user_id,)).fetchone()[0]


# ---------- Рефералы ----------

def add_referral(referrer_id, referred_id):
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO referrals(referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id),
        )
        if cur.rowcount:
            c.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (referrer_id, referred_id))
            return True
        return False


def confirm_referral(referred_id):
    """Подтверждает реферал, возвращает referrer_id или None."""
    with db() as c:
        row = c.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id=? AND confirmed=0", (referred_id,)
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE referrals SET confirmed=1 WHERE referred_id=?", (referred_id,))
        return row["referrer_id"]


def add_referral_bonus(referrer_id):
    """+1 к bonus_limit, пока не достигнут REFERRAL_BONUS_LIMIT."""
    with db() as c:
        cur = c.execute(
            "UPDATE users SET bonus_limit=bonus_limit+1 WHERE user_id=? AND bonus_limit<?",
            (referrer_id, config.REFERRAL_BONUS_LIMIT),
        )
        return cur.rowcount > 0


def count_referrals(referrer_id, confirmed_only=True):
    sql = "SELECT COUNT(*) FROM referrals WHERE referrer_id=?"
    if confirmed_only:
        sql += " AND confirmed=1"
    with db() as c:
        return c.execute(sql, (referrer_id,)).fetchone()[0]


# ---------- Стримы ----------

def get_active_stream():
    with db() as c:
        return _one(c, "SELECT * FROM stream_sessions WHERE is_active=1 ORDER BY id DESC LIMIT 1")


def start_stream():
    with db() as c:
        c.execute(
            "UPDATE stream_sessions SET is_active=0, ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP) "
            "WHERE is_active=1"
        )
        cur = c.execute("INSERT INTO stream_sessions(is_active) VALUES (1)")
        c.execute("UPDATE users SET stream_track_count=0")
        return cur.lastrowid


def end_stream():
    with db() as c:
        cur = c.execute(
            "UPDATE stream_sessions SET ended_at=CURRENT_TIMESTAMP, is_active=0 WHERE is_active=1"
        )
        return cur.rowcount > 0


# ---------- Треки ----------

def add_track(user_id, pseudonym, title, artist_tag, source, file_id, file_path, url):
    with db() as c:
        cur = c.execute(
            "INSERT INTO tracks(user_id, pseudonym, title, artist_tag, source, file_id, file_path, url, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (user_id, pseudonym, title, artist_tag, source, file_id, file_path, url),
        )
        c.execute(
            "UPDATE users SET stream_track_count=stream_track_count+1, total_tracks=total_tracks+1 "
            "WHERE user_id=?",
            (user_id,),
        )
        return cur.lastrowid


def get_track(track_id):
    with db() as c:
        return _one(
            c,
            "SELECT t.*, u.username AS username, datetime(t.created_at, 'localtime') AS created_local "
            "FROM tracks t LEFT JOIN users u ON u.user_id = t.user_id WHERE t.id=?",
            (track_id,),
        )


def get_queue_position(track_id):
    """Позиция pending-трека в очереди (донат выше). 0 — если трека нет в очереди."""
    t = get_track(track_id)
    if not t or t["status"] != "pending":
        return 0
    with db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM tracks WHERE status='pending' AND ("
            "is_donate > ? OR (is_donate = ? AND (created_at < ? OR (created_at = ? AND id <= ?))))",
            (t["is_donate"], t["is_donate"], t["created_at"], t["created_at"], track_id),
        ).fetchone()[0]


def get_queue():
    with db() as c:
        return _all(
            c,
            "SELECT id, pseudonym, title, source, is_donate, "
            "strftime('%H:%M', created_at, 'localtime') AS t "
            "FROM tracks WHERE status='pending' ORDER BY is_donate DESC, created_at ASC, id ASC",
        )


def get_user_tracks(user_id):
    with db() as c:
        return _all(c, "SELECT * FROM tracks WHERE user_id=? ORDER BY id DESC", (user_id,))


def _scores_dict(scores):
    return dict(zip(SCORE_KEYS, scores))


def rate_track(track_id, scores):
    """Оценка pending-трека. Итог = сумма пяти шкал."""
    total = sum(scores)
    with db() as c:
        cur = c.execute(
            "UPDATE tracks SET beat=?, structure=?, style=?, delivery=?, vibe=?, total_score=?, "
            "status='rated', rated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (*scores, total, track_id),
        )
        return cur.rowcount > 0


def rerate_track(track_id, scores, admin_id):
    """Переоценка rated-трека с записью в ratings_log."""
    total = sum(scores)
    with db() as c:
        old = c.execute(
            "SELECT beat, structure, style, delivery, vibe, total_score FROM tracks "
            "WHERE id=? AND status='rated'",
            (track_id,),
        ).fetchone()
        if not old:
            return False
        c.execute(
            "UPDATE tracks SET beat=?, structure=?, style=?, delivery=?, vibe=?, total_score=? WHERE id=?",
            (*scores, total, track_id),
        )
        c.execute(
            "INSERT INTO ratings_log(track_id, admin_id, old_scores, new_scores) VALUES (?, ?, ?, ?)",
            (
                track_id,
                admin_id,
                json.dumps(dict(old), ensure_ascii=False),
                json.dumps({**_scores_dict(scores), "total_score": total}, ensure_ascii=False),
            ),
        )
        return True


def skip_track(track_id):
    with db() as c:
        cur = c.execute(
            "UPDATE tracks SET status='skipped' WHERE id=? AND status='pending'", (track_id,)
        )
        return cur.rowcount > 0


def set_donate(track_id):
    with db() as c:
        cur = c.execute("UPDATE tracks SET is_donate=1 WHERE id=?", (track_id,))
        return cur.rowcount > 0


# ---------- Ачивки ----------

def has_achievement(user_id, code):
    with db() as c:
        return bool(
            c.execute(
                "SELECT 1 FROM achievements WHERE user_id=? AND code=?", (user_id, code)
            ).fetchone()
        )


def grant_achievement(user_id, code):
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO achievements(user_id, code) VALUES (?, ?)", (user_id, code)
        )
        return cur.rowcount > 0


def get_achievement(code):
    with db() as c:
        return _one(c, "SELECT * FROM achievements_catalog WHERE code=?", (code,))


def get_catalog():
    """Каталог в порядке ACHIEVEMENTS."""
    with db() as c:
        rows = {r["code"]: r for r in _all(c, "SELECT * FROM achievements_catalog")}
    return [rows[a[0]] for a in ACHIEVEMENTS if a[0] in rows]


def get_user_achievement_codes(user_id):
    with db() as c:
        return {r[0] for r in c.execute("SELECT code FROM achievements WHERE user_id=?", (user_id,))}


def get_last_rated_scores(user_id, n=3):
    with db() as c:
        return [
            r[0]
            for r in c.execute(
                "SELECT total_score FROM tracks WHERE user_id=? AND status='rated' "
                "ORDER BY rated_at DESC, id DESC LIMIT ?",
                (user_id, n),
            )
        ]


def has_score_at_least(user_id, score):
    with db() as c:
        return bool(
            c.execute(
                "SELECT 1 FROM tracks WHERE user_id=? AND status='rated' AND total_score>=? LIMIT 1",
                (user_id, score),
            ).fetchone()
        )


# ---------- Топ ----------

def get_top_rated(limit=20):
    with db() as c:
        return _all(
            c,
            "SELECT id, user_id, pseudonym, title, total_score FROM tracks WHERE status='rated' "
            "ORDER BY total_score DESC, rated_at ASC, id ASC LIMIT ?",
            (limit,),
        )


def save_top_snapshot(top_data):
    with db() as c:
        cur = c.execute(
            "INSERT INTO top_snapshots(payload) VALUES (?)", (json.dumps(top_data, ensure_ascii=False),)
        )
        return cur.lastrowid


def mark_snapshot_posted(snapshot_id):
    with db() as c:
        c.execute("UPDATE top_snapshots SET posted_to_channel=1 WHERE id=?", (snapshot_id,))


def get_last_snapshot():
    with db() as c:
        return _one(
            c,
            "SELECT id, payload, posted_to_channel, datetime(snapshot_date, 'localtime') AS d "
            "FROM top_snapshots ORDER BY id DESC LIMIT 1",
        )


def get_snapshots(limit=30):
    with db() as c:
        return _all(
            c,
            "SELECT id, payload, posted_to_channel, datetime(snapshot_date, 'localtime') AS d "
            "FROM top_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        )


# ---------- Статистика ----------

def stats_today():
    day_c = "date(created_at, 'localtime') = date('now', 'localtime')"
    with db() as c:
        accepted = c.execute(f"SELECT COUNT(*) FROM tracks WHERE {day_c}").fetchone()[0]
        rated_row = c.execute(
            "SELECT COUNT(*), AVG(total_score) FROM tracks WHERE status='rated' "
            "AND date(rated_at, 'localtime') = date('now', 'localtime')"
        ).fetchone()
        skipped = c.execute(
            f"SELECT COUNT(*) FROM tracks WHERE status='skipped' AND {day_c}"
        ).fetchone()[0]
        new_users = c.execute(
            "SELECT COUNT(*) FROM users WHERE date(registered_at, 'localtime') = date('now', 'localtime')"
        ).fetchone()[0]
        refs = c.execute(f"SELECT COUNT(*) FROM referrals WHERE {day_c}").fetchone()[0]
    return {
        "accepted": accepted,
        "rated": rated_row[0],
        "avg": rated_row[1],
        "skipped": skipped,
        "new_users": new_users,
        "refs": refs,
    }


def calculate_user_stats(user_id):
    """Личная статистика: треки, средний балл, лучший трек, место в зачёте и т.д."""
    u = get_user(user_id)
    if not u:
        return None
    with db() as c:
        agg = c.execute(
            "SELECT COUNT(*), AVG(total_score) FROM tracks WHERE user_id=? AND status='rated'",
            (user_id,),
        ).fetchone()
        best = c.execute(
            "SELECT title, total_score FROM tracks WHERE user_id=? AND status='rated' "
            "ORDER BY total_score DESC, rated_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        ranking = [
            r[0]
            for r in c.execute(
                "SELECT user_id FROM tracks WHERE status='rated' GROUP BY user_id "
                "ORDER BY AVG(total_score) DESC, user_id ASC"
            )
        ]
    rank = ranking.index(user_id) + 1 if user_id in ranking else None
    return {
        "total_tracks": u["total_tracks"],
        "rated_count": agg[0],
        "avg_score": agg[1],
        "best_title": best["title"] if best else None,
        "best_score": best["total_score"] if best else None,
        "rank": rank,
        "ranked_total": len(ranking),
        "top1_count": u["top1_count"],
        "invited": count_referrals(user_id, confirmed_only=True),
        "bonus_limit": u["bonus_limit"],
        "stream_track_count": u["stream_track_count"],
    }
