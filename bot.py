import os
import re
import csv
import asyncio
from io import BytesIO
from datetime import datetime, timedelta, date
from typing import Optional, Tuple, List, Callable, Any, Dict

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

PANEL_KEY = "panel_msg_id"
PANEL_RESET_TASK_KEY = "panel_reset_task"

DEFAULT_CATEGORIES = [
    "продукты",
    "кафе",
    "такси",
    "развлечения",
    "платежи",
    "разное",
]


# ---------------- Pool / DB runner ----------------

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


async def run_db(fn: Callable[..., Any], *args, **kwargs):
    """Run sync DB function in a thread to avoid blocking event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------- DB (SYNC) ----------------

def init_db_sync():
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
            c.execute("CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date DESC)")

            # migration helpers (safe)
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


def ensure_user_sync(user):
    """Upsert user + ensure default categories."""
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
            for cat in DEFAULT_CATEGORIES:
                c.execute(
                    """
                    INSERT INTO categories (user_id, name)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (user.id, cat),
                )


def ensure_category_sync(user_id: int, category: str):
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


def delete_category_sync(user_id: int, category: str) -> int:
    category = (category or "").strip().lower()
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM categories WHERE user_id=%s AND name=%s", (user_id, category))
            return c.rowcount


def add_expense_sync(user_id: int, amount: float, category: str, description: str) -> int:
    """Insert expense and return inserted id."""
    category = (category or "").strip().lower()
    ensure_category_sync(user_id, category)
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO expenses (user_id, amount, category, description, date)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (user_id, amount, category, description, datetime.now()),
            )
            row = c.fetchone()
            return int(row[0]) if row else 0


def update_expense_amount_sync(user_id: int, expense_id: int, new_amount: float) -> int:
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE expenses SET amount=%s WHERE id=%s AND user_id=%s",
                (new_amount, expense_id, user_id),
            )
            return c.rowcount


def delete_expense_sync(user_id: int, expense_id: int) -> int:
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM expenses WHERE id=%s AND user_id=%s", (expense_id, user_id))
            return c.rowcount


def get_expenses_sync(user_id: int, days: Optional[int] = None):
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as c:
            if days:
                date_from = datetime.now() - timedelta(days=days)
                c.execute(
                    "SELECT * FROM expenses WHERE user_id=%s AND date >= %s ORDER BY date DESC",
                    (user_id, date_from),
                )
            else:
                c.execute("SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC", (user_id,))
            return c.fetchall()


def get_expenses_between_sync(user_id: int, dt_from: datetime, dt_to: datetime):
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as c:
            c.execute(
                """
                SELECT * FROM expenses
                WHERE user_id=%s AND date >= %s AND date < %s
                ORDER BY date DESC
                """,
                (user_id, dt_from, dt_to),
            )
            return c.fetchall()


def get_last_expenses_sync(user_id: int, limit: int = 10):
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as c:
            c.execute(
                "SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC LIMIT %s",
                (user_id, limit),
            )
            return c.fetchall()


def get_categories_list_sync(user_id: int):
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT name FROM categories WHERE user_id=%s ORDER BY name", (user_id,))
            return [row[0] for row in c.fetchall()]


def clear_data_db_sync(user_id: int) -> int:
    with get_pool().connection() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM expenses WHERE user_id=%s", (user_id,))
            return c.rowcount


# ---------------- Parsing ----------------

def parse_expense_message(text: str):
    """Format: <category> <desc...> <amount> OR <category> <amount>."""
    parts = text.split()
    if len(parts) < 2:
        return None

    category = parts[0].lower()
    amount = None
    amount_index = -1

    for i in range(1, len(parts)):
        cleaned = parts[i].replace("₸", "").replace(",", ".")
        cleaned = cleaned.replace(" ", "")
        if AMOUNT_PATTERN.match(cleaned):
            amount = float(cleaned)
            amount_index = i
            break

    if amount is None:
        return None

    description = category if amount_index == 1 else " ".join(parts[1:amount_index])
    return category, description, amount


def parse_amount_only_message(text: str, default_desc: str) -> Optional[Tuple[str, float]]:
    """Format: <desc...> <amount> OR <amount>."""
    parts = text.split()
    if not parts:
        return None

    amount = None
    amount_index = -1
    for i, p in enumerate(parts):
        cleaned = p.replace("₸", "").replace(",", ".").replace(" ", "")
        if AMOUNT_PATTERN.match(cleaned):
            amount = float(cleaned)
            amount_index = i
            break

    if amount is None:
        return None

    desc = default_desc if amount_index <= 0 else (" ".join(parts[:amount_index]).strip() or default_desc)
    return desc, amount


