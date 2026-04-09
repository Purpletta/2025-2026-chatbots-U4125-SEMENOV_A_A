"""
Telegram-бот: трекер привычек.
Управление через кнопки (Reply + Inline). Данные — JSON по user_id.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Подписи кнопок главного меню (точное совпадение текста) ---
BTN_LIST = "📋 Список"
BTN_DONE = "✅ Отметить"
BTN_STATS = "📊 Статистика"
BTN_DELETE = "🗑 Удалить"
BTN_ADD = "➕ Создать привычку"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "↩️ Отмена"

# Состояние диалога: ждём название новой привычки
WAITING_HABIT_NAME = 1

TZ = ZoneInfo("Europe/Moscow")
DATA_PATH = Path(__file__).resolve().parent / "data.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню: все действия через кнопки."""
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD, BTN_LIST],
            [BTN_DONE, BTN_STATS],
            [BTN_DELETE, BTN_HELP],
        ],
        resize_keyboard=True,
    )


def cancel_only_keyboard() -> ReplyKeyboardMarkup:
    """Пока ждём название привычки — только отмена (чтобы случайный текст не ушёл в другое действие)."""
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


def load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {"users": {}}
    try:
        raw = DATA_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or "users" not in data:
            logger.warning("Некорректная структура data.json, создаём заново")
            return {"users": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.exception("Не удалось прочитать data.json: %s", e)
        return {"users": {}}


def save_data(data: dict[str, Any]) -> None:
    DATA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


def ensure_user(data: dict[str, Any], user_id: int) -> dict[str, Any]:
    key = str(user_id)
    if key not in data["users"]:
        data["users"][key] = {"habits": {}, "next_id": 1}
    return data["users"][key]


def habits_ordered(user: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    habits = user.get("habits", {})
    return sorted(habits.items(), key=lambda x: int(x[0]))


def streak_for_habit(completions: list[str], today: str) -> int:
    present = set(completions)
    if not present:
        return 0
    d_today = date.fromisoformat(today)
    yesterday = (d_today - timedelta(days=1)).isoformat()

    if today in present:
        cursor = d_today
    elif yesterday in present:
        cursor = d_today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor.isoformat() in present:
        streak += 1
        cursor = cursor - timedelta(days=1)
    return streak


# --- /start: только вход и клавиатура ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Привет! Я бот для отслеживания привычек.\n\n"
        "Всё управление — кнопками внизу экрана.\n"
        "Чтобы добавить привычку: нажми «Создать привычку», "
        "потом одним сообщением напиши её название.\n\n"
        "Команды в чат вводить не нужно.",
        reply_markup=main_keyboard(),
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Что делают кнопки:\n\n"
        f"{BTN_ADD} — затем введи название привычки текстом.\n"
        f"{BTN_LIST} — список привычек.\n"
        f"{BTN_DONE} — выбери привычку кнопкой, чтобы отметить сегодня.\n"
        f"{BTN_STATS} — статистика.\n"
        f"{BTN_DELETE} — выбери привычку, чтобы удалить.\n\n"
        "«Сегодня» и даты считаются по часовому поясу Europe/Moscow.",
        reply_markup=main_keyboard(),
    )


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    ordered = habits_ordered(user)
    if not ordered:
        await update.message.reply_text(
            "Пока нет привычек. Нажми «Создать привычку».",
            reply_markup=main_keyboard(),
        )
        return
    lines = [f"{i}. {h['name']}" for i, (_hid, h) in enumerate(ordered, start=1)]
    await update.message.reply_text(
        "Твои привычки:\n" + "\n".join(lines),
        reply_markup=main_keyboard(),
    )


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    ordered = habits_ordered(user)
    if not ordered:
        await update.message.reply_text(
            "Пока нет данных. Создай привычку кнопкой ниже.",
            reply_markup=main_keyboard(),
        )
        return
    today = today_str()
    lines = ["Статистика (Москва):\n"]
    for _hid, habit in ordered:
        comps = habit.get("completions", [])
        total = len(set(comps))
        streak = streak_for_habit(comps, today)
        lines.append(
            f"• {habit['name']}\n"
            f"  дней с отметкой: {total}; серия: {streak}"
        )
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


def habits_inline_keyboard(prefix: str, ordered: list[tuple[str, dict[str, Any]]]) -> InlineKeyboardMarkup:
    """prefix: 'done' или 'del' для callback_data."""
    rows = [
        [InlineKeyboardButton(text=h["name"][:64], callback_data=f"{prefix}:{hid}")]
        for hid, h in ordered
    ]
    return InlineKeyboardMarkup(rows)


async def prompt_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    ordered = habits_ordered(user)
    if not ordered:
        await update.message.reply_text(
            "Пока нечего отмечать. Сначала создай привычку.",
            reply_markup=main_keyboard(),
        )
        return
    await update.message.reply_text(
        "Нажми кнопку с названием привычки, чтобы отметить её на сегодня:",
        reply_markup=habits_inline_keyboard("done", ordered),
    )


async def prompt_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    ordered = habits_ordered(user)
    if not ordered:
        await update.message.reply_text(
            "Удалять пока нечего.",
            reply_markup=main_keyboard(),
        )
        return
    await update.message.reply_text(
        "Выбери привычку, которую нужно удалить:",
        reply_markup=habits_inline_keyboard("del", ordered),
    )


async def on_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    await query.answer()
    m = re.match(r"^done:(.+)$", query.data)
    if not m:
        return
    hid = m.group(1)

    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    habit = user.get("habits", {}).get(hid)
    if habit is None:
        await query.message.reply_text("Привычка не найдена. Обнови список.")
        return

    day = today_str()
    comps = habit.setdefault("completions", [])
    if day in comps:
        await query.message.reply_text(
            f"«{habit['name']}» уже отмечена на сегодня.",
            reply_markup=main_keyboard(),
        )
        await query.edit_message_reply_markup(reply_markup=None)
        return
    comps.append(day)
    comps.sort()
    try:
        save_data(data)
    except OSError as e:
        logger.exception("Ошибка записи: %s", e)
        await query.message.reply_text("Не удалось сохранить.", reply_markup=main_keyboard())
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Отлично! «{habit['name']}» отмечена на {day}.",
        reply_markup=main_keyboard(),
    )


