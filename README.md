# Music Rating Bot

Telegram-бот для Twitch-стримера: зрители отправляют mp3 или ссылки SoundCloud, стример оценивает треки по 5 шкалам (1–10, итог = сумма, 5..50). Профили, статистика, ачивки, рефералы, автопост топ-3 в канал.

## 1. Установка
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Настройка .env
```bash
cp .env.example .env
```
- `BOT_TOKEN` — токен от @BotFather
- `BOT_USERNAME` — username бота без @
- `ADMIN_ID` — твой Telegram ID (узнать у @userinfobot)
- `CHANNEL_ID` — канал для автопоста, например `@salasmalekule` (или числовой id)
- `STREAM_TRACK_LIMIT`, `REFERRAL_BONUS_LIMIT`, `ANTIFLOOD_SECONDS` — лимиты
- `WEBHOOK_URL` — опционально: после каждой оценки на него уходит POST с JSON

## 3. Бот — админ канала
Добавь бота в канал как администратора с правом публикации сообщений.

## 4. Запуск
```bash
python bot.py
```
Логи пишутся в `bot.log`.

## 5. Первый тест
1. `/start_stream` (от админа)
2. Со второго аккаунта: `/start`, заполнить профиль, отправить mp3
3. Подтвердить трек — он придёт админу с кнопками
4. `/rate <id> 9 8 9 8 9` или кнопка «🎧 Оценить»
5. `/fix_top` — фиксация топа и автопост в канал

## Команды админа
`/start_stream`, `/end_stream`, `/rate`, `/rerate`, `/skip <id>`, `/ban <user_id>`, `/unban <user_id>`, `/queue`, `/stats`, `/fix_top`, `/history_top`, `/add_donate <id>`
