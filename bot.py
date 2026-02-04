import os
import re
import csv
import asyncio
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

import psycopg
from psycopg.rows import dict_row
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

AMOUNT_PATTERN = re.compile(r"^-?\d+(?:[.,]\d+)?$")


# ---------------- DB ----------------

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL не установлен!")
    return psycopg.connect(database_url)


def ensure_user(user):
    with get_db_connection() as conn:
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


def init_db():
    with get_db_connection() as conn:
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

            # миграция на всякий случай (если раньше была только expenses)
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


def ensure_category(user_id: int, category: str):
    category = category.strip().lower()
    if not category:
        return
    with get_db_connection() as conn:
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
    category = category.strip().lower()
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM categories WHERE user_id=%s AND name=%s",
                (user_id, category),
            )
            return c.rowcount


def add_expense(user_id: int, amount: float, category: str, description: str):
    category = category.strip().lower()
    ensure_category(user_id, category)
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                """
                INSERT INTO expenses (user_id, amount, category, description, date)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, amount, category, description, datetime.now()),
            )


def delete_expense(user_id: int, expense_id: int) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM expenses WHERE id=%s AND user_id=%s",
                (expense_id, user_id),
            )
            return c.rowcount


def get_expenses(user_id: int, days: Optional[int] = None):
    with get_db_connection() as conn:
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
    with get_db_connection() as conn:
        with conn.cursor(row_factory=dict_row) as c:
            c.execute(
                "SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC LIMIT %s",
                (user_id, limit),
            )
            return c.fetchall()


def get_categories_list(user_id: int):
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT name FROM categories WHERE user_id=%s ORDER BY name", (user_id,))
            categories = [row[0] for row in c.fetchall()]
            if categories:
                return categories
            c.execute(
                "SELECT DISTINCT category FROM expenses WHERE user_id=%s ORDER BY category",
                (user_id,),
            )
            return [row[0] for row in c.fetchall()]


# ---------------- Parsing ----------------

def parse_expense_message(text: str):
    """
    Формат: <категория> <название...> <сумма>
    Пример: еда дельпапа 12000
    Пример: такси 2000 -> description="такси"
    """
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

    if amount_index == 1:
        description = category
    else:
        description = " ".join(parts[1:amount_index])

    return category, description, amount


def parse_amount_only_message(text: str, default_desc: str) -> Optional[Tuple[str, float]]:
    """
    Для режима 'категория выбрана кнопкой':
    ожидаем: <название...> <сумма>  ИЛИ просто <сумма>
    Возвращает: (description, amount)
    """
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
        desc = " ".join(parts[:amount_index]).strip()
        if not desc:
            desc = default_desc

    return desc, amount


# ---------------- Inline Keyboards ----------------

def kb_main():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить трату", callback_data="exp:new:0")],
            [InlineKeyboardButton("📊 Траты (период)", callback_data="m:period")],
            [InlineKeyboardButton("📈 Отчёт", callback_data="m:report")],
            [InlineKeyboardButton("🧾 Последние (удалить)", callback_data="m:last")],
            [InlineKeyboardButton("📤 Экспорт CSV", callback_data="m:export")],
            [InlineKeyboardButton("📂 Категории", callback_data="m:categories")],
            [InlineKeyboardButton("🗑 Очистить все", callback_data="do:clear")],
        ]
    )


def kb_period():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Сегодня", callback_data="p:today"),
                InlineKeyboardButton("Неделя", callback_data="p:week"),
            ],
            [
                InlineKeyboardButton("Месяц", callback_data="p:month"),
                InlineKeyboardButton("Всё время", callback_data="p:all"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="m:main")],
        ]
    )


def kb_report():
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


def kb_export():
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


def kb_pick_category(user_id: int, context: ContextTypes.DEFAULT_TYPE, page: int = 0, page_size: int = 10):
    """
    Пагинация категорий.
    callback exp:new:<page> - открыть страницу
    callback exp:cat:<idx>  - выбрать категорию по абсолютному индексу
    """
    cats = get_categories_list(user_id)
    if not cats:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Создать категорию", callback_data="cat:add")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="m:main")],
            ]
        )

    # сохраняем список, чтобы по индексу доставать имя
    context.user_data["cats_full"] = cats

    total = len(cats)
    max_page = max(0, (total - 1) // page_size)
    page = max(0, min(page, max_page))

    start = page * page_size
    end = min(start + page_size, total)
    slice_cats = list(enumerate(cats[start:end], start=start))  # (absolute_index, name)

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
    # каждая строка: удалить
    buttons = []
    for exp in expenses:
        exp_id = exp["id"]
        desc = exp["description"] or ""
        cat = exp["category"] or ""
        amount = float(exp["amount"] or 0)
        label = f"🗑 {cat}: {desc} — {amount:.0f} ₸"
        # callback_data лимит 64, поэтому режем label только в тексте сообщения, а тут только id
        buttons.append([InlineKeyboardButton(f"🗑 Удалить #{exp_id}", callback_data=f"e:del:{exp_id}")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="m:main")])
    return InlineKeyboardMarkup(buttons)


# ---------------- Views ----------------

async def show_period(update: Update, context: ContextTypes.DEFAULT_TYPE, days, period_name):
    ensure_user(update.effective_user)
    expenses = get_expenses(update.effective_user.id, days=days)

    if not expenses:
        await update.effective_message.reply_text(f"📭 За {period_name} расходов нет.", reply_markup=kb_main())
        return

    categories_data = {}
    total = 0.0

    for exp in expenses:
        category = exp["category"]
        amount = float(exp["amount"] or 0)
        description = exp["description"]
        date = exp["date"]

        categories_data.setdefault(category, []).append(
            {"amount": amount, "description": description, "date": date}
        )
        total += amount

    text = f"📊 Расходы за {period_name}:\n\n"

    sorted_categories = sorted(
        categories_data.items(),
        key=lambda item: sum(x["amount"] for x in item[1]),
        reverse=True,
    )

    for category, items in sorted_categories:
        category_total = sum(x["amount"] for x in items)
        text += f"📌 {category} — {category_total:.0f} ₸\n"

        for e in items[:10]:
            exp_date = e["date"].strftime("%d.%m %H:%M") if e["date"] else ""
            text += f"  • {e['description']} — {e['amount']:.0f} ₸ ({exp_date})\n"

        if len(items) > 10:
            text += f"  … и ещё {len(items) - 10}\n"
        text += "\n"

    text += f"💵 ИТОГО: {total:.0f} ₸"

    await update.effective_message.reply_text(text, reply_markup=kb_period())


async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE, days, period_name):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    expenses = get_expenses(user_id, days=days)

    if not expenses:
        await update.effective_message.reply_text(f"📭 За {period_name} расходов нет.", reply_markup=kb_report())
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

    await update.effective_message.reply_text(text, reply_markup=kb_report())


async def send_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE, days, period_key: str):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    expenses = get_expenses(user_id, days=days)

    if not expenses:
        await update.effective_message.reply_text("📭 Нечего экспортировать.", reply_markup=kb_export())
        return

    output = BytesIO()
    output.write("date,category,description,amount\n".encode("utf-8"))

    writer = csv.writer(output)
    for e in reversed(expenses):  # в файле пусть будет от старых к новым
        dt = e["date"].strftime("%Y-%m-%d %H:%M:%S") if e["date"] else ""
        writer.writerow([dt, e["category"], e["description"], float(e["amount"] or 0)])

    output.seek(0)
    filename = f"expenses_{period_key}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    await update.effective_message.reply_document(
        document=InputFile(output, filename=filename),
        caption="📤 Экспорт расходов (CSV)",
        reply_markup=kb_export(),
    )


async def show_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    expenses = get_last_expenses(user_id, limit=10)

    if not expenses:
        await update.effective_message.reply_text("📭 Трат пока нет.", reply_markup=kb_main())
        return

    text = "🧾 Последние 10 трат:\n\n"
    for e in expenses:
        exp_id = e["id"]
        amt = float(e["amount"] or 0)
        cat = e["category"] or "—"
        desc = e["description"] or ""
        dt = e["date"].strftime("%d.%m %H:%M") if e["date"] else ""
        text += f"#{exp_id} • {cat} / {desc} — {amt:.0f} ₸ ({dt})\n"

    await update.effective_message.reply_text(text, reply_markup=kb_last_expenses(expenses))


async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user_id = update.effective_user.id
    categories = get_categories_list(user_id)

    if not categories:
        await update.effective_message.reply_text(
            "📭 У тебя пока нет категорий.\n\nНажми «➕ Добавить» и создай первую.",
            reply_markup=kb_categories_menu(),
        )
        return

    expenses = get_expenses(user_id, days=30)
    stats = {}
    for exp in expenses:
        stats[exp["category"]] = stats.get(exp["category"], 0) + float(exp["amount"] or 0)

    text = "📂 Категории (за 30 дней):\n\n"
    for cat in categories:
        text += f"• {cat}: {stats.get(cat, 0):.0f} ₸\n"

    await update.effective_message.reply_text(text, reply_markup=kb_categories_menu())


async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM expenses WHERE user_id=%s", (update.effective_user.id,))
            deleted = c.rowcount

    await update.effective_message.reply_text(f"🗑️ Удалено {deleted} записей.", reply_markup=kb_main())


# ---------------- Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("selected_category", None)

    text = (
        "🎯 Привет! Я бот для учета расходов.\n\n"
        "✅ Можно записывать трату текстом:\n"
        "<категория> <название> <сумма>\n"
        "Пример: еда дельпапа 12000\n\n"
        "Или нажми «➕ Добавить трату» и выбери категорию кнопкой 👇"
    )
    await update.effective_message.reply_text(text, reply_markup=kb_main())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Любой текст:
    - если ждём категорию (add/del) -> обработаем
    - если ждём сумму/описание по выбранной категории -> добавим расход
    - иначе -> пытаемся парсить как обычный расход: <категория> <название> <сумма>
    """
    ensure_user(update.effective_user)
    text = (update.message.text or "").strip()
    if not text:
        return

    awaiting = context.user_data.get("awaiting")

    # --- добавление категории ---
    if awaiting == "add_category":
        category = text.lower().strip()
        if not category:
            await update.effective_message.reply_text(
                "❌ Категория не может быть пустой.",
                reply_markup=kb_back_cancel("m:categories"),
            )
            return
        ensure_category(update.effective_user.id, category)
        context.user_data.pop("awaiting", None)
        await update.effective_message.reply_text(f"✅ Категория добавлена: {category}", reply_markup=kb_categories_menu())
        return

    # --- удаление категории ---
    if awaiting == "del_category":
        category = text.lower().strip()
        if not category:
            await update.effective_message.reply_text(
                "❌ Категория не может быть пустой.",
                reply_markup=kb_back_cancel("m:categories"),
            )
            return
        deleted = delete_category(update.effective_user.id, category)
        context.user_data.pop("awaiting", None)
        if deleted:
            await update.effective_message.reply_text(f"🗑️ Категория удалена: {category}", reply_markup=kb_categories_menu())
        else:
            await update.effective_message.reply_text(f"⚠️ Категория не найдена: {category}", reply_markup=kb_categories_menu())
        return

    # --- расход по выбранной категории кнопкой ---
    if awaiting == "expense_in_category":
        category = context.user_data.get("selected_category")
        if not category:
            context.user_data.pop("awaiting", None)
            await update.effective_message.reply_text("⚠️ Категория не выбрана. Открой меню заново.", reply_markup=kb_main())
            return

        parsed2 = parse_amount_only_message(text, default_desc=category)
        if not parsed2:
            await update.effective_message.reply_text(
                "❌ Не нашёл сумму.\n\nПиши так:\n<название> <сумма>\nПример: дельпапа 12000\n\nИли просто сумму: 2000",
                reply_markup=kb_back_cancel("exp:new:0"),
            )
            return

        description, amount = parsed2
        add_expense(update.effective_user.id, amount, category, description)

        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)

        await update.effective_message.reply_text(
            f"✅ Записано!\n\n📂 Категория: {category}\n📝 Название: {description}\n💰 Сумма: {amount:.0f} ₸",
            reply_markup=kb_main(),
        )
        return

    # --- обычный режим: category name amount ---
    parsed = parse_expense_message(text)
    if not parsed:
        await update.effective_message.reply_text(
            "❌ Не понял формат.\n\n"
            "1) Через кнопки: «➕ Добавить трату»\n"
            "2) Или текстом:\n<категория> <название> <сумма>\nПример: еда дельпапа 12000",
            reply_markup=kb_main(),
        )
        return

    category, description, amount = parsed
    add_expense(update.effective_user.id, amount, category, description)

    await update.effective_message.reply_text(
        f"✅ Записано!\n\n📂 Категория: {category}\n📝 Название: {description}\n💰 Сумма: {amount:.0f} ₸",
        reply_markup=kb_main(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ensure_user(update.effective_user)
    data = query.data or ""

    # noop for pager label
    if data == "noop":
        return

    # --- global cancel ---
    if data == "do:cancel":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text("Ок, отменил 👌", reply_markup=kb_main())
        return

    # --- меню ---
    if data == "m:main":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text("Главное меню 👇", reply_markup=kb_main())
        return

    if data == "m:period":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text("Выбери период 👇", reply_markup=kb_period())
        return

    if data == "m:report":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text("Выбери период для отчёта 👇", reply_markup=kb_report())
        return

    if data == "m:export":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text("Выбери период для экспорта 👇", reply_markup=kb_export())
        return

    if data == "m:last":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        return await show_last(update, context)

    if data == "m:categories":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text("Меню категорий 👇", reply_markup=kb_categories_menu())
        return

    # --- добавить трату (пагинация категорий) ---
    if data.startswith("exp:new:"):
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        page = int(data.split(":")[-1])
        await update.effective_message.reply_text(
            "Выбери категорию 👇",
            reply_markup=kb_pick_category(update.effective_user.id, context, page=page),
        )
        return

    if data.startswith("exp:cat:"):
        abs_idx = int(data.split(":")[-1])
        cats = context.user_data.get("cats_full") or get_categories_list(update.effective_user.id)
        if abs_idx < 0 or abs_idx >= len(cats):
            await update.effective_message.reply_text("⚠️ Категория не найдена. Открой выбор заново.", reply_markup=kb_main())
            return

        category = cats[abs_idx]
        context.user_data["selected_category"] = category
        context.user_data["awaiting"] = "expense_in_category"

        await update.effective_message.reply_text(
            f"📌 Категория выбрана: {category}\n\n"
            "Теперь напиши:\n<название> <сумма>\n"
            "Пример: дельпапа 12000\n\n"
            "Или просто сумму: 2000",
            reply_markup=kb_back_cancel("exp:new:0"),
        )
        return

    # --- периоды (обычный список) ---
    if data == "p:today":
        return await show_period(update, context, 1, "сегодня")
    if data == "p:week":
        return await show_period(update, context, 7, "неделю")
    if data == "p:month":
        return await show_period(update, context, 30, "месяц")
    if data == "p:all":
        return await show_period(update, context, None, "всё время")

    # --- отчёт ---
    if data == "r:today":
        return await show_report(update, context, 1, "сегодня")
    if data == "r:week":
        return await show_report(update, context, 7, "неделю")
    if data == "r:month":
        return await show_report(update, context, 30, "месяц")
    if data == "r:all":
        return await show_report(update, context, None, "всё время")

    # --- экспорт ---
    if data == "x:today":
        return await send_export_csv(update, context, 1, "today")
    if data == "x:week":
        return await send_export_csv(update, context, 7, "week")
    if data == "x:month":
        return await send_export_csv(update, context, 30, "month")
    if data == "x:all":
        return await send_export_csv(update, context, None, "all")

    # --- категории ---
    if data == "cat:list":
        return await categories_list(update, context)

    if data == "cat:add":
        context.user_data["awaiting"] = "add_category"
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text(
            "Напиши название категории (например: кафе)",
            reply_markup=kb_back_cancel("m:categories"),
        )
        return

    if data == "cat:del":
        context.user_data["awaiting"] = "del_category"
        context.user_data.pop("selected_category", None)
        await update.effective_message.reply_text(
            "Напиши категорию, которую удалить (например: кафе)",
            reply_markup=kb_back_cancel("m:categories"),
        )
        return

    # --- удаление трат ---
    if data.startswith("e:del:"):
        exp_id = int(data.split(":")[-1])
        deleted = delete_expense(update.effective_user.id, exp_id)
        if deleted:
            await update.effective_message.reply_text(f"🗑️ Удалено: #{exp_id}", reply_markup=kb_main())
        else:
            await update.effective_message.reply_text(f"⚠️ Не нашёл трату #{exp_id}", reply_markup=kb_main())
        return

    # --- очистка ---
    if data == "do:clear":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("selected_category", None)
        return await clear_data(update, context)


# ---------------- Main (manual polling) ----------------

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

    # manual polling (без Updater) — чтобы не падало на Python 3.13
    application = Application.builder().token(token).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Бот запущен (удаление/экспорт/пагинация/отчёт + manual polling)")

    async def runner():
        await application.initialize()
        await application.start()

        offset = None
        try:
            while True:
                updates = await application.bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=Update.ALL_TYPES,
                )
                for upd in updates:
                    offset = upd.update_id + 1
                    await application.process_update(upd)
        finally:
            await application.stop()
            await application.shutdown()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
