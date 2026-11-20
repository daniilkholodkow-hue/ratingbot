"""Inline-клавиатуры."""
from telebot import types

B = types.InlineKeyboardButton


def start_kb():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        B("👤 Мой профиль", callback_data="menu_profile"),
        B("🏆 Топ треков", callback_data="menu_top"),
        B("ℹ️ Как скинуть трек", callback_data="menu_howto"),
        B("👥 Реферальная ссылка", callback_data="menu_referral"),
    )
    return m


def fill_profile_kb(label="✏️ Заполнить"):
    m = types.InlineKeyboardMarkup()
    m.add(B(label, callback_data="fill_profile"))
    return m


def confirm_kb(temp_id):
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        B("✅ В очередь", callback_data=f"confirm_{temp_id}"),
        B("✏️ Изменить", callback_data=f"edit_title_{temp_id}"),
        B("❌ Отмена", callback_data=f"cancel_{temp_id}"),
    )
    return m


def admin_track_kb(track_id):
    m = types.InlineKeyboardMarkup(row_width=3)
    m.add(
        B("🎧 Оценить", callback_data=f"rate_{track_id}"),
        B("⏭ Скип", callback_data=f"skip_{track_id}"),
        B("🚫 Бан", callback_data=f"ban_{track_id}"),
    )
    return m
