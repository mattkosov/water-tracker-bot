"""Telegram-трекер воды. Python 3.11+, сторонние пакеты не нужны.

1. Создайте бота через @BotFather командой /newbot.
2. В терминале macOS (zsh) в папке с файлом выполните:
       read -rs 'TELEGRAM_BOT_TOKEN?Токен от BotFather: '
       export TELEGRAM_BOT_TOKEN
       python3 water_bot.py
3. Откройте личный чат с ботом и нажмите «Старт».

По умолчанию день считается по Тюмени. Можно задать другой часовой
пояс переменной WATER_BOT_TZ, например Europe/Moscow.
Данные хранятся в water_bot.sqlite3 рядом со скриптом.
Бот отвечает, пока скрипт работает. Для остановки нажмите Ctrl+C.
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEYBOARD = {
    "keyboard": [
        ["+500 мл", "+1 л"],
        ["Своё значение", "Цель на день"],
        ["Прогресс"],
    ],
    "resize_keyboard": True,
}
PRESETS = {"+500 мл": 500, "+1 л": 1000}
SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    user_id INTEGER PRIMARY KEY,
    amount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    celebrated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS actions (
    update_id INTEGER PRIMARY KEY,
    reply TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def api(token, method, **params):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=40) as response:
            data = json.load(response)
    except HTTPError as error:
        with error:
            try:
                data = json.load(error)
            except ValueError:
                raise TelegramError(error.code) from None
    if not data["ok"]:
        retry_after = data.get("parameters", {}).get("retry_after", 0)
        raise TelegramError(data["error_code"], retry_after)
    return data["result"]


def read_amount(text):
    match = re.fullmatch(r"([0-9]{1,5})(?:[.,]([0-9]{1,3}))?\s*(мл|л)?", text.lower())
    if not match:
        return None
    whole, fraction, unit = match.groups()
    if unit == "л":
        amount = int(whole) * 1000 + int((fraction or "").ljust(3, "0"))
    elif fraction is None:
        amount = int(whole)
    else:
        return None
    return amount if 0 < amount <= 100_000 else None


def progress(db, user_id, day):
    goal = db.execute("SELECT amount FROM goals WHERE user_id = ?", (user_id,)).fetchone()
    entry = db.execute(
        "SELECT amount, celebrated FROM daily WHERE user_id = ? AND day = ?",
        (user_id, day),
    ).fetchone()
    total, celebrated = entry or (0, 0)
    return total, goal[0] if goal else None, celebrated


def format_progress(total, goal):
    if goal is None:
        return f"Сегодня выпито {total} мл. Задай цель кнопкой «Цель на день»."
    remaining = max(0, goal - total)
    status = f"Осталось {remaining} мл." if remaining else "Цель выполнена."
    return f"Сегодня выпито {total} из {goal} мл. {status}"


def record(db, update_id, user_id, day, action, amount):
    with db:
        # Повторная доставка обновления Telegram не меняет объём повторно.
        inserted = db.execute(
            "INSERT OR IGNORE INTO actions VALUES (?, '', ?)",
            (update_id, int(time.time())),
        )
        if not inserted.rowcount:
            return db.execute(
                "SELECT reply FROM actions WHERE update_id = ?", (update_id,)
            ).fetchone()[0]

        db.execute("INSERT OR IGNORE INTO daily (user_id, day) VALUES (?, ?)", (user_id, day))
        if action == "goal":
            db.execute(
                "INSERT INTO goals VALUES (?, ?) ON CONFLICT(user_id) "
                "DO UPDATE SET amount = excluded.amount",
                (user_id, amount),
            )
            prefix = f"Цель установлена: {amount} мл в день."
        else:
            db.execute(
                "UPDATE daily SET amount = amount + ? WHERE user_id = ? AND day = ?",
                (amount, user_id, day),
            )
            prefix = f"Добавлено {amount} мл."

        total, goal, celebrated = progress(db, user_id, day)
        if goal is not None and total >= goal and not celebrated:
            db.execute(
                "UPDATE daily SET celebrated = 1 WHERE user_id = ? AND day = ?",
                (user_id, day),
            )
            reply = f"{prefix}\n🎉 Дневная цель выполнена! Сегодня выпито {total} из {goal} мл."
        else:
            reply = f"{prefix}\n{format_progress(total, goal)}"

        db.execute("UPDATE actions SET reply = ? WHERE update_id = ?", (reply, update_id))
        db.execute("DELETE FROM actions WHERE created_at < ?", (int(time.time()) - 172800,))
    return reply


def handle(db, pending, update, day):
    message = update.get("message", {})
    chat = message.get("chat", {})
    text = message.get("text")
    if chat.get("type") != "private" or not text:
        return None

    user_id = chat["id"]
    text = text.strip()
    update_id = update["update_id"]
    previous = db.execute("SELECT reply FROM actions WHERE update_id = ?", (update_id,)).fetchone()
    if previous:
        return previous[0]

    if text == "/start":
        pending.pop(user_id, None)
        return "💧 Здесь можно записывать воду и следить за дневной целью.\n" + format_progress(
            *progress(db, user_id, day)[:2]
        )
    if text == "/cancel":
        pending.pop(user_id, None)
        return "Ввод отменён."
    if text == "Прогресс":
        pending.pop(user_id, None)
        return format_progress(*progress(db, user_id, day)[:2])
    if text in ("Цель на день", "Своё значение"):
        pending[user_id] = "goal" if text == "Цель на день" else "drink"
        prompt = "Сколько воды хочешь выпивать за день?" if pending[user_id] == "goal" else "Сколько воды ты выпил?"
        return f"{prompt} Введи, например, 750 мл или 1,25 л. Отмена — /cancel."
    if text in PRESETS:
        pending.pop(user_id, None)
        return record(db, update_id, user_id, day, "drink", PRESETS[text])
    if user_id in pending:
        amount = read_amount(text)
        if amount is None:
            return "Введи объём больше нуля в миллилитрах или литрах. Например, 350 или 0,35 л."
        reply = record(db, update_id, user_id, day, pending[user_id], amount)
        pending.pop(user_id)
        return reply
    return "Выбери действие на клавиатуре."


def run(token, timezone, db):
    offset = 0
    pending = {}
    while True:
        try:
            updates = api(token, "getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
            for update in updates:
                message = update.get("message", {})
                day = datetime.fromtimestamp(message.get("date", time.time()), timezone).date().isoformat()
                reply = handle(db, pending, update, day)
                if reply:
                    try:
                        api(token, "sendMessage", chat_id=message["chat"]["id"], text=reply, reply_markup=KEYBOARD)
                    except TelegramError as error:
                        if error.code != 403:
                            raise
                offset = update["update_id"] + 1
        except TelegramError as error:
            if error.code in (401, 404):
                raise SystemExit("Telegram отклонил токен. Проверь токен от @BotFather.") from None
            if error.code == 409:
                raise SystemExit("Telegram уже получает обновления другим способом. Проверь, что запущена одна копия бота и выключен webhook.") from None
            print(f"Ошибка Telegram ({error.code}). Повторяю запрос.", file=sys.stderr)
            time.sleep(error.retry_after or 3)
        except (URLError, TimeoutError):
            print("Не удалось подключиться к Telegram. Повторяю запрос.", file=sys.stderr)
            time.sleep(3)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Укажи токен от @BotFather в переменной TELEGRAM_BOT_TOKEN. Инструкция в начале файла.")
    try:
        timezone = ZoneInfo(os.environ.get("WATER_BOT_TZ", "Asia/Yekaterinburg"))
    except ZoneInfoNotFoundError:
        raise SystemExit("Часовой пояс не найден. Проверь WATER_BOT_TZ и наличие базы часовых поясов.") from None

    db = sqlite3.connect(Path(__file__).with_suffix(".sqlite3"))
    try:
        db.executescript(SCHEMA)
        print("Бот запущен. Открой его в Telegram и нажми «Старт».")
        run(token, timezone, db)
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