async def on_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    await query.answer()
    m = re.match(r"^del:(.+)$", query.data)
    if not m:
        return
    hid = m.group(1)

    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    habits = user.get("habits", {})
    habit = habits.get(hid)
    if habit is None:
        await query.message.reply_text("Привычка уже удалена или не найдена.")
        return
    name = habit["name"]
    del habits[hid]
    try:
        save_data(data)
    except OSError as e:
        logger.exception("Ошибка записи: %s", e)
        await query.message.reply_text("Не удалось сохранить.", reply_markup=main_keyboard())
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"Удалено: «{name}».", reply_markup=main_keyboard())


# --- Диалог: только после «Создать привычку» вводим название ---


async def begin_add_habit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    await update.message.reply_text(
        "Напиши название привычки следующим сообщением (одним текстом). "
        "Если передумал — нажми «Отмена».",
        reply_markup=cancel_only_keyboard(),
    )
    return WAITING_HABIT_NAME


async def receive_habit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user is None or update.message is None or not update.message.text:
        return WAITING_HABIT_NAME

    text = update.message.text.strip()
    if text == BTN_CANCEL:
        await update.message.reply_text("Окей, отменено.", reply_markup=main_keyboard())
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("Пустое название. Напиши ещё раз или «Отмена».")
        return WAITING_HABIT_NAME

    data = load_data()
    user = ensure_user(data, update.effective_user.id)
    hid = str(user["next_id"])
    user["habits"][hid] = {"name": text, "completions": []}
    user["next_id"] = int(hid) + 1
    try:
        save_data(data)
    except OSError as e:
        logger.exception("Ошибка записи: %s", e)
        await update.message.reply_text(
            "Не удалось сохранить. Попробуй позже.",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"Привычка добавлена: «{text}».",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Окей, отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def conv_fallback_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Во время ввода названия: /start отменяет ввод и возвращает главное меню."""
    if update.message:
        await update.message.reply_text(
            "Ввод названия отменён. Ниже снова главное меню.",
            reply_markup=main_keyboard(),
        )
    return ConversationHandler.END


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Произвольный текст вне режима ввода названия — просим пользоваться кнопками."""
    if update.message is None:
        return
    await update.message.reply_text(
        "Я понимаю только кнопки меню внизу. "
        "Текстом можно ввести название только после нажатия «Создать привычку».",
        reply_markup=main_keyboard(),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка при обработке update: %s", context.error)


def main() -> None:
    load_dotenv()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        logger.error("Задайте BOT_TOKEN в .env")
        raise SystemExit(1)

    app = Application.builder().token(token).build()

    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD)}$"), begin_add_habit),
        ],
        states={
            WAITING_HABIT_NAME: [
                MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), cancel_conversation),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_habit_name),
            ],
        },
        fallbacks=[
            CommandHandler("start", conv_fallback_start),
        ],
        name="add_habit",
        persistent=False,
    )

    # Сначала диалог создания привычки, чтобы /start внутри режима ввода обрабатывался fallbacks
    app.add_handler(add_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"), show_help))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_LIST)}$"), show_list))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STATS)}$"), show_stats))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DONE)}$"), prompt_done))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"), prompt_delete))
    app.add_handler(CallbackQueryHandler(on_done_callback, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^del:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    app.add_error_handler(on_error)

    logger.info("Бот запущен (polling, кнопки)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