def parse_number_only(text: str) -> Optional[float]:
    parts = text.split()
    if not parts:
        return None
    # take first numeric token
    for p in parts:
        cleaned = p.replace("₸", "").replace(",", ".").replace(" ", "")
        if AMOUNT_PATTERN.match(cleaned):
            return float(cleaned)
    return None


# ---------------- Panel (ONE message) ----------------

async def panel_show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """
    Single-panel UI:
    - in callback: edit the message that contains the pressed button
    - else: edit saved panel message id
    - else: create new
    """
    chat_id = update.effective_chat.id

    # cancel any scheduled auto-return when we explicitly show something new
    task = context.user_data.get(PANEL_RESET_TASK_KEY)
    if task:
        try:
            task.cancel()
        except Exception:
            pass
        context.user_data.pop(PANEL_RESET_TASK_KEY, None)

    # 1) callback -> edit same message
    if update.callback_query and update.callback_query.message:
        msg = update.callback_query.message
        try:
            await msg.edit_text(text=text, reply_markup=reply_markup)
            context.user_data[PANEL_KEY] = msg.message_id
            return
        except Exception:
            pass

    # 2) saved panel id
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

    # 3) create new
    sent = await update.effective_message.reply_text(text, reply_markup=reply_markup)
    context.user_data[PANEL_KEY] = sent.message_id


async def panel_edit_by_id(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int, text: str, reply_markup=None):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=reply_markup,
        )
    except Exception:
        pass


def schedule_return_to_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, delay_sec: float = 1.7):
    """Auto return to main menu after a short toast, without sending new messages."""
    msg_id = context.user_data.get(PANEL_KEY)
    if not msg_id:
        return

    async def job():
        await asyncio.sleep(delay_sec)
        await panel_edit_by_id(context, chat_id, msg_id, "🎯 Меню", reply_markup=kb_main())

    t = context.application.create_task(job())
    context.user_data[PANEL_RESET_TASK_KEY] = t


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


def kb_after_add():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Изменить сумму", callback_data="last:edit"),
                InlineKeyboardButton("↩️ Отменить", callback_data="last:undo"),
            ],
            [InlineKeyboardButton("➕ Ещё", callback_data="exp:new:0")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="m:main")],
        ]
    )


def kb_categories_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📌 Список", callback_data="cat:list")],
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
                InlineKeyboardButton("Всё", callback_data="r:all"),
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
                InlineKeyboardButton("Всё", callback_data="x:all"),
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


async def kb_pick_category(user_id: int, context: ContextTypes.DEFAULT_TYPE, page: int = 0, page_size: int = 10):
    cats = await run_db(get_categories_list_sync, user_id)
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
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="m:main")])
    return InlineKeyboardMarkup(buttons)


# ---------------- Report helpers ----------------

def period_to_days(key: str) -> Tuple[Optional[int], str]:
    if key == "today":
        return 1, "сегодня"
    if key == "week":
        return 7, "неделю"
    if key == "month":
        return 30, "месяц"
    return None, "всё время"


def pct_change(curr: float, prev: float) -> Optional[float]:
    if prev <= 0:
        return None
    return (curr - prev) / prev * 100.0


def aggregate_by_day(expenses: List[dict]) -> Dict[date, float]:
    out: Dict[date, float] = {}
    for e in expenses:
        dt = e.get("date")
        if not dt:
            continue
        d = dt.date()
        out[d] = out.get(d, 0.0) + float(e.get("amount") or 0)
    return out


