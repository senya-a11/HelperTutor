import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional
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

# PostgreSQL connection string для Render.com
DATABASE_URL = os.getenv('DATABASE_URL')  # Render предоставляет эту переменную

# Состояния для ConversationHandler
WAITING_HW_TEXT, WAITING_HW_DEADLINE, WAITING_SCHEDULE_TIME, WAITING_SCHEDULE_TOPIC = range(4)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Пул соединений PostgreSQL
connection_pool = None


# ====================== БАЗА ДАННЫХ POSTGRESQL ======================
def init_db():
    """Инициализация базы данных PostgreSQL"""
    global connection_pool

    try:
        # Создаем пул соединений
        connection_pool = psycopg2.pool.SimpleConnectionPool(
            1, 20,  # min, max connections
            DATABASE_URL,
            sslmode='require'  # Для Render.com обязательно
        )
        logger.info("Пул соединений PostgreSQL создан")

        # Создаем таблицы если их нет
        create_tables()

    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")
        raise


def get_connection():
    """Получить соединение из пула"""
    return connection_pool.getconn()


def return_connection(conn):
    """Вернуть соединение в пул"""
    connection_pool.putconn(conn)


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                timezone VARCHAR(50) DEFAULT 'Europe/Moscow'
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
                reminder_sent_24h BOOLEAN DEFAULT FALSE,
                reminder_sent_1h BOOLEAN DEFAULT FALSE,
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
                duration_minutes INTEGER DEFAULT 60,
                notify_student BOOLEAN DEFAULT TRUE,
                reminder_sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Индексы для производительности
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_homeworks_deadline ON homeworks(deadline)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_homeworks_student_id ON homeworks(student_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_schedule_lesson_time ON schedule(lesson_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_schedule_student_id ON schedule(student_id)')

        conn.commit()
        logger.info("Таблицы PostgreSQL созданы/проверены")

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка создания таблиц: {e}")
        raise
    finally:
        cursor.close()
        return_connection(conn)


async def db_execute(query: str, params: tuple = ()):
    """Выполнить SQL запрос (INSERT/UPDATE/DELETE)"""
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
        logger.info(f"Зарегистрирован новый пользователь: {full_name} ({role})")
        return True
    return False


async def is_tutor(telegram_id: int) -> bool:
    """Проверить, является ли пользователь репетитором"""
    user = await get_user(telegram_id)
    if user:
        return user['role'] == 'tutor'
    return telegram_id == TUTOR_ID


def format_datetime(dt: datetime) -> str:
    """Форматирование даты-времени для отображения"""
    return dt.strftime('%d.%m.%Y %H:%M')


# ====================== КОМАНДЫ БОТА ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Регистрация пользователя
    if await is_tutor(user.id):
        role = 'tutor'
        await register_user(user.id, user.username, user.full_name, role)
        await update.message.reply_text(
            f"👨‍🏫 Добро пожаловать, репетитор {user.full_name}!\n\n"
            f"Ваш ID: {user.id}\n"
            f"Используйте команду /menu для управления",
            reply_markup=ReplyKeyboardRemove()
        )
        await show_tutor_menu(update, context)
    else:
        role = 'student'
        await register_user(user.id, user.username, user.full_name, role)
        await update.message.reply_text(
            f"👨‍🎓 Привет, {user.full_name}!\n\n"
            f"Я помогу вам следить за домашними заданиями и расписанием.\n"
            f"Используйте кнопки ниже:",
            reply_markup=get_student_keyboard()
        )


async def show_tutor_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню репетитора"""
    user_id = update.effective_user.id
    if not await is_tutor(user_id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    keyboard = [
        [InlineKeyboardButton("📝 Добавить ДЗ", callback_data='add_hw')],
        [InlineKeyboardButton("📋 Список ДЗ", callback_data='list_hw')],
        [InlineKeyboardButton("📅 Добавить занятие", callback_data='add_lesson')],
        [InlineKeyboardButton("🗓 Расписание занятий", callback_data='list_lessons')],
        [InlineKeyboardButton("👥 Список учеников", callback_data='list_students')],
        [InlineKeyboardButton("🔄 Обновить напоминания", callback_data='refresh_reminders')],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "📊 Панель управления репетитора:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            "📊 Панель управления репетитора:",
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

        # Получаем список учеников
        students = await db_fetchall(
            'SELECT telegram_id, full_name FROM users WHERE role = %s ORDER BY full_name',
            ('student',)
        )
        if not students:
            await query.edit_message_text("Нет зарегистрированных учеников!")
            return

        keyboard = []
        for student in students:
            keyboard.append([
                InlineKeyboardButton(
                    student['full_name'],
                    callback_data=f'select_student_hw:{student["telegram_id"]}'
                )
            ])
        keyboard.append([InlineKeyboardButton("Назад", callback_data='menu')])

        await query.edit_message_text(
            "👥 Выберите ученика для ДЗ:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith('select_student_hw:'):
        student_id = int(data.split(':')[1])
        context.user_data['selected_student'] = student_id
        await query.edit_message_text(
            "✏️ Введите текст домашнего задания:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data='menu')]])
        )
        return WAITING_HW_TEXT

    elif data == 'list_hw':
        hws = await db_fetchall('''
            SELECT h.task_text, h.deadline, h.is_completed, u.full_name, h.student_id
            FROM homeworks h
            JOIN users u ON h.student_id = u.id
            WHERE h.deadline > CURRENT_TIMESTAMP
            ORDER BY h.deadline
            LIMIT 20
        ''')

        if not hws:
            text = "📭 Нет активных домашних заданий."
        else:
            text = "📚 Последние 20 активных ДЗ:\n\n"
            for hw in hws:
                status = "✅ Выполнено" if hw['is_completed'] else "⏳ В процессе"
                deadline = hw['deadline'].strftime('%d.%m.%Y %H:%M') if hw['deadline'] else "Не указан"
                text += f"👤 {hw['full_name']}\n📝 {hw['task_text'][:50]}...\n📅 Дедлайн: {deadline}\n{status}\n\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='menu')]])
        )

    elif data == 'add_lesson':
        if not await is_tutor(user_id):
            await query.edit_message_text("Доступно только репетитору!")
            return

        students = await db_fetchall(
            'SELECT telegram_id, full_name FROM users WHERE role = %s ORDER BY full_name',
            ('student',)
        )
        if not students:
            await query.edit_message_text("Нет зарегистрированных учеников!")
            return

        keyboard = []
        for student in students:
            keyboard.append([
                InlineKeyboardButton(
                    student['full_name'],
                    callback_data=f'select_student_lesson:{student["telegram_id"]}'
                )
            ])
        keyboard.append([InlineKeyboardButton("Назад", callback_data='menu')])

        await query.edit_message_text(
            "👥 Выберите ученика для занятия:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith('select_student_lesson:'):
        student_id = int(data.split(':')[1])
        context.user_data['selected_student'] = student_id
        await query.edit_message_text(
            "🕐 Введите дату и время занятия (в формате ДД.ММ.ГГГГ ЧЧ:ММ):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data='menu')]])
        )
        return WAITING_SCHEDULE_TIME

    elif data == 'list_lessons':
        lessons = await db_fetchall('''
            SELECT s.lesson_time, s.topic, u.full_name, s.notify_student, s.duration_minutes
            FROM schedule s
            JOIN users u ON s.student_id = u.id
            WHERE s.lesson_time > CURRENT_TIMESTAMP
            ORDER BY s.lesson_time
            LIMIT 20
        ''')

        if not lessons:
            text = "📭 Нет запланированных занятий."
        else:
            text = "🗓 Ближайшие 20 занятий:\n\n"
            for lesson in lessons:
                notify = "🔔" if lesson['notify_student'] else "🔕"
                topic = lesson['topic'] if lesson['topic'] else "Без темы"
                lesson_time = lesson['lesson_time'].strftime('%d.%m.%Y %H:%M')
                duration = f"{lesson['duration_minutes']} мин" if lesson['duration_minutes'] else "60 мин"
                text += f"👤 {lesson['full_name']}\n📅 {lesson_time} ({duration})\n📌 {topic}\n{notify}\n\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='menu')]])
        )

    elif data == 'list_students':
        students = await db_fetchall(
            'SELECT full_name, username, created_at FROM users WHERE role = %s ORDER BY created_at DESC',
            ('student',)
        )

        if not students:
            text = "👥 Нет зарегистрированных учеников."
        else:
            text = f"👥 Список учеников ({len(students)}):\n\n"
            for student in students:
                username = f"(@{student['username']})" if student['username'] else ""
                created = student['created_at'].strftime('%d.%m.%Y')
                text += f"• {student['full_name']} {username} - с {created}\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='menu')]])
        )

    elif data == 'hw_done':
        # Ученик нажал "ДЗ выполнено"
        student = await get_user(user_id)
        if not student:
            await query.edit_message_text("Сначала зарегистрируйтесь через /start")
            return

        # Находим активные ДЗ для этого ученика
        active_hw = await db_fetchone('''
            SELECT id, task_text FROM homeworks 
            WHERE student_id = %s AND is_completed = FALSE AND deadline > CURRENT_TIMESTAMP
            ORDER BY deadline LIMIT 1
        ''', (student['id'],))

        if not active_hw:
            await query.edit_message_text("У вас нет активных домашних заданий!")
            return

        hw_id = active_hw['id']
        task_text = active_hw['task_text']

        # Помечаем как выполненное
        await db_execute(
            '''UPDATE homeworks SET is_completed = TRUE, completed_at = CURRENT_TIMESTAMP 
               WHERE id = %s''',
            (hw_id,)
        )

        # Отправляем уведомление репетитору
        try:
            await context.bot.send_message(
                chat_id=TUTOR_ID,
                text=f"🎉 Ученик {student['full_name']} выполнил ДЗ!\n\n"
                     f"📝 Задание: {task_text[:100]}...\n"
                     f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление репетитору: {e}")

        await query.edit_message_text(
            f"✅ Отлично! Я сообщил репетитору, что вы выполнили задание:\n\n📝 {task_text[:200]}",
            reply_markup=get_student_keyboard()
        )

    elif data == 'my_homework':
        student = await get_user(user_id)
        if not student:
            await query.edit_message_text("Сначала зарегистрируйтесь через /start")
            return

        hws = await db_fetchall('''
            SELECT task_text, deadline, is_completed 
            FROM homeworks 
            WHERE student_id = %s AND deadline > CURRENT_TIMESTAMP
            ORDER BY deadline
            LIMIT 10
        ''', (student['id'],))

        if not hws:
            text = "📭 У вас нет активных домашних заданий."
        else:
            text = "📚 Ваши домашние задания:\n\n"
            for hw in hws:
                status = "✅ Выполнено" if hw['is_completed'] else "⏳ В процессе"
                deadline = hw['deadline'].strftime('%d.%m.%Y %H:%M')
                text += f"📝 {hw['task_text'][:100]}...\n📅 Дедлайн: {deadline}\n{status}\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_student_keyboard()
        )

    elif data == 'my_schedule':
        student = await get_user(user_id)
        if not student:
            await query.edit_message_text("Сначала зарегистрируйтесь через /start")
            return

        lessons = await db_fetchall('''
            SELECT lesson_time, topic, duration_minutes
            FROM schedule 
            WHERE student_id = %s AND lesson_time > CURRENT_TIMESTAMP
            ORDER BY lesson_time
            LIMIT 10
        ''', (student['id'],))

        if not lessons:
            text = "🗓 У вас нет запланированных занятий."
        else:
            text = "🗓 Ваше расписание:\n\n"
            for lesson in lessons:
                topic = lesson['topic'] if lesson['topic'] else "Без темы"
                lesson_time = lesson['lesson_time'].strftime('%d.%m.%Y %H:%M')
                duration = f"{lesson['duration_minutes']} мин" if lesson['duration_minutes'] else "60 мин"
                text += f"📅 {lesson_time} ({duration})\n📌 {topic}\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_student_keyboard()
        )

    elif data == 'refresh_reminders':
        if not await is_tutor(user_id):
            await query.edit_message_text("Доступно только репетитору!")
            return

        # Перезапускаем планировщик
        await restart_scheduler()
        await query.edit_message_text(
            "🔄 Напоминания перезапущены!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='menu')]])
        )


async def add_hw_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить текст ДЗ от репетитора"""
    if update.message:
        context.user_data['hw_text'] = update.message.text
        await update.message.reply_text(
            "📅 Теперь введите дедлайн (в формате ДД.ММ.ГГГГ ЧЧ:ММ):",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_HW_DEADLINE
    return ConversationHandler.END


async def add_hw_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить дедлайн ДЗ и сохранить"""
    if update.message:
        try:
            deadline_str = update.message.text
            deadline = datetime.strptime(deadline_str, '%d.%m.%Y %H:%M')

            student_id = context.user_data.get('selected_student')
            hw_text = context.user_data.get('hw_text')

            # Находим ID ученика в нашей БД
            student = await get_user(student_id)
            tutor = await get_user(update.effective_user.id)

            if not student or not tutor:
                await update.message.reply_text("Ошибка: пользователь не найден!")
                return ConversationHandler.END

            # Сохраняем ДЗ в БД
            await db_execute(
                '''INSERT INTO homeworks (student_id, tutor_id, task_text, deadline) 
                   VALUES (%s, %s, %s, %s)''',
                (student['id'], tutor['id'], hw_text, deadline)
            )

            # Планируем напоминания
            await schedule_hw_reminders(student_id, deadline, hw_text, student['full_name'])

            # Отправляем уведомление ученику
            try:
                await context.bot.send_message(
                    chat_id=student_id,
                    text=f"📚 Новое домашнее задание!\n\n📝 {hw_text}\n📅 Дедлайн: {deadline_str}\n\n"
                         f"Нажмите '✅ ДЗ выполнено', когда выполните."
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление ученику: {e}")

            await update.message.reply_text(
                f"✅ ДЗ успешно добавлено для ученика {student['full_name']}!\n"
                f"Дедлайн: {deadline_str}",
                reply_markup=ReplyKeyboardRemove()
            )

            # Очищаем временные данные
            context.user_data.clear()

        except ValueError:
            await update.message.reply_text("❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
            return WAITING_HW_DEADLINE

    await show_tutor_menu(update, context)
    return ConversationHandler.END


async def add_lesson_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить время занятия от репетитора"""
    if update.message:
        try:
            lesson_time_str = update.message.text
            lesson_time = datetime.strptime(lesson_time_str, '%d.%m.%Y %H:%M')
            context.user_data['lesson_time'] = lesson_time

            await update.message.reply_text(
                "📌 Введите тему занятия (или отправьте '-' чтобы пропустить):",
                reply_markup=ReplyKeyboardRemove()
            )
            return WAITING_SCHEDULE_TOPIC
        except ValueError:
            await update.message.reply_text("❌ Неверный формат! Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
            return WAITING_SCHEDULE_TIME
    return ConversationHandler.END


async def add_lesson_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить тему занятия и сохранить"""
    if update.message:
        topic = update.message.text if update.message.text != '-' else None
        lesson_time = context.user_data.get('lesson_time')
        student_id = context.user_data.get('selected_student')

        # Находим ID ученика в нашей БД
        student = await get_user(student_id)
        tutor = await get_user(update.effective_user.id)

        if not student or not tutor:
            await update.message.reply_text("Ошибка: пользователь не найден!")
            return ConversationHandler.END

        # Сохраняем занятие в БД
        await db_execute(
            '''INSERT INTO schedule (student_id, tutor_id, lesson_time, topic) 
               VALUES (%s, %s, %s, %s)''',
            (student['id'], tutor['id'], lesson_time, topic)
        )

        # Планируем напоминание о занятии
        await schedule_lesson_reminder(student_id, lesson_time, topic, student['full_name'])

        await update.message.reply_text(
            f"✅ Занятие успешно добавлено для ученика {student['full_name']}!\n"
            f"Время: {lesson_time.strftime('%d.%m.%Y %H:%M')}\n"
            f"Тема: {topic if topic else 'Не указана'}",
            reply_markup=ReplyKeyboardRemove()
        )

        # Очищаем временные данные
        context.user_data.clear()

    await show_tutor_menu(update, context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    await show_tutor_menu(update, context)
    return ConversationHandler.END


# ====================== НАПОМИНАНИЯ ======================
scheduler = AsyncIOScheduler(timezone=timezone(TIMEZONE))


async def schedule_hw_reminders(student_id: int, deadline: datetime, hw_text: str, student_name: str):
    """Запланировать напоминания о дедлайне ДЗ"""

    # Напоминание за 24 часа
    reminder_24h = deadline - timedelta(hours=24)
    if reminder_24h > datetime.now():
        scheduler.add_job(
            send_hw_reminder,
            'date',
            run_date=reminder_24h,
            args=[student_id,
                  f"⏰ Напоминание: ДЗ через 24 часа!\n📝 {hw_text[:100]}...\n📅 Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}"],
            id=f"hw_24h_{student_id}_{deadline.timestamp()}",
            replace_existing=True
        )

    # Напоминание за 1 час
    reminder_1h = deadline - timedelta(hours=1)
    if reminder_1h > datetime.now():
        scheduler.add_job(
            send_hw_reminder,
            'date',
            run_date=reminder_1h,
            args=[student_id, f"⏰ СРОЧНО: ДЗ через 1 час!\n📝 {hw_text[:100]}..."],
            id=f"hw_1h_{student_id}_{deadline.timestamp()}",
            replace_existing=True
        )

    logger.info(f"Запланированы напоминания для {student_name} на {deadline}")


async def schedule_lesson_reminder(student_id: int, lesson_time: datetime, topic: str, student_name: str):
    """Запланировать напоминание о занятии"""
    reminder_time = lesson_time - timedelta(hours=1)

    if reminder_time > datetime.now():
        topic_text = f" по теме: {topic[:50]}..." if topic else ""
        scheduler.add_job(
            send_hw_reminder,
            'date',
            run_date=reminder_time,
            args=[student_id,
                  f"👨‍🏫 Напоминание: занятие через 1 час{topic_text}\n🕐 Начало: {lesson_time.strftime('%d.%m.%Y %H:%M')}"],
            id=f"lesson_{student_id}_{lesson_time.timestamp()}",
            replace_existing=True
        )
        logger.info(f"Запланировано напоминание о занятии для {student_name} на {lesson_time}")


async def send_hw_reminder(chat_id: int, message: str):
    """Отправить напоминание пользователю"""
    try:
        from bot import application
        await application.bot.send_message(chat_id=chat_id, text=message)
        logger.info(f"Напоминание отправлено пользователю {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания пользователю {chat_id}: {e}")


async def restart_scheduler():
    """Перезапустить планировщик и загрузить напоминания из БД"""
    scheduler.remove_all_jobs()

    # Загружаем активные ДЗ и планируем напоминания
    active_hws = await db_fetchall('''
        SELECT h.deadline, h.task_text, u.telegram_id, u.full_name
        FROM homeworks h
        JOIN users u ON h.student_id = u.id
        WHERE h.deadline > CURRENT_TIMESTAMP AND h.is_completed = FALSE
    ''')

    for hw in active_hws:
        await schedule_hw_reminders(
            hw['telegram_id'],
            hw['deadline'],
            hw['task_text'],
            hw['full_name']
        )

    # Загружаем предстоящие занятия
    upcoming_lessons = await db_fetchall('''
        SELECT s.lesson_time, s.topic, u.telegram_id, u.full_name
        FROM schedule s
        JOIN users u ON s.student_id = u.id
        WHERE s.lesson_time > CURRENT_TIMESTAMP
    ''')

    for lesson in upcoming_lessons:
        await schedule_lesson_reminder(
            lesson['telegram_id'],
            lesson['lesson_time'],
            lesson['topic'],
            lesson['full_name']
        )

    logger.info(f"Планировщик перезапущен: {len(active_hws)} ДЗ, {len(upcoming_lessons)} занятий")


# ====================== КЛАВИАТУРЫ ======================
def get_student_keyboard():
    """Клавиатура для ученика"""
    keyboard = [
        [InlineKeyboardButton("✅ ДЗ выполнено", callback_data='hw_done')],
        [InlineKeyboardButton("📚 Мои ДЗ", callback_data='my_homework')],
        [InlineKeyboardButton("🗓 Моё расписание", callback_data='my_schedule')],
    ]
    return InlineKeyboardMarkup(keyboard)


# ====================== ЗАПУСК БОТА ======================
async def main():
    """Запуск бота"""
    if not TOKEN:
        logger.error("Токен бота не найден! Укажите TELEGRAM_BOT_TOKEN в переменных окружения")
        return

    if not DATABASE_URL:
        logger.error("DATABASE_URL не найден! На Render.com эта переменная должна быть установлена автоматически")
        return

    # Инициализация БД
    init_db()

    # Создаем приложение
    application = Application.builder().token(TOKEN).build()

    # Сохраняем application глобально для напоминаний
    globals()['application'] = application

    # Запускаем планировщик
    scheduler.start()
    await restart_scheduler()

    # Conversation handler для добавления ДЗ
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^select_student_hw:')],
        states={
            WAITING_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_hw_text)],
            WAITING_HW_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_hw_deadline)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Conversation handler для добавления занятия
    conv_handler_lesson = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern='^select_student_lesson:')],
        states={
            WAITING_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lesson_time)],
            WAITING_SCHEDULE_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_lesson_topic)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", show_tutor_menu))
    application.add_handler(conv_handler)
    application.add_handler(conv_handler_lesson)
    application.add_handler(CallbackQueryHandler(button_handler))

    async def echo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Простой обработчик для случайных сообщений"""
        await update.message.reply_text(
            "Пожалуйста, используйте команды из меню или кнопки."
        )

    # Запускаем бота
    logger.info("Бот запущен с PostgreSQL...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

    if __name__ == '__main__':
        asyncio.run(main())