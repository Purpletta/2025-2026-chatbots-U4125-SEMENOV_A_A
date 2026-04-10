"""
Telegram-бот: трекер привычек.
Управление через кнопки (Reply + Inline). Данные — SQLite (users, habits, completions).
Геймификация: очки, уровни; серии; график за 7 дней (matplotlib).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from dotenv import load_dotenv
from flask import Flask, Response, request
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
BTN_PROGRESS = "📈 Мой прогресс"
BTN_CHART = "📉 График"

WAITING_HABIT_NAME = 1

# За одну новую отметку за день
POINTS_PER_COMPLETION = 10
# Уровень: 1 при 0–99 очков, далее +1 каждые 100 очков
POINTS_PER_LEVEL = 100

TZ = ZoneInfo("Europe/Moscow")
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
JSON_LEGACY_PATH = BASE_DIR / "data.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def connect_db() -> sqlite3.Connection:
    """Подключение к SQLite; check_same_thread=False — хендлеры PTB асинхронные, запросы короткие."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in cur.fetchall()}


def init_db(conn: sqlite3.Connection) -> None:
    """Создаёт таблицы, если их ещё нет (включая поля геймификации для новых установок)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            total_points INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            best_streak INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS completions (
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            PRIMARY KEY (habit_id, day)
        );

        CREATE INDEX IF NOT EXISTS idx_habits_user ON habits(user_id);
        CREATE INDEX IF NOT EXISTS idx_completions_habit ON completions(habit_id);
        """
    )
    conn.commit()


def migrate_schema_v2(conn: sqlite3.Connection) -> None:
    """
    Добавляет колонки геймификации в существующие БД и один раз пересчитывает очки / лучшие серии.
    """
    users_cols = _table_columns(conn, "users")
    habits_cols = _table_columns(conn, "habits")
    added = False
    if "total_points" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN total_points INTEGER NOT NULL DEFAULT 0")
        added = True
    if "best_streak" not in habits_cols:
        conn.execute("ALTER TABLE habits ADD COLUMN best_streak INTEGER NOT NULL DEFAULT 0")
        added = True

    conn.commit()

    # Пересчёт только при первом появлении колонок (апгрейд со старой схемы)
    if not added:
        return

    # Пересчёт total_points: 10 * число всех отметок пользователя
    cur = conn.execute("SELECT telegram_id FROM users")
    for (tid,) in cur.fetchall():
        cur2 = conn.execute(
            """
            SELECT COUNT(*) FROM completions c
            JOIN habits h ON c.habit_id = h.id
            WHERE h.user_id = ?
            """,
            (tid,),
        )
        n = int(cur2.fetchone()[0])
        conn.execute(
            "UPDATE users SET total_points = ? WHERE telegram_id = ?",
            (n * POINTS_PER_COMPLETION, tid),
        )

    # Пересчёт best_streak по истории дат для каждой привычки
    cur = conn.execute("SELECT id FROM habits")
    for (habit_id,) in cur.fetchall():
        days = completion_days(conn, int(habit_id))
        best = longest_streak_in_history(days)
        conn.execute(
            "UPDATE habits SET best_streak = ? WHERE id = ?",
            (best, habit_id),
        )

    conn.commit()
    logger.info("Миграция схемы v2 (очки и лучшие серии) выполнена.")


def migrate_from_json_if_needed(conn: sqlite3.Connection) -> None:
    """
    Однократный перенос из legacy data.json, если файл есть, а таблица habits пуста.
    """
    cur = conn.execute("SELECT COUNT(*) FROM habits")
    if cur.fetchone()[0] > 0:
        return
    if not JSON_LEGACY_PATH.is_file():
        return
    try:
        raw = JSON_LEGACY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Не удалось прочитать data.json для миграции: %s", e)
        return
    if not isinstance(data, dict) or "users" not in data:
        return

    now = datetime.now(TZ).isoformat(timespec="seconds")
    for uid_str, udata in data.get("users", {}).items():
        try:
            telegram_id = int(uid_str)
        except ValueError:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, created_at, total_points) VALUES (?, ?, 0)",
            (telegram_id, now),
        )
        habits = (udata or {}).get("habits") or {}
        for hid in sorted(habits.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
            h = habits[hid]
            name = (h or {}).get("name") or ""
            if not name.strip():
                continue
            cur = conn.execute(
                "INSERT INTO habits (user_id, name, created_at, best_streak) VALUES (?, ?, ?, 0)",
                (telegram_id, name.strip(), now),
            )
            new_habit_id = cur.lastrowid
            for day in (h or {}).get("completions") or []:
                if isinstance(day, str) and len(day) == 10:
                    conn.execute(
                        "INSERT OR IGNORE INTO completions (habit_id, day) VALUES (?, ?)",
                        (new_habit_id, day),
                    )
    conn.commit()
    logger.info("Миграция из data.json завершена (если были данные).")


def ensure_user(conn: sqlite3.Connection, telegram_id: int) -> None:
    """Гарантирует строку в users."""
    now = datetime.now(TZ).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id, created_at, total_points) VALUES (?, ?, 0)",
        (telegram_id, now),
    )
    conn.commit()


