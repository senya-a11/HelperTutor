import os
import logging
import asyncio
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from pytz import timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ====================== НАСТРОЙКИ ======================
load_dotenv()

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TUTOR_ID = int(os.getenv('TUTOR_ID', 0))
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Moscow')
DATABASE_URL = os.getenv('DATABASE_URL')  # Render автоматически добавляет эту переменную

# Состояния для ConversationHandler
WAITING_HW_TEXT, WAITING_HW_DEADLINE, WAITING_SCHEDULE_TIME, WAITING_SCHEDULE_TOPIC = range(4)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Пул соединений PostgreSQL
db_pool = None
# Thread pool для асинхронных операций с БД
thread_pool = ThreadPoolExecutor(max_workers=10)


# ====================== БАЗА ДАННЫХ POSTGRESQL ======================
def init_db():
    """Инициализация базы данных PostgreSQL"""
    global db_pool

    try:
        # Создаем пул соединений для psycopg2
        db_pool = pool.SimpleConnectionPool(
            1, 20,  # min, max connections
            DATABASE_URL,
            sslmode='require'  # Для Render.com обязательно
        )
        logger.info(f"Пул соединений PostgreSQL создан для {DATABASE_URL[:30]}...")

        # Создаем таблицы если их нет
        create_tables()

    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")
        # Даем больше информации об ошибке
        logger.error(f"DATABASE_URL: {DATABASE_URL[:50]}..." if DATABASE_URL else "DATABASE_URL не установлен")
        raise