async def render_report_text(user_id: int, period_key: str) -> str:
    days, period_name = period_to_days(period_key)

    # current
    if days is None:
        expenses = await run_db(get_expenses_sync, user_id, None)
    else:
        expenses = await run_db(get_expenses_sync, user_id, days)

    if not expenses:
        return f"📭 За {period_name} расходов нет."

    total = sum(float(e["amount"] or 0) for e in expenses)

    # previous period comparison (only for today/week/month)
    compare_line = ""
    if days is not None:
        now = datetime.now()
        curr_from = now - timedelta(days=days)
        prev_from = curr_from - timedelta(days=days)
        prev_to = curr_from
        prev_exp = await run_db(get_expenses_between_sync, user_id, prev_from, prev_to)
        prev_total = sum(float(e["amount"] or 0) for e in prev_exp)

        change = pct_change(total, prev_total)
        if change is None:
            compare_line = "➖ Нет данных для сравнения\n"
        else:
            arrow = "⬆️" if change > 0 else "⬇️" if change < 0 else "➖"
            compare_line = f"{arrow} {change:+.1f}% к прошлому периоду\n"

    # by category
    by_cat: Dict[str, float] = {}
    for e in expenses:
        cat = e["category"] or "без категории"
        by_cat[cat] = by_cat.get(cat, 0.0) + float(e["amount"] or 0)

    top_cats = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)[:8]
    top_exp = sorted(expenses, key=lambda x: float(x["amount"] or 0), reverse=True)[:5]

    # biggest category
    biggest_cat, biggest_amt = max(by_cat.items(), key=lambda x: x[1])

    # biggest day
    by_day = aggregate_by_day(expenses)
    biggest_day_line = ""
    if by_day:
        d, amt = max(by_day.items(), key=lambda x: x[1])
        biggest_day_line = f"📅 Самый дорогой день: {d.strftime('%d.%m')} — {amt:.0f} ₸\n"

    text = f"📈 Отчёт за {period_name}\n"
    text += f"💵 Итого: {total:.0f} ₸\n"
    if compare_line:
        text += compare_line
    text += f"🔥 Самая затратная категория: {biggest_cat} — {biggest_amt:.0f} ₸\n"
    if biggest_day_line:
        text += biggest_day_line

    text += "\n🏷 Топ категорий:\n"
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

    return text


