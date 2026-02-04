import os
import re
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes


AMOUNT_PATTERN = re.compile(r"^-?\d+(?:[.,]\d+)?$")


def get_db_connection():
    """Создать подключение к PostgreSQL"""
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise RuntimeError("DATABASE_URL не установлен!")
    return psycopg.connect(database_url)


def ensure_user(user):
    """Создать пользователя, если он еще не существует."""
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
    """Инициализация базы данных"""
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


def ensure_category(user_id, category):
    """Создать категорию, если ее еще нет."""
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


def add_expense(user_id, amount, category, description):
    """Добавить расход"""
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


def get_expenses(user_id, days=None):
    """Получить расходы за период"""
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


def get_categories_list(user_id):
    """Получить список всех категорий пользователя"""
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT name FROM categories WHERE user_id=%s ORDER BY name",
                (user_id,),
            )
            categories = [row[0] for row in c.fetchall()]
            if categories:
                return categories
            c.execute(
                "SELECT DISTINCT category FROM expenses WHERE user_id=%s ORDER BY category",
                (user_id,),
            )
            return [row[0] for row in c.fetchall()]


def delete_category(user_id, category):
    """Удалить категорию из списка пользователя."""
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM categories WHERE user_id=%s AND name=%s",
                (user_id, category),
            )
            return c.rowcount


def parse_expense_message(text):
    """Разобрать сообщение с расходом."""
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
        description = ' '.join(parts[1:amount_index])

    return category, description, amount


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    welcome_text = """
🎯 Привет! Я помогу тебе вести учет расходов.

💡 Формат записи:
<категория> <название> <сумма>

Примеры:
• еда дельпапа 12000
• такси 2000
• кафе кфс 4500
• продукты магнум 8000
• сигареты 1500

Если название не нужно:
• такси 2000 (запишется как "такси")

📊 Команды:
/today - траты за сегодня
/week - траты за неделю
/month - траты за месяц
/all - все траты
/categories - мои категории
/addcategory - добавить категорию
/delcategory - удалить категорию
/clear - очистить все данные
    """
    await update.message.reply_text(welcome_text)


async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщения с расходом"""
    try:
        ensure_user(update.effective_user)
        text = update.message.text.strip()
        parsed = parse_expense_message(text)

        if not parsed:
            await update.message.reply_text(
                "❌ Слишком мало данных!\n\nФормат: <категория> <название> <сумма>\nПример: еда дельпапа 12000"
            )
            return

        category, description, amount = parsed

        add_expense(update.effective_user.id, amount, category, description)

        await update.message.reply_text(
            f"✅ Записано!\n\n"
            f"📂 Категория: {category}\n"
            f"📝 Название: {description}\n"
            f"💰 Сумма: {amount} ₸"
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка: {str(e)}\n\nФормат: <категория> <название> <сумма>"
        )


async def show_period(update: Update, context: ContextTypes.DEFAULT_TYPE, days, period_name):
    ensure_user(update.effective_user)
    expenses = get_expenses(update.effective_user.id, days=days)

    if not expenses:
        await update.message.reply_text(f"📭 За {period_name} расходов нет.")
        return

    categories_data = {}
    total = 0

    for exp in expenses:
        category = exp["category"]
        amount = exp["amount"]
        description = exp["description"]
        date = exp["date"]

        categories_data.setdefault(category, []).append(
            {"amount": amount, "description": description, "date": date}
        )
        total += amount

    text = f"📊 Расходы за {period_name}:\n\n"

    # сортируем категории по сумме расходов (убывание)
    sorted_categories = sorted(
        categories_data.items(),
        key=lambda item: sum(x["amount"] for x in item[1]),
        reverse=True,
    )

    for category, items in sorted_categories:
        category_total = sum(x["amount"] for x in items)
        text += f"📌 {category} — {category_total} ₸\n"

        # показываем до 10 последних трат в категории
        for exp in items[:10]:
            exp_date = exp["date"].strftime("%d.%m %H:%M") if exp["date"] else ""
            text += f"  • {exp['description']} — {exp['amount']} ₸ ({exp_date})\n"

        if len(items) > 10:
            text += f"  … и ещё {len(items) - 10}\n"

        text += "\n"

    text += f"💵 ИТОГО: {total} ₸"
    await update.message.reply_text(text)



async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_period(update, context, 1, "сегодня")


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_period(update, context, 7, "неделю")


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_period(update, context, 30, "месяц")


async def all_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_period(update, context, None, "всё время")


async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    categories = get_categories_list(update.effective_user.id)

    if not categories:
        await update.message.reply_text("📭 У тебя пока нет категорий.\n\nНачни добавлять расходы!")
        return

    expenses = get_expenses(update.effective_user.id, days=30)

    category_stats = {}
    for exp in expenses:
        category_stats[exp['category']] = category_stats.get(exp['category'], 0) + exp['amount']

    text = "📂 Твои категории (за месяц):\n\n"

    for category, total in sorted(category_stats.items(), key=lambda x: x[1], reverse=True):
        text += f"• {category}: {total} ₸\n"

    for cat in categories:
        if cat not in category_stats:
            text += f"• {cat}: 0 ₸\n"

    await update.message.reply_text(text)


async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    with get_db_connection() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM expenses WHERE user_id=%s", (update.effective_user.id,))
            deleted = c.rowcount

    await update.message.reply_text(f"🗑️ Удалено {deleted} записей.\n\nВсе твои данные очищены!")


async def add_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("❌ Укажи название категории.\n\nПример: /addcategory кафе")
        return

    category = ' '.join(context.args).strip().lower()
    if not category:
        await update.message.reply_text("❌ Категория не может быть пустой.")
        return

    ensure_category(update.effective_user.id, category)
    await update.message.reply_text(f"✅ Категория добавлена: {category}")


async def del_category_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("❌ Укажи название категории.\n\nПример: /delcategory кафе")
        return

    category = ' '.join(context.args).strip().lower()
    if not category:
        await update.message.reply_text("❌ Категория не может быть пустой.")
        return

    deleted = delete_category(update.effective_user.id, category)
    if deleted:
        await update.message.reply_text(f"🗑️ Категория удалена: {category}")
    else:
        await update.message.reply_text(f"⚠️ Категория не найдена: {category}")


def main():
    try:
        init_db()
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        return

    token = os.getenv('BOT_TOKEN')
    if not token:
        print("❌ Ошибка: BOT_TOKEN не установлен!")
        return

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("month", month))
    application.add_handler(CommandHandler("all", all_expenses))
    application.add_handler(CommandHandler("categories", categories_list))
    application.add_handler(CommandHandler("addcategory", add_category_command))
    application.add_handler(CommandHandler("delcategory", del_category_command))
    application.add_handler(CommandHandler("clear", clear_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))

    print("🤖 Бот запущен и подключен к PostgreSQL!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