def create_tables():
    """Создать таблицы в PostgreSQL"""
    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username VARCHAR(100),
                full_name VARCHAR(200) NOT NULL,
                role VARCHAR(20) CHECK(role IN ('tutor', 'student')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблица домашних заданий
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS homeworks (
                id SERIAL PRIMARY KEY,
                student_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                tutor_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                task_text TEXT NOT NULL,
                deadline TIMESTAMP NOT NULL,
                is_completed BOOLEAN DEFAULT FALSE,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Таблица расписания
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule (
                id SERIAL PRIMARY KEY,
                student_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                tutor_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                lesson_time TIMESTAMP NOT NULL,
                topic TEXT,
                notify_student BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        logger.info("Таблицы PostgreSQL созданы/проверены")

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка создания таблиц: {e}")
        raise
    finally:
        cursor.close()
        return_connection(conn)


def get_connection():
    """Получить соединение из пула"""
    return db_pool.getconn()


def return_connection(conn):
    """Вернуть соединение в пул"""
    db_pool.putconn(conn)


# ====================== АСИНХРОННЫЕ ОПЕРАЦИИ С БАЗОЙ ======================
async def db_execute(query: str, params: tuple = ()):
    """Выполнить SQL запрос (INSERT/UPDATE/DELETE)"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, _db_execute_sync, query, params)


def _db_execute_sync(query: str, params: tuple = ()):
    """Синхронное выполнение SQL запроса"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        return cursor.rowcount
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка выполнения запроса: {e}")
        raise
    finally:
        cursor.close()
        return_connection(conn)


async def db_fetchall(query: str, params: tuple = ()):
    """Выполнить запрос и вернуть все результаты"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, _db_fetchall_sync, query, params)


def _db_fetchall_sync(query: str, params: tuple = ()):
    """Синхронное выполнение запроса с возвратом всех результатов"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"Ошибка запроса fetchall: {e}")
        return []
    finally:
        cursor.close()
        return_connection(conn)


async def db_fetchone(query: str, params: tuple = ()):
    """Выполнить запрос и вернуть одну строку"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, _db_fetchone_sync, query, params)


def _db_fetchone_sync(query: str, params: tuple = ()):
    """Синхронное выполнение запроса с возвратом одной строки"""
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute(query, params)
        return cursor.fetchone()
    except Exception as e:
        logger.error(f"Ошибка запроса fetchone: {e}")
        return None
    finally:
        cursor.close()
        return_connection(conn)


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
async def get_user(telegram_id: int):
    """Получить пользователя по ID Telegram"""
    return await db_fetchone(
        'SELECT * FROM users WHERE telegram_id = %s',
        (telegram_id,)
    )


async def register_user(telegram_id: int, username: str, full_name: str, role: str = 'student'):
    """Зарегистрировать нового пользователя"""
    user = await get_user(telegram_id)
    if not user:
        await db_execute(
            '''INSERT INTO users (telegram_id, username, full_name, role) 
               VALUES (%s, %s, %s, %s)''',
            (telegram_id, username, full_name, role)
        )
        return True
    return False


async def is_tutor(telegram_id: int) -> bool:
    """Проверить, является ли пользователь репетитором"""
    user = await get_user(telegram_id)
    if user:
        return user['role'] == 'tutor'
    return telegram_id == TUTOR_ID


# ====================== КОМАНДЫ БОТА ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user

    # Регистрация пользователя
    if await is_tutor(user.id):
        role = 'tutor'
        await register_user(user.id, user.username, user.full_name, role)
        await update.message.reply_text(
            f"👨‍🏫 Добро пожаловать, репетитор {user.full_name}!\n\n"
            f"Используйте команду /menu для управления",
            reply_markup=ReplyKeyboardRemove()
        )
        await show_tutor_menu(update, context)
    else:
        role = 'student'
        await register_user(user.id, user.username, user.full_name, role)
        await update.message.reply_text(
            f"👨‍🎓 Привет, {user.full_name}!\n\n"
            f"Я помогу вам следить за домашними заданиями и расписанием.",
            reply_markup=get_student_keyboard()
        )


async def show_tutor_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню репетитора"""
    keyboard = [
        [InlineKeyboardButton("📝 Добавить ДЗ", callback_data='add_hw')],
        [InlineKeyboardButton("📋 Список ДЗ", callback_data='list_hw')],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "📊 Панель управления:",
            reply_markup=reply_markup
        )
    elif update.message:
        await update.message.reply_text(
            "📊 Панель управления:",
            reply_markup=reply_markup
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline кнопок"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == 'menu':
        await show_tutor_menu(update, context)

    elif data == 'add_hw':
        if not await is_tutor(user_id):
            await query.edit_message_text("Доступно только репетитору!")
            return

        await query.edit_message_text("Функция добавления ДЗ в разработке...")

    elif data == 'list_hw':
        hws = await db_fetchall('''
            SELECT h.task_text, h.deadline, h.is_completed, u.full_name
            FROM homeworks h
            JOIN users u ON h.student_id = u.id
            WHERE h.deadline > CURRENT_TIMESTAMP
            ORDER BY h.deadline
            LIMIT 10
        ''')

        if not hws:
            text = "📭 Нет активных домашних заданий."
        else:
            text = "📚 Активные домашние задания:\n\n"
            for hw in hws:
                status = "✅ Выполнено" if hw['is_completed'] else "⏳ В процессе"
                deadline = hw['deadline'].strftime('%d.%m.%Y %H:%M') if hw['deadline'] else "Не указан"
                text += f"👤 {hw['full_name']}\n📝 {hw['task_text'][:50]}...\n📅 Дедлайн: {deadline}\n{status}\n\n"

        await query.edit_message_text(text)

    elif data == 'hw_done':
        student = await get_user(user_id)
        if not student:
            await query.edit_message_text("Сначала напишите /start")
            return

        await query.edit_message_text(
            "✅ Вы отметили ДЗ как выполненное! Репетитор получит уведомление.",
            reply_markup=get_student_keyboard()
        )


def get_student_keyboard():
    """Клавиатура для ученика"""
    keyboard = [
        [InlineKeyboardButton("✅ ДЗ выполнено", callback_data='hw_done')],
        [InlineKeyboardButton("📚 Мои ДЗ", callback_data='my_homework')],
    ]
    return InlineKeyboardMarkup(keyboard)


async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик для неизвестных текстовых сообщений"""
    if update.message:
        user_id = update.effective_user.id

        if await is_tutor(user_id):
            await update.message.reply_text(
                "Используйте /menu для доступа к панели управления."
            )
        else:
            await update.message.reply_text(
                "Используйте кнопки ниже:",
                reply_markup=get_student_keyboard()
            )


# ====================== ЗАПУСК БОТА ======================
async def main():
    """Запуск бота"""
    # Проверяем обязательные переменные
    if not TOKEN:
        logger.error("❌ Токен бота не найден! Укажите TELEGRAM_BOT_TOKEN")
        return

    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL не найден!")
        logger.error("На Render.com убедитесь что вы:")
        logger.error("1. Создали PostgreSQL базу данных")
        logger.error("2. Добавили DATABASE_URL в Environment Variables")
        return

    if not TUTOR_ID:
        logger.warning("⚠️ TUTOR_ID не установлен. Некоторые функции могут не работать.")

    logger.info(f"✅ TOKEN: {'установлен' if TOKEN else 'не установлен'}")
    logger.info(f"✅ DATABASE_URL: {'установлен' if DATABASE_URL else 'не установлен'}")
    logger.info(f"✅ TUTOR_ID: {TUTOR_ID if TUTOR_ID else 'не установлен'}")

    try:
        # Инициализация БД
        init_db()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        return

    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    logger.info("✅ Telegram приложение создано")

    # Простые обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", show_tutor_menu))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_message))

    logger.info("✅ Бот запускается...")

    try:
        await application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске бота: {e}")


if __name__ == '__main__':
    asyncio.run(main())