import os
import re
import csv
import asyncio
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

AMOUNT_PATTERN = re.compile(r"^-?\d+(?:[.,]\d+)?$")

DATABASE_URL = os.getenv("DATABASE_URL")

POOL: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global POOL
    if POOL is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не установлен!")
        POOL = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=5,
            timeout=10,
            open=True,
        )
    return POOL


def init_db():
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, name)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    amount REAL,
                    category TEXT,
                    description TEXT,
                    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date DESC)"
            )

            # миграция со старой структуры (если уже были расходы)
            c.execute(
                """
                INSERT INTO users (id)
                SELECT DISTINCT user_id
                FROM expenses
                WHERE user_id IS NOT NULL
                ON CONFLICT DO NOTHING
                """
            )
            c.execute(
                """
                INSERT INTO categories (user_id, name)
                SELECT DISTINCT user_id, category
                FROM expenses
                WHERE user_id IS NOT NULL AND category IS NOT NULL
                ON CONFLICT DO NOTHING
                """
            )
    print("✅ База данных инициализирована")


def ensure_user(user):
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO users (id, username, first_name, last_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name
                """,
                (user.id, user.username, user.first_name, user.last_name),
            )


def ensure_category(user_id: int, category: str):
    category = (category or "").strip().lower()
    if not category:
        return
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO categories (user_id, name)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (user_id, category),
            )


def delete_category(user_id: int, category: str) -> int:
    category = (category or "").strip().lower()
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM categories WHERE user_id=%s AND name=%s",
                (user_id, category),
            )
            return c.rowcount


def add_expense(user_id: int, amount: float, category: str, description: str):
    category = (category or "").strip().lower()
    ensure_category(user_id, category)
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO expenses (user_id, amount, category, description, date)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, amount, category, description, datetime.now()),
            )


def delete_expense(user_id: int, expense_id: int) -> int:
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM expenses WHERE id=%s AND user_id=%s",
                (expense_id, user_id),
            )
            return c.rowcount


def get_expenses(user_id: int, days: Optional[int] = None):
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as c:
            if days:
                date_from = datetime.now() - timedelta(days=days)
                c.execute(
                    "SELECT * FROM expenses WHERE user_id=%s AND date >= %s ORDER BY date DESC",
                    (user_id, date_from),
                )
            else:
                c.execute(
                    "SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC",
                    (user_id,),
                )
            return c.fetchall()


def get_last_expenses(user_id: int, limit: int = 10):
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as c:
            c.execute(
                "SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC LIMIT %s",
                (user_id, limit),
            )
            return c.fetchall()


def get_categories_list(user_id: int):
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT name FROM categories WHERE user_id=%s ORDER BY name", (user_id,))
            categories = [row[0] for row in c.fetchall()]
            if categories:
                return categories
            c.execute("SELECT DISTINCT category FROM expenses WHERE user_id=%s ORDER BY category", (user_id,))
            return [row[0] for row in c.fetchall()]


# ---------------- Parsing ----------------

def parse_expense_message(text: str):
    parts = text.split()
    if len(parts) < 2:
        return None

    category = parts[0].lower()
    amount = None
    amount_index = -1

    for i in range(1, len(parts)):
        cleaned = parts[i].replace("₸", "").replace(",", ".")
        if AMOUNT_PATTERN.match(cleaned):
            amount = float(cleaned)
            amount_index = i
            break

    if amount is None:
        return None

    description = category if amount_index == 1 else " ".join(parts[1:amount_index])
    return category, description, amount


def parse_amount_only_message(text: str, default_desc: str) -> Optional[Tuple[str, float]]:
    parts = text.split()
    if not parts:
        return None

    amount = None
    amount_index = -1
    for i, p in enumerate(parts):
        cleaned = p.replace("₸", "").replace(",", ".")
        if AMOUNT_PATTERN.match(cleaned):
            amount = float(cleaned)
            amount_index = i
            break

    if amount is None:
        return None

    if amount_index <= 0:
        desc = default_desc
    else:
        desc = " ".join(parts[:amount_index]).strip() or default_desc

    return desc, amount


# ---------------- Panel helpers (anti-spam) ----------------

PANEL_KEY = "panel_msg_id"


async def panel_show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Редактируем одно меню-сообщение. Если нет — создаём и запоминаем."""
    chat_id = update.effective_chat.id
    msg_id = context.user_data.get(PANEL_KEY)

    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass

    sent = await update.effective_message.reply_text(text, reply_markup=reply_markup)
    context.user_data[PANEL_KEY] = sent.message_id


# ---------------- Keyboards ----------------

def kb_main():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить трату", callback_data="exp:new:0")],
            [InlineKeyboardButton("📈 Отчёт", callback_data="m:report")],
            [InlineKeyboardButton("📤 Экспорт CSV", callback_data="m:export")],
            [InlineKeyboardButton("🧾 Последние (удалить)", callback_data="m:last")],
            [InlineKeyboardButton("📂 Категории", callback_data="m:categories")],
            [InlineKeyboardButton("🗑 Очистить все", callback_data="do:clear")],
        ]
    )


def kb_categories_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📌 Показать категории", callback_data="cat:list")],
            [
                InlineKeyboardButton("➕ Добавить", callback_data="cat:add"),
                InlineKeyboardButton("➖ Удалить", callback_data="cat:del"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:main")],
        ]
    )


def kb_back_cancel(back_cb: str):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)],
            [InlineKeyboardButton("❌ Отмена", callback_data="do:cancel")],
        ]
    )


def kb_report_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="r:today"),
                InlineKeyboardButton("Неделя", callback_data="r:week"),
            ],
            [
                InlineKeyboardButton("Месяц", callback_data="r:month"),
                InlineKeyboardButton("Всё время", callback_data="r:all"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:main")],
        ]
    )


def kb_export_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="x:today"),
                InlineKeyboardButton("Неделя", callback_data="x:week"),
            ],
            [
                InlineKeyboardButton("Месяц", callback_data="x:month"),
                InlineKeyboardButton("Всё время", callback_data="x:all"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:main")],
        ]
    )


def kb_report_result(period_key: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔁 Обновить", callback_data=f"rr:{period_key}"),
                InlineKeyboardButton("📤 Экспорт CSV", callback_data=f"rx:{period_key}"),
            ],
            [InlineKeyboardButton("⬅️ В меню", callback_data="m:main")],
        ]
    )


def kb_pick_category(user_id: int, context: ContextTypes.DEFAULT_TYPE, page: int = 0, page_size: int = 10):
    cats = get_categories_list(user_id)
    if not cats:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Создать категорию", callback_data="cat:add")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="m:main")],
            ]
        )

    context.user_data["cats_full"] = cats

    total = len(cats)
    max_page = max(0, (total - 1) // page_size)
    page = max(0, min(page, max_page))

    start = page * page_size
    end = min(start + page_size, total)
    slice_cats = list(enumerate(cats[start:end], start=start))

    buttons = []
    row = []
    for abs_idx, name in slice_cats:
        row.append(InlineKeyboardButton(name, callback_data=f"exp:cat:{abs_idx}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"exp:new:{page-1}"))
    nav.append(InlineKeyboardButton(f"Стр {page+1}/{max_page+1}", callback_data="noop"))
    if page < max_page:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"exp:new:{page+1}"))
    buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ Новая категория", callback_data="cat:add")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="m:main")])
    return InlineKeyboardMarkup(buttons)


def kb_last_expenses(expenses: List[dict]):
    buttons = []
    for exp in expenses:
        exp_id = exp["id"]
        buttons.append([InlineKeyboardButton(f"🗑 Удалить #{exp_id}", callback_data=f"e:del:{exp_id}")])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="m:last_refresh")])
    buttons.append([InlineKeyboardButton("⬅️ В меню", callback_data="m:main")])
    return InlineKeyboardMarkup(buttons)


# ---------------- Report/export helpers ----------------

def period_to_days(key: str) -> Tuple[Optional[int], str]:
    if key == "today":
        return 1, "сегодня"
    if key == "week":
        return 7, "неделю"
    if key == "month":
        return 30, "месяц"
    return None, "всё время"


async def send_report(update: Update, context: ContextTypes.DEFAULT_TYPE, period_key: str):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    days, period_name = period_to_days(period_key)
    expenses = get_expenses(user_id, days=days)

    if not expenses:
        await update.effective_message.reply_text(f"📭 За {period_name} расходов нет.")
        return

    total = sum(float(e["amount"] or 0) for e in expenses)
    by_cat = {}
    for e in expenses:
        cat = e["category"] or "без категории"
        by_cat[cat] = by_cat.get(cat, 0.0) + float(e["amount"] or 0)

    top_cats = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)[:8]
    top_exp = sorted(expenses, key=lambda x: float(x["amount"] or 0), reverse=True)[:5]

    text = f"📈 Отчёт за {period_name}\n"
    text += f"💵 Итого: {total:.0f} ₸\n\n"
    text += "🏷 Топ категорий:\n"
    for cat, amt in top_cats:
        pct = (amt / total * 100) if total > 0 else 0
        text += f"• {cat}: {amt:.0f} ₸ ({pct:.1f}%)\n"

    text += "\n💎 Топ трат:\n"
    for e in top_exp:
        amt = float(e["amount"] or 0)
        cat = e["category"] or "—"
        desc = e["description"] or ""
        dt = e["date"].strftime("%d.%m %H:%M") if e["date"] else ""
        text += f"• {amt:.0f} ₸ — {cat} / {desc} ({dt})\n"

    await update.effective_message.reply_text(text, reply_markup=kb_report_result(period_key))


async def send_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE, period_key: str):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    days, period_name = period_to_days(period_key)
    expenses = get_expenses(user_id, days=days)

    if not expenses:
        await update.effective_message.reply_text(f"📭 За {period_name} нечего экспортировать.")
        return

    output = BytesIO()
    output.write("date,category,description,amount\n".encode("utf-8"))
    writer = csv.writer(output)

    for e in reversed(expenses):
        dt = e["date"].strftime("%Y-%m-%d %H:%M:%S") if e["date"] else ""
        writer.writerow([dt, e["category"], e["description"], float(e["amount"] or 0)])

    output.seek(0)
    filename = f"expenses_{period_key}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    await update.effective_message.reply_document(
        document=InputFile(output, filename=filename),
        caption="📤 Экспорт расходов (CSV)",
    )


async def render_last_text(expenses: List[dict]) -> str:
    if not expenses:
        return "📭 Трат пока нет."
    text = "🧾 Последние 10 трат:\n\n"
    for e in expenses:
        exp_id = e["id"]
        amt = float(e["amount"] or 0)
        cat = e["category"] or "—"
        desc = e["description"] or ""
        dt = e["date"].strftime("%d.%m %H:%M") if e["date"] else ""
        text += f"#{exp_id} • {cat} / {desc} — {amt:.0f} ₸ ({dt})\n"
    return text


async def show_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    expenses = get_last_expenses(user_id, limit=10)
    text = await render_last_text(expenses)
    markup = kb_last_expenses(expenses) if expenses else InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ В меню", callback_data="m:main")]]
    )

    if update.callback_query and edit_message:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass

    await update.effective_message.reply_text(text, reply_markup=markup)


async def categories_list_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    categories = get_categories_list(user_id)
    if not categories:
        await update.effective_message.reply_text("📭 Категорий пока нет.")
        return

    expenses = get_expenses(user_id, days=30)
    stats = {}
    for exp in expenses:
        stats[exp["category"]] = stats.get(exp["category"], 0) + float(exp["amount"] or 0)

    text = "📂 Категории (за 30 дней):\n\n"
    for cat in categories:
        text += f"• {cat}: {stats.get(cat, 0):.0f} ₸\n"
    await update.effective_message.reply_text(text)