def habits_ordered(conn: sqlite3.Connection, user_id: int) -> list[tuple[int, str]]:
    """Список (id, name) привычек пользователя по возрастанию id."""
    cur = conn.execute(
        "SELECT id, name FROM habits WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    )
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def habit_belongs_to_user(conn: sqlite3.Connection, user_id: int, habit_id: int) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM habits WHERE id = ? AND user_id = ? LIMIT 1",
        (habit_id, user_id),
    )
    return cur.fetchone() is not None


def get_habit_name(conn: sqlite3.Connection, user_id: int, habit_id: int) -> str | None:
    cur = conn.execute(
        "SELECT name FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, user_id),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def get_user_total_points(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute(
        "SELECT total_points FROM users WHERE telegram_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def level_from_points(total_points: int) -> int:
    """Уровень 1 при 0–99 очков, далее +1 каждые 100 очков."""
    return total_points // POINTS_PER_LEVEL + 1


def add_habit(conn: sqlite3.Connection, user_id: int, name: str) -> int:
    """Добавляет привычку; возвращает id новой строки."""
    ensure_user(conn, user_id)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO habits (user_id, name, created_at, best_streak) VALUES (?, ?, ?, 0)",
        (user_id, name, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def delete_habit(conn: sqlite3.Connection, user_id: int, habit_id: int) -> bool:
    """Удаляет привычку, если она принадлежит пользователю. CASCADE чистит completions."""
    cur = conn.execute(
        "DELETE FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def completion_days(conn: sqlite3.Connection, habit_id: int) -> list[str]:
    cur = conn.execute(
        "SELECT day FROM completions WHERE habit_id = ? ORDER BY day ASC",
        (habit_id,),
    )
    return [str(r[0]) for r in cur.fetchall()]


def count_completions(conn: sqlite3.Connection, habit_id: int) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM completions WHERE habit_id = ?",
        (habit_id,),
    )
    return int(cur.fetchone()[0])


def count_completions_last_days(conn: sqlite3.Connection, habit_id: int, days: int) -> int:
    """Число отметок за последние `days` календарных дней включая сегодня."""
    today = datetime.now(TZ).date()
    start = today - timedelta(days=days - 1)
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM completions
        WHERE habit_id = ? AND day >= ? AND day <= ?
        """,
        (habit_id, start.isoformat(), today.isoformat()),
    )
    return int(cur.fetchone()[0])


def total_completions_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    """Всего отметок по всем привычкам пользователя."""
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM completions c
        JOIN habits h ON c.habit_id = h.id
        WHERE h.user_id = ?
        """,
        (user_id,),
    )
    return int(cur.fetchone()[0])


def count_habits_user(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM habits WHERE user_id = ?",
        (user_id,),
    )
    return int(cur.fetchone()[0])


def max_best_streak_among_habits(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute(
        "SELECT COALESCE(MAX(best_streak), 0) FROM habits WHERE user_id = ?",
        (user_id,),
    )
    return int(cur.fetchone()[0])


def today_str() -> str:
    return datetime.now(TZ).date().isoformat()


def streak_for_habit(completion_dates: list[str], today: str) -> int:
    """Текущая серия: подряд идущие дни, заканчивающиеся сегодня или вчера."""
    present = set(completion_dates)
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


def longest_streak_in_history(sorted_days: list[str]) -> int:
    """
    Максимальная длина отрезка подряд идущих календарных дней в истории (по уникальным датам).
    """
    if not sorted_days:
        return 0
    uniq = sorted(set(sorted_days))
    best = 1
    cur_len = 1
    for i in range(1, len(uniq)):
        prev_d = date.fromisoformat(uniq[i - 1])
        cur_d = date.fromisoformat(uniq[i])
        if cur_d == prev_d + timedelta(days=1):
            cur_len += 1
            best = max(best, cur_len)
        else:
            cur_len = 1
    return best


def mark_done_and_reward(
    conn: sqlite3.Connection,
    user_id: int,
    habit_id: int,
    day: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Атомарно: вставка отметки, +10 очков, обновление best_streak.
    Возвращает (успех, код_ошибки_или_имя, данные_для_сообщения_или_None).
    При успехе dict: name, points_added, total_points, level, current_streak, best_streak.
    """
    name = get_habit_name(conn, user_id, habit_id)
    if name is None:
        return False, "not_found", None

    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO completions (habit_id, day) VALUES (?, ?)",
            (habit_id, day),
        )
        conn.execute(
            "UPDATE users SET total_points = total_points + ? WHERE telegram_id = ?",
            (POINTS_PER_COMPLETION, user_id),
        )
        days = completion_days(conn, habit_id)
        today = today_str()
        current = streak_for_habit(days, today)
        cur = conn.execute(
            "SELECT best_streak FROM habits WHERE id = ?",
            (habit_id,),
        )
        row = cur.fetchone()
        old_best = int(row[0]) if row else 0
        new_best = max(old_best, current)
        conn.execute(
            "UPDATE habits SET best_streak = ? WHERE id = ?",
            (new_best, habit_id),
        )
        total_pts = int(
            conn.execute(
                "SELECT total_points FROM users WHERE telegram_id = ?",
                (user_id,),
            ).fetchone()[0]
        )
        conn.commit()
        lvl = level_from_points(total_pts)
        return True, name, {
            "name": name,
            "points_added": POINTS_PER_COMPLETION,
            "total_points": total_pts,
            "level": lvl,
            "current_streak": current,
            "best_streak": new_best,
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        return False, "already", None
    except sqlite3.Error as e:
        conn.rollback()
        logger.exception("SQLite при отметке и начислении: %s", e)
        return False, "db", None


def build_chart_last_7_days(
    conn: sqlite3.Connection,
    user_id: int,
) -> Path | None:
    """
    Столбчатый график: по X — названия привычек, по Y — число отметок за последние 7 дней.
    Возвращает путь к временному PNG или None, если показывать нечего.
    """
    ordered = habits_ordered(conn, user_id)
    if not ordered:
        return None
    names: list[str] = []
    counts: list[int] = []
    for hid, name in ordered:
        c = count_completions_last_days(conn, hid, 7)
        names.append(name if len(name) <= 24 else name[:21] + "…")
        counts.append(c)
    if sum(counts) == 0:
        return None

    fig, ax = plt.subplots(figsize=(max(6, len(names) * 0.9), 4))
    x_pos = range(len(names))
    ax.bar(x_pos, counts, color="#4CAF50", edgecolor="#2E7D32")
    ax.set_xticks(list(x_pos))
    ax.set_xticklabels(names, rotation=25, ha="right", fontsize=8)
    ax.set_ylabel("Отметок за 7 дней")
    ax.set_title("Привычки: активность за последние 7 дней")
    fig.tight_layout()

    # Временный PNG в системном каталоге (/tmp и т.д.) — надёжнее на хостинге (Railway), чем рядом с кодом
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        fig.savefig(tmp_path, dpi=120, bbox_inches="tight")
    finally:
        plt.close(fig)

    return tmp_path


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_ADD, BTN_LIST],
            [BTN_DONE, BTN_STATS],
            [BTN_PROGRESS, BTN_CHART],
            [BTN_DELETE, BTN_HELP],
        ],
        resize_keyboard=True,
    )


def cancel_only_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)


def habits_inline_keyboard(prefix: str, ordered: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name[:64], callback_data=f"{prefix}:{hid}")]
        for hid, name in ordered
    ]
    return InlineKeyboardMarkup(rows)


# --- Хендлеры (context.bot_data['db'] — соединение) ---


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Привет! Я бот для отслеживания привычек.\n\n"
        "Всё управление — кнопками внизу экрана.\n"
        "За каждую новую отметку за день начисляются очки и растёт уровень.\n"
        "Чтобы добавить привычку: «Создать привычку», затем название одним сообщением.\n\n"
        "Данные в SQLite (database.db).",
        reply_markup=main_keyboard(),
    )


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Что делают кнопки:\n\n"
        f"{BTN_ADD} — название привычки текстом.\n"
        f"{BTN_LIST} — список привычек.\n"
        f"{BTN_DONE} — отметить сегодня (начисляются очки).\n"
        f"{BTN_STATS} — подробная статистика и лидеры за 7 дней.\n"
        f"{BTN_PROGRESS} — очки, уровень, всего выполнений.\n"
        f"{BTN_CHART} — график активности за 7 дней.\n"
        f"{BTN_DELETE} — удалить привычку.\n\n"
        f"За отметку: +{POINTS_PER_COMPLETION} очков; уровень растёт каждые {POINTS_PER_LEVEL} очков.",
        reply_markup=main_keyboard(),
    )


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    conn: sqlite3.Connection = context.bot_data["db"]
    try:
        ensure_user(conn, update.effective_user.id)
        ordered = habits_ordered(conn, update.effective_user.id)
    except sqlite3.Error as e:
        logger.exception("Ошибка БД: %s", e)
        await update.message.reply_text("Не удалось загрузить данные. Попробуй позже.")
        return

    if not ordered:
        await update.message.reply_text(
            "Пока нет привычек. Нажми «Создать привычку».",
            reply_markup=main_keyboard(),
        )
        return
    lines = [f"{i}. {name}" for i, (_hid, name) in enumerate(ordered, start=1)]
    await update.message.reply_text(
        "Твои привычки:\n" + "\n".join(lines),
        reply_markup=main_keyboard(),
    )


async def show_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экран «Мой прогресс»: привычки, выполнения, очки, уровень, лучшая серия среди привычек."""
    if update.effective_user is None or update.message is None:
        return
    conn: sqlite3.Connection = context.bot_data["db"]
    uid = update.effective_user.id
    try:
        ensure_user(conn, uid)
        n_habits = count_habits_user(conn, uid)
        n_done = total_completions_for_user(conn, uid)
        pts = get_user_total_points(conn, uid)
        lvl = level_from_points(pts)
        best_among = max_best_streak_among_habits(conn, uid)
    except sqlite3.Error as e:
        logger.exception("Ошибка БД: %s", e)
        await update.message.reply_text("Не удалось загрузить прогресс. Попробуй позже.")
        return

    text = (
        "📈 Мой прогресс\n\n"
        f"Привычек: {n_habits}\n"
        f"Всего выполнений (отметок): {n_done}\n"
        f"Очков: {pts}\n"
        f"Уровень: {lvl}\n"
        f"Лучшая серия среди привычек (дней подряд за всё время): {best_among}\n"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    conn: sqlite3.Connection = context.bot_data["db"]
    uid = update.effective_user.id
    today = today_str()
    try:
        ensure_user(conn, uid)
        ordered = habits_ordered(conn, uid)
    except sqlite3.Error as e:
        logger.exception("Ошибка БД: %s", e)
        await update.message.reply_text("Не удалось загрузить статистику. Попробуй позже.")
        return

    if not ordered:
        await update.message.reply_text(
            "Пока нет данных. Создай привычку кнопкой ниже.",
            reply_markup=main_keyboard(),
        )
        return

    lines: list[str] = [
        "Статистика.\n",
        "По каждой привычке: всего отметок; за 7 дней; текущая серия; лучшая серия (за всё время).\n",
    ]
    last7_list: list[tuple[str, int]] = []

    for habit_id, name in ordered:
        try:
            total = count_completions(conn, habit_id)
            last7 = count_completions_last_days(conn, habit_id, 7)
            days = completion_days(conn, habit_id)
            streak = streak_for_habit(days, today)
            cur = conn.execute(
                "SELECT best_streak FROM habits WHERE id = ?",
                (habit_id,),
            )
            best_row = cur.fetchone()
            best_s = int(best_row[0]) if best_row else 0
        except sqlite3.Error as e:
            logger.exception("Ошибка БД при статистике: %s", e)
            await update.message.reply_text("Ошибка при расчёте статистики.", reply_markup=main_keyboard())
            return
        last7_list.append((name, last7))
        lines.append(
            f"• {name}\n"
            f"  всего: {total}; за 7 дней: {last7}; серия сейчас: {streak}; рекорд серии: {best_s}"
        )

    # Самая активная и наименее активная за 7 дней (среди всех привычек, включая нули)
    max_c = max(c for _, c in last7_list)
    min_c = min(c for _, c in last7_list)
    top_names = [n for n, c in last7_list if c == max_c]
    low_names = [n for n, c in last7_list if c == min_c]

    lines.append("")
    if max_c == 0:
        lines.append(
            "За последние 7 дней пока нет ни одной отметки — лидеров нет. Отмечай привычки кнопкой «Отметить»."
        )
    else:
        lines.append(
            "Самая активная привычка за 7 дней: "
            + ", ".join(f"«{n}»" for n in top_names)
            + f" ({max_c} отм.)"
        )
        lines.append(
            "Наименьшее число отметок за 7 дней: "
            + ", ".join(f"«{n}»" for n in low_names)
            + f" ({min_c} отм.)"
        )

    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def send_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет столбчатый график за 7 дней или сообщение, если данных нет."""
    if update.effective_user is None or update.message is None:
        return
    conn: sqlite3.Connection = context.bot_data["db"]
    uid = update.effective_user.id
    path: Path | None = None
    try:
        ensure_user(conn, uid)
        path = build_chart_last_7_days(conn, uid)
    except sqlite3.Error as e:
        logger.exception("Ошибка БД при графике: %s", e)
        await update.message.reply_text("Не удалось построить график. Попробуй позже.")
        return
    except Exception as e:
        logger.exception("Ошибка matplotlib: %s", e)
        await update.message.reply_text(
            "Не удалось построить график (ошибка отрисовки).",
            reply_markup=main_keyboard(),
        )
        return

    if path is None:
        await update.message.reply_text(
            "Нет данных за последние 7 дней: сначала отметь хотя бы одну привычку "
            "или добавь привычки. График пустой строить не буду.",
            reply_markup=main_keyboard(),
        )
        return

    try:
        with open(path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="Активность за последние 7 дней.",
                reply_markup=main_keyboard(),
            )
    except OSError as e:
        logger.exception("Не удалось отправить файл графика: %s", e)
        await update.message.reply_text("Не удалось отправить изображение.", reply_markup=main_keyboard())
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Не удалось удалить временный файл графика: %s", e)


async def prompt_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return
    conn: sqlite3.Connection = context.bot_data["db"]
    try:
        ensure_user(conn, update.effective_user.id)
        ordered = habits_ordered(conn, update.effective_user.id)
    except sqlite3.Error as e:
        logger.exception("Ошибка БД: %s", e)
        await update.message.reply_text("Не удалось загрузить привычки.")
        return

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
    conn: sqlite3.Connection = context.bot_data["db"]
    try:
        ensure_user(conn, update.effective_user.id)
        ordered = habits_ordered(conn, update.effective_user.id)
    except sqlite3.Error as e:
        logger.exception("Ошибка БД: %s", e)
        await update.message.reply_text("Не удалось загрузить привычки.")
        return

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
    m = re.match(r"^done:(\d+)$", query.data)
    if not m:
        return
    habit_id = int(m.group(1))
    uid = update.effective_user.id
    conn: sqlite3.Connection = context.bot_data["db"]
    day = today_str()

    if not habit_belongs_to_user(conn, uid, habit_id):
        await query.message.reply_text("Привычка не найдена. Обнови список.")
        return

    ok, info, data = mark_done_and_reward(conn, uid, habit_id, day)
    if not ok:
        if info == "already":
            name = get_habit_name(conn, uid, habit_id) or ""
            await query.message.reply_text(
                f"«{name}» уже отмечена на сегодня.",
                reply_markup=main_keyboard(),
            )
        else:
            await query.message.reply_text(
                "Не удалось сохранить отметку.",
                reply_markup=main_keyboard(),
            )
        await query.edit_message_reply_markup(reply_markup=None)
        return

    await query.edit_message_reply_markup(reply_markup=None)
    assert data is not None
    msg = (
        f"Отлично! «{data['name']}» отмечена на {day}.\n\n"
        f"Начислено очков: +{data['points_added']}.\n"
        f"Всего очков: {data['total_points']}.\n"
        f"Уровень: {data['level']}.\n"
        f"Текущая серия: {data['current_streak']} дн.\n"
        f"Лучшая серия по этой привычке: {data['best_streak']} дн."
    )
    await query.message.reply_text(msg, reply_markup=main_keyboard())


async def on_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    await query.answer()
    m = re.match(r"^del:(\d+)$", query.data)
    if not m:
        return
    habit_id = int(m.group(1))
    uid = update.effective_user.id
    conn: sqlite3.Connection = context.bot_data["db"]

    name = get_habit_name(conn, uid, habit_id)
    if name is None:
        await query.message.reply_text("Привычка уже удалена или не найдена.")
        await query.edit_message_reply_markup(reply_markup=None)
        return

    try:
        delete_habit(conn, uid, habit_id)
    except sqlite3.Error as e:
        logger.exception("Ошибка удаления: %s", e)
        await query.message.reply_text("Не удалось удалить.", reply_markup=main_keyboard())
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"Удалено: «{name}».", reply_markup=main_keyboard())


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

    conn: sqlite3.Connection = context.bot_data["db"]
    try:
        add_habit(conn, update.effective_user.id, text)
    except sqlite3.Error as e:
        logger.exception("Ошибка добавления привычки: %s", e)
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
    if update.message:
        await update.message.reply_text(
            "Ввод названия отменён. Ниже снова главное меню.",
            reply_markup=main_keyboard(),
        )
    return ConversationHandler.END


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        logger.error(
            "Не задан BOT_TOKEN. Локально: файл .env. На Railway: Variables → BOT_TOKEN."
        )
        raise SystemExit(1)
    logger.info("BOT_TOKEN загружен из окружения (длина %s символов)", len(token))

    conn = connect_db()
    try:
        init_db(conn)
        migrate_from_json_if_needed(conn)
        migrate_schema_v2(conn)
    except sqlite3.Error as e:
        logger.exception("Не удалось инициализировать БД: %s", e)
        conn.close()
        raise SystemExit(1) from e

    ptb_app = Application.builder().token(token).build()
    ptb_app.bot_data["db"] = conn

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

    ptb_app.add_handler(add_conv)
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_HELP)}$"), show_help))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_LIST)}$"), show_list))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STATS)}$"), show_stats))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_PROGRESS)}$"), show_progress))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_CHART)}$"), send_chart))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DONE)}$"), prompt_done))
    ptb_app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"), prompt_delete))
    ptb_app.add_handler(CallbackQueryHandler(on_done_callback, pattern=r"^done:\d+$"))
    ptb_app.add_handler(CallbackQueryHandler(on_delete_callback, pattern=r"^del:\d+$"))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    ptb_app.add_error_handler(on_error)

    # --- Flask webhook server ---
    flask_app = Flask(__name__)

    # Shared event loop that Flask routes post updates into
    loop = asyncio.new_event_loop()

    @flask_app.get("/")
    def health_check() -> Response:
        return Response("OK", status=200, mimetype="text/plain")

    @flask_app.post("/webhook")
    def webhook() -> Response:
        data = request.get_json(force=True, silent=True)
        if data is None:
            return Response("Bad Request", status=400, mimetype="text/plain")
        try:
            update = Update.de_json(data, ptb_app.bot)
            asyncio.run_coroutine_threadsafe(
                ptb_app.process_update(update), loop
            ).result(timeout=60)
        except Exception as exc:
            logger.exception("Ошибка при обработке webhook-update: %s", exc)
            return Response("Internal Server Error", status=500, mimetype="text/plain")
        return Response("OK", status=200, mimetype="text/plain")

    async def run_ptb() -> None:
        """Initialise PTB and keep the event loop alive for update processing."""
        await ptb_app.initialize()
        await ptb_app.start()
        logger.info("PTB запущен в режиме webhook; база данных: %s", DB_PATH)
        # Run forever — Flask runs in the main thread, PTB lives in this loop
        await asyncio.Event().wait()

    import threading

    def start_event_loop() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_ptb())
        finally:
            loop.run_until_complete(ptb_app.stop())
            loop.run_until_complete(ptb_app.shutdown())
            conn.close()
            logger.info("Соединение с БД закрыто")

    ptb_thread = threading.Thread(target=start_event_loop, daemon=True)
    ptb_thread.start()

    port = 443
    logger.info("Запуск Flask на порту %s", port)
    flask_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