async def render_last_text(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    expenses = await run_db(get_last_expenses_sync, user_id, 10)
    if not expenses:
        return "📭 Трат пока нет.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="m:main")]])

    text = "🧾 Последние 10 трат:\n\n"
    for e in expenses:
        exp_id = e["id"]
        amt = float(e["amount"] or 0)
        cat = e["category"] or "—"
        desc = e["description"] or ""
        dt = e["date"].strftime("%d.%m %H:%M") if e["date"] else ""
        text += f"#{exp_id} • {cat} / {desc} — {amt:.0f} ₸ ({dt})\n"

    return text, kb_last_expenses(expenses)


async def render_categories_text(user_id: int) -> str:
    cats = await run_db(get_categories_list_sync, user_id)
    if not cats:
        return "📭 Категорий пока нет."

    expenses = await run_db(get_expenses_sync, user_id, 30)
    stats: Dict[str, float] = {}
    for exp in expenses:
        stats[exp["category"]] = stats.get(exp["category"], 0.0) + float(exp["amount"] or 0)

    text = "📂 Категории (за 30 дней):\n\n"
    for cat in cats:
        text += f"• {cat}: {stats.get(cat, 0):.0f} ₸\n"
    return text


# ---------------- Export ----------------

async def send_export_csv_file(update: Update, context: ContextTypes.DEFAULT_TYPE, period_key: str):
    """CSV must be sent as file message (Telegram limitation)."""
    user_id = update.effective_user.id
    days, period_name = period_to_days(period_key)

    expenses = await run_db(get_expenses_sync, user_id, days if days is not None else None)
    if not expenses:
        await panel_show(update, context, f"📭 За {period_name} нечего экспортировать.", reply_markup=kb_main())
        return

    output = BytesIO()
    output.write("date,category,description,amount\n".encode("utf-8"))
    writer = csv.writer(output)
    for e in reversed(expenses):
        dt = e["date"].strftime("%Y-%m-%d %H:%M:%S") if e["date"] else ""
        writer.writerow([dt, e["category"], e["description"], float(e["amount"] or 0)])
    output.seek(0)

    filename = f"expenses_{period_key}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await update.effective_chat.send_document(
        document=InputFile(output, filename=filename),
        caption="📤 Экспорт расходов (CSV)",
    )


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_db(ensure_user_sync, update.effective_user)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("selected_category", None)
    context.user_data.pop("last_expense_id", None)

    text = (
        "🎯 Меню\n\n"
        "• Можно писать прямо текстом (чат очищается):\n"
        "  <категория> <название> <сумма>\n"
        "  Пример: еда дельпапа 12000\n\n"
        "• Или кнопкой ➕ (выбор категории)\n\n"
        "🧽 Все твои сообщения бот удаляет после обработки."
    )
    await panel_show(update, context, text, reply_markup=kb_main())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete ALL user messages after processing (success/error) to keep chat clean."""
    await run_db(ensure_user_sync, update.effective_user)

    user_msg = update.message
    text = (user_msg.text or "").strip()

    try:
        if not text:
            return

        awaiting = context.user_data.get("awaiting")
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # add category
        if awaiting == "add_category":
            category = text.lower().strip()
            if not category:
                await panel_show(update, context, "❌ Категория не может быть пустой.", reply_markup=kb_back_cancel("m:categories"))
                return
            await run_db(ensure_category_sync, user_id, category)
            context.user_data.pop("awaiting", None)
            await panel_show(update, context, f"✅ Категория добавлена: {category}", reply_markup=kb_categories_menu())
            schedule_return_to_menu(context, chat_id)
            return

        # del category
        if awaiting == "del_category":
            category = text.lower().strip()
            if not category:
                await panel_show(update, context, "❌ Категория не может быть пустой.", reply_markup=kb_back_cancel("m:categories"))
                return
            deleted = await run_db(delete_category_sync, user_id, category)
            context.user_data.pop("awaiting", None)
            msg = f"🗑️ Категория удалена: {category}" if deleted else f"⚠️ Категория не найдена: {category}"
            await panel_show(update, context, msg, reply_markup=kb_categories_menu())
            schedule_return_to_menu(context, chat_id)
            return

        # edit amount for last
        if awaiting == "edit_last_amount":
            exp_id = context.user_data.get("edit_expense_id")
            new_amount = parse_number_only(text)
            if not exp_id:
                context.user_data.pop("awaiting", None)
                context.user_data.pop("edit_expense_id", None)
                await panel_show(update, context, "⚠️ Не нашёл трату для изменения.", reply_markup=kb_main())
                return
            if new_amount is None:
                await panel_show(update, context, "❌ Напиши только новую сумму (пример: 6500)", reply_markup=kb_back_cancel("m:main"))
                return

            updated = await run_db(update_expense_amount_sync, user_id, int(exp_id), float(new_amount))
            context.user_data.pop("awaiting", None)
            context.user_data.pop("edit_expense_id", None)

            if updated:
                await panel_show(update, context, f"✅ Сумма обновлена: {new_amount:.0f} ₸", reply_markup=kb_main())
            else:
                await panel_show(update, context, "⚠️ Не получилось обновить (возможно, трата уже удалена).", reply_markup=kb_main())
            schedule_return_to_menu(context, chat_id)
            return

        # expense in chosen category (input: <desc> <amount> or <amount>)
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
            exp_id = await run_db(add_expense_sync, user_id, float(amount), category, description)

            context.user_data["last_expense_id"] = exp_id
            context.user_data.pop("awaiting", None)
            context.user_data.pop("selected_category", None)

            await panel_show(
                update,
                context,
                f"✅ +{float(amount):.0f} ₸ — {category} / {description}",
                reply_markup=kb_after_add(),
            )
            schedule_return_to_menu(context, chat_id)
            return

        # raw expense (input: <cat> <desc> <amount> or <cat> <amount>)
        parsed = parse_expense_message(text)
        if not parsed:
            await panel_show(
                update,
                context,
                "❌ Не понял формат.\nПиши: <категория> <название> <сумма>\nПример: еда дельпапа 12000",
                reply_markup=kb_main(),
            )
            schedule_return_to_menu(context, chat_id)
            return

        category, description, amount = parsed
        exp_id = await run_db(add_expense_sync, user_id, float(amount), category, description)
        context.user_data["last_expense_id"] = exp_id

        await panel_show(
            update,
            context,
            f"✅ +{float(amount):.0f} ₸ — {category} / {description}",
            reply_markup=kb_after_add(),
        )
        schedule_return_to_menu(context, chat_id)

    finally:
        try:
            await user_msg.delete()
        except Exception:
            pass


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await run_db(ensure_user_sync, update.effective_user)
    data = query.data or ""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if data == "noop":
        return

    if data == "do:cancel":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        context.user_data.pop("edit_expense_id", None)
        await panel_show(update, context, "Ок, отменил 👌", reply_markup=kb_main())
        schedule_return_to_menu(context, chat_id)
        return

    if data == "m:main":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        context.user_data.pop("edit_expense_id", None)
        await panel_show(update, context, "🎯 Меню", reply_markup=kb_main())
        return

    if data == "m:categories":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        context.user_data.pop("edit_expense_id", None)
        await panel_show(update, context, "📂 Категории", reply_markup=kb_categories_menu())
        return

    if data == "m:report":
        await panel_show(update, context, "📈 Отчёт: выбери период", reply_markup=kb_report_menu())
        return

    if data == "m:export":
        await panel_show(update, context, "📤 Экспорт CSV: выбери период", reply_markup=kb_export_menu())
        return

    if data == "m:last" or data == "m:last_refresh":
        text, markup = await render_last_text(user_id)
        await panel_show(update, context, text, reply_markup=markup)
        return

    if data == "do:clear":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        context.user_data.pop("edit_expense_id", None)
        deleted = await run_db(clear_data_db_sync, user_id)
        await panel_show(update, context, f"🗑️ Удалено {deleted} записей.", reply_markup=kb_main())
        schedule_return_to_menu(context, chat_id)
        return

    # last actions
    if data == "last:undo":
        exp_id = context.user_data.get("last_expense_id")
        if not exp_id:
            await panel_show(update, context, "⚠️ Нет последней траты для отмены.", reply_markup=kb_main())
            schedule_return_to_menu(context, chat_id)
            return
        deleted = await run_db(delete_expense_sync, user_id, int(exp_id))
        if deleted:
            context.user_data.pop("last_expense_id", None)
            await panel_show(update, context, "↩️ Последняя трата отменена.", reply_markup=kb_main())
        else:
            await panel_show(update, context, "⚠️ Не получилось отменить (возможно, уже удалено).", reply_markup=kb_main())
        schedule_return_to_menu(context, chat_id)
        return

    if data == "last:edit":
        exp_id = context.user_data.get("last_expense_id")
        if not exp_id:
            await panel_show(update, context, "⚠️ Нет последней траты для изменения.", reply_markup=kb_main())
            schedule_return_to_menu(context, chat_id)
            return
        context.user_data["awaiting"] = "edit_last_amount"
        context.user_data["edit_expense_id"] = int(exp_id)
        await panel_show(update, context, "✏️ Введи новую сумму (пример: 6500)", reply_markup=kb_back_cancel("m:main"))
        return

    # categories screens
    if data == "cat:list":
        text = await render_categories_text(user_id)
        await panel_show(update, context, text, reply_markup=kb_categories_menu())
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

    # add expense flow
    if data.startswith("exp:new:"):
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        page = int(data.split(":")[-1])
        markup = await kb_pick_category(user_id, context, page=page)
        await panel_show(update, context, "Выбери категорию 👇", reply_markup=markup)
        return

    if data.startswith("exp:cat:"):
        abs_idx = int(data.split(":")[-1])
        cats = context.user_data.get("cats_full") or (await run_db(get_categories_list_sync, user_id))
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

    # report
    if data.startswith("r:") or data.startswith("rr:"):
        period_key = data.split(":")[-1]
        text = await render_report_text(user_id, period_key)
        await panel_show(update, context, text, reply_markup=kb_report_result(period_key))
        return

    # export (must send file message)
    if data.startswith("x:") or data.startswith("rx:"):
        period_key = data.split(":")[-1]
        await send_export_csv_file(update, context, period_key)
        await panel_show(update, context, "✅ Экспорт отправлен файлом.", reply_markup=kb_main())
        schedule_return_to_menu(context, chat_id)
        return

    # delete in last list
    if data.startswith("e:del:"):
        exp_id = int(data.split(":")[-1])
        await run_db(delete_expense_sync, user_id, exp_id)
        text, markup = await render_last_text(user_id)
        await panel_show(update, context, text, reply_markup=markup)
        return


# ---------------- Main (manual polling) ----------------

def main():
    try:
        init_db_sync()
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        return

    token = os.getenv("BOT_TOKEN")
    if not token:
        print("❌ BOT_TOKEN не установлен!")
        return

    application = Application.builder().token(token).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Bot running (one-panel UX + edit amount + report compare + quiet confirmations)")

    async def runner():
        await application.initialize()
        await application.start()

        offset = None
        try:
            while True:
                updates = await application.bot.get_updates(
                    offset=offset,
                    timeout=5,
                    allowed_updates=Update.ALL_TYPES,
                )
                if not updates:
                    await asyncio.sleep(0.05)
                    continue

                for upd in updates:
                    offset = upd.update_id + 1
                    try:
                        await application.process_update(upd)
                    except Exception as e:
                        print("❌ update error:", e)

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