async def clear_data_db(user_id: int) -> int:
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM expenses WHERE user_id=%s", (user_id,))
            return c.rowcount



# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("selected_category", None)

    text = (
        "🎯 Меню\n\n"
        "• Добавить трату: кнопкой (категории) или текстом\n"
        "  <категория> <название> <сумма>\n"
        "  Пример: еда дельпапа 12000"
    )
    await panel_show(update, context, text, reply_markup=kb_main())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ВАЖНО: удаляем ВСЕ сообщения пользователя после обработки (успех/ошибка).
    Чтобы не засорять чат.
    """
    ensure_user(update.effective_user)
    user_msg = update.message
    text = (user_msg.text or "").strip()
    if not text:
        # даже пустое — удалим
        try:
            await user_msg.delete()
        except Exception:
            pass
        return

    try:
        awaiting = context.user_data.get("awaiting")

        # add category
        if awaiting == "add_category":
            category = text.lower().strip()
            if not category:
                await panel_show(update, context, "❌ Категория не может быть пустой.", reply_markup=kb_back_cancel("m:categories"))
                return
            ensure_category(update.effective_user.id, category)
            context.user_data.pop("awaiting", None)
            await panel_show(update, context, f"✅ Категория добавлена: {category}", reply_markup=kb_categories_menu())
            return

        # del category
        if awaiting == "del_category":
            category = text.lower().strip()
            if not category:
                await panel_show(update, context, "❌ Категория не может быть пустой.", reply_markup=kb_back_cancel("m:categories"))
                return
            deleted = delete_category(update.effective_user.id, category)
            context.user_data.pop("awaiting", None)
            msg = f"🗑️ Категория удалена: {category}" if deleted else f"⚠️ Категория не найдена: {category}"
            await panel_show(update, context, msg, reply_markup=kb_categories_menu())
            return

        # expense in chosen category
        if awaiting == "expense_in_category":
            category = context.user_data.get("selected_category")
            if not category:
                context.user_data.pop("awaiting", None)
                await panel_show(update, context, "⚠️ Категория не выбрана.", reply_markup=kb_main())
                return

            parsed2 = parse_amount_only_message(text, default_desc=category)
            if not parsed2:
                await panel_show(
                    update,
                    context,
                    "❌ Не нашёл сумму.\n\nПиши: <название> <сумма> (пример: дельпапа 12000)\nИли просто сумму: 2000",
                    reply_markup=kb_back_cancel("exp:new:0"),
                )
                return

            description, amount = parsed2
            add_expense(update.effective_user.id, amount, category, description)

            context.user_data.pop("awaiting", None)
            context.user_data.pop("selected_category", None)

            # подтверждение в панели (без спама)
            await panel_show(update, context, f"✅ Записано: {category} / {description} — {amount:.0f} ₸", reply_markup=kb_main())
            return

        # raw expense
        parsed = parse_expense_message(text)
        if not parsed:
            await panel_show(
                update,
                context,
                "❌ Не понял формат.\nПиши: <категория> <название> <сумма>\nПример: еда дельпапа 12000",
                reply_markup=kb_main(),
            )
            return

        category, description, amount = parsed
        add_expense(update.effective_user.id, amount, category, description)
        await panel_show(update, context, f"✅ Записано: {category} / {description} — {amount:.0f} ₸", reply_markup=kb_main())

    finally:
        # удаляем сообщение пользователя ВСЕГДА
        try:
            await user_msg.delete()
        except Exception:
            pass


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ensure_user(update.effective_user)
    data = query.data or ""

    if data == "noop":
        return

    if data == "do:cancel":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "Ок, отменил 👌", reply_markup=kb_main())
        return

    if data == "m:main":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "🎯 Меню", reply_markup=kb_main())
        return

    if data == "m:categories":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "📂 Категории", reply_markup=kb_categories_menu())
        return

    if data == "m:report":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "📈 Отчёт: выбери период", reply_markup=kb_report_menu())
        return

    if data == "m:export":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "📤 Экспорт CSV: выбери период", reply_markup=kb_export_menu())
        return

    if data == "m:last":
        await show_last_message(update, context, edit_message=False)
        return

    if data == "m:last_refresh":
        await show_last_message(update, context, edit_message=True)
        return

    if data == "do:clear":
    context.user_data.pop("awaiting", None)
    context.user_data.pop("selected_category", None)

    deleted = clear_data_db(update.effective_user.id)

    await panel_show(
        update,
        context,
        f"🗑️ Удалено {deleted} записей.",
        reply_markup=kb_main(),
    )
    return

    if data == "cat:list":
        await categories_list_message(update, context)
        return

    if data == "cat:add":
        context.user_data["awaiting"] = "add_category"
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "Напиши название категории (например: кафе)", reply_markup=kb_back_cancel("m:categories"))
        return

    if data == "cat:del":
        context.user_data["awaiting"] = "del_category"
        context.user_data.pop("selected_category", None)
        await panel_show(update, context, "Напиши категорию, которую удалить (например: кафе)", reply_markup=kb_back_cancel("m:categories"))
        return

    if data.startswith("exp:new:"):
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        page = int(data.split(":")[-1])
        await panel_show(update, context, "Выбери категорию 👇", reply_markup=kb_pick_category(update.effective_user.id, context, page=page))
        return

    if data.startswith("exp:cat:"):
        abs_idx = int(data.split(":")[-1])
        cats = context.user_data.get("cats_full") or get_categories_list(update.effective_user.id)
        if abs_idx < 0 or abs_idx >= len(cats):
            await panel_show(update, context, "⚠️ Категория не найдена. Открой выбор заново.", reply_markup=kb_main())
            return

        category = cats[abs_idx]
        context.user_data["selected_category"] = category
        context.user_data["awaiting"] = "expense_in_category"
        await panel_show(
            update,
            context,
            f"📌 Категория: {category}\n\nНапиши: <название> <сумма>\nПример: дельпапа 12000\nИли просто сумму: 2000",
            reply_markup=kb_back_cancel("exp:new:0"),
        )
        return

    if data.startswith("r:"):
        period_key = data.split(":")[-1]
        await send_report(update, context, period_key)
        return

    if data.startswith("rr:"):
        period_key = data.split(":")[-1]
        await send_report(update, context, period_key)
        return

    if data.startswith("x:"):
        period_key = data.split(":")[-1]
        await send_export_csv(update, context, period_key)
        return

    if data.startswith("rx:"):
        period_key = data.split(":")[-1]
        await send_export_csv(update, context, period_key)
        return

    if data.startswith("e:del:"):
        exp_id = int(data.split(":")[-1])
        delete_expense(update.effective_user.id, exp_id)
        await show_last_message(update, context, edit_message=True)
        return


# ---------------- Main (manual polling, faster) ----------------

def main():
    try:
        init_db()
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        return

    token = os.getenv("BOT_TOKEN")
    if not token:
        print("❌ Ошибка: BOT_TOKEN не установлен!")
        return

    application = Application.builder().token(token).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Бот запущен (pool + faster polling + delete user messages)")

    async def runner():
        await application.initialize()
        await application.start()

        offset = None
        try:
            while True:
                updates = await application.bot.get_updates(
                    offset=offset,
                    timeout=10,  # было 30
                    allowed_updates=Update.ALL_TYPES,
                )
                if not updates:
                    await asyncio.sleep(0.05)
                    continue

                for upd in updates:
                    offset = upd.update_id + 1
                    await application.process_update(upd)
        finally:
            await application.stop()
            await application.shutdown()
            global POOL
            try:
                if POOL:
                    POOL.close()
            except Exception:
                pass

    asyncio.run(runner())


if __name__ == "__main__":
    main()
