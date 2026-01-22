import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

def get_db_connection():
    """Создать подключение к PostgreSQL"""
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        raise Exception("DATABASE_URL не установлен!")
    return psycopg2.connect(database_url)

def init_db():
    """Инициализация базы данных"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS expenses
                 (id SERIAL PRIMARY KEY,
                  user_id BIGINT,
                  amount REAL,
                  category TEXT,
                  description TEXT,
                  date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

def add_expense(user_id, amount, category, description):
    """Добавить расход"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO expenses (user_id, amount, category, description, date) VALUES (%s, %s, %s, %s, %s)",
              (user_id, amount, category, description, datetime.now()))
    conn.commit()
    conn.close()

def get_expenses(user_id, days=None):
    """Получить расходы за период"""
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    
    if days:
        date_from = datetime.now() - timedelta(days=days)
        c.execute("SELECT * FROM expenses WHERE user_id=%s AND date >= %s ORDER BY date DESC",
                  (user_id, date_from))
    else:
        c.execute("SELECT * FROM expenses WHERE user_id=%s ORDER BY date DESC", (user_id,))
    
    expenses = c.fetchall()
    conn.close()
    return expenses

def get_categories_list(user_id):
    """Получить список всех категорий пользователя"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT category FROM expenses WHERE user_id=%s ORDER BY category", (user_id,))
    categories = [row[0] for row in c.fetchall()]
    conn.close()
    return categories

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
/clear - очистить все данные
    """
    await update.message.reply_text(welcome_text)

async def handle_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщения с расходом"""
    try:
        text = update.message.text.strip()
        parts = text.split()
        
        if len(parts) < 2:
            await update.message.reply_text("❌ Слишком мало данных!\n\nФормат: <категория> <название> <сумма>\nПример: еда дельпапа 12000")
            return
        
        # Первое слово - всегда категория
        category = parts[0].lower()
        
        # Ищем число (сумму) в оставшихся частях
        amount = None
        amount_index = -1
        
        for i in range(1, len(parts)):
            try:
                amount = float(parts[i].replace(',', '.'))
                amount_index = i
                break
            except ValueError:
                continue
        
        if amount is None:
            await update.message.reply_text("❌ Не нашел сумму!\n\nУкажи число в сообщении.\nПример: еда дельпапа 12000")
            return
        
        # Описание - всё между категорией и суммой
        if amount_index == 1:
            # Если сумма сразу после категории: "такси 2000"
            description = category
        else:
            # Если есть название: "еда дельпапа 12000"
            description_parts = parts[1:amount_index]
            description = ' '.join(description_parts)
        
        # Сохраняем
        add_expense(update.effective_user.id, amount, category, description)
        
        await update.message.reply_text(
            f"✅ Записано!\n\n"
            f"📂 Категория: {category}\n"
            f"📝 Название: {description}\n"
            f"💰 Сумма: {amount} ₸"
        )
    
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}\n\nФормат: <категория> <название> <сумма>")

async def show_period(update: Update, context: ContextTypes.DEFAULT_TYPE, days, period_name):
    """Показать расходы за период"""
    expenses = get_expenses(update.effective_user.id, days=days)
    
    if not expenses:
        await update.message.reply_text(f"📭 За {period_name} расходов нет.")
        return
    
    # Группируем по категориям
    categories_data = {}
    total = 0
    
    for exp in expenses:
        category = exp['category']
        amount = exp['amount']
        description = exp['description']
        date = exp['date']
        
        if category not in categories_data:
            categories_data[category] = []
        
        categories_data[category].append({
            'amount': amount,
            'description': description,
            'date': date
        })
        total += amount
    
    # Формируем сообщение
    text = f"📊 Расходы за {period_name}:\n\n"
    
    # Сортируем категории по сумме
    sorted_categories = sorted(
        categories_data.items(),
        key=lambda x: sum(item['amount'] for item in x[1]),
        reverse=True
    )
    
    for category, items in sorted_categories:
        category_total = sum(item['amount'] for item in items)
        
        text += f"📂 {category.upper()}: {category_total} ₸\n"
        
        # Показываем позиции
        for item in items[:15]:
            date_str = item['date'].strftime('%m-%d')
            text += f"  • {item['description']}: {item['amount']} ₸ ({date_str})\n"
        
        if len(items) > 15:
            text += f"  ... и еще {len(items) - 15} позиций\n"
        
        text += "\n"
    
    text += f"💵 ИТОГО: {total} ₸"
    
    # Telegram ограничивает сообщения 4096 символами
    if len(text) > 4000:
        messages = []
        current_msg = f"📊 Расходы за {period_name}:\n\n"
        
        for category, items in sorted_categories:
            category_total = sum(item['amount'] for item in items)
            category_text = f"📂 {category.upper()}: {category_total} ₸\n"
            
            for item in items[:10]:
                date_str = item['date'].strftime('%m-%d')
                category_text += f"  • {item['description']}: {item['amount']} ₸ ({date_str})\n"
            
            if len(items) > 10:
                category_text += f"  ... и еще {len(items) - 10} позиций\n"
            
            category_text += "\n"
            
            if len(current_msg) + len(category_text) > 3900:
                messages.append(current_msg)
                current_msg = category_text
            else:
                current_msg += category_text
        
        if current_msg:
            current_msg += f"💵 ИТОГО: {total} ₸"
            messages.append(current_msg)
        
        for msg in messages:
            await update.message.reply_text(msg)
    else:
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
    """Показать список всех категорий пользователя"""
    categories = get_categories_list(update.effective_user.id)
    
    if not categories:
        await update.message.reply_text("📭 У тебя пока нет категорий.\n\nНачни добавлять расходы!")
        return
    
    # Получаем статистику по каждой категории
    expenses = get_expenses(update.effective_user.id, days=30)
    
    category_stats = {}
    for exp in expenses:
        category = exp['category']
        amount = exp['amount']
        
        if category not in category_stats:
            category_stats[category] = 0
        category_stats[category] += amount
    
    text = "📂 Твои категории (за месяц):\n\n"
    
    # Сортируем по сумме
    sorted_cats = sorted(category_stats.items(), key=lambda x: x[1], reverse=True)
    
    for category, total in sorted_cats:
        text += f"• {category}: {total} ₸\n"
    
    # Добавляем категории без трат за месяц
    for cat in categories:
        if cat not in category_stats:
            text += f"• {cat}: 0 ₸\n"
    
    await update.message.reply_text(text)

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистить все данные пользователя"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE user_id=%s", (update.effective_user.id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"🗑️ Удалено {deleted} записей.\n\nВсе твои данные очищены!")

def main():
    # Инициализация БД
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
    
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("month", month))
    application.add_handler(CommandHandler("all", all_expenses))
    application.add_handler(CommandHandler("categories", categories_list))
    application.add_handler(CommandHandler("clear", clear_data))
    
    # Обработка текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense))
    
    print("🤖 Бот запущен и подключен к PostgreSQL!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
