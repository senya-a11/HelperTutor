import os
import sys
import logging
import asyncio
import signal
import atexit
import re
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv
from pytz import timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Импорт для веб-сервера
try:
    from aiohttp import web

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ====================== НАСТРОЙКИ ======================
load_dotenv()

# Получаем порт из окружения (Render автоматически назначает PORT)
PORT = int(os.getenv('PORT', 8080))

# Состояния для ConversationHandler
WAITING_HW_TEXT, WAITING_HW_DEADLINE, WAITING_HW_STUDENT, WAITING_LESSON_TIME, WAITING_LESSON_TOPIC, WAITING_LESSON_STUDENT = range(
    6)


# Безопасное получение переменных
def safe_getenv(key, default=None):
    value = os.getenv(key, default)
    if value:
        try:
            return value.encode('utf-8').decode('utf-8')
        except:
            return ''.join(c for c in str(value) if ord(c) < 128)
    return value


TOKEN = safe_getenv('TELEGRAM_BOT_TOKEN')
TUTOR_ID = int(safe_getenv('TUTOR_ID', '0') or 0)
TIMEZONE = safe_getenv('TIMEZONE', 'Europe/Moscow')

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ======================
application = None
scheduler = None
web_runner = None

# ====================== ХРАНИЛИЩЕ В ПАМЯТИ ======================
users_db = {}  # telegram_id -> {id, username, full_name, role, created_at}
homeworks_db = []  # [{id, student_id, tutor_id, task_text, deadline, is_completed, completed_at}]
lessons_db = []  # [{id, student_id, tutor_id, lesson_time, topic, notify_student}]
next_id = 1


# ====================== ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECKS ======================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    """Простой HTTP обработчик для health checks"""

    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            status = {
                'status': 'ok',
                'timestamp': datetime.now().isoformat(),
                'service': 'helper-tutor-bot',
                'stats': {
                    'users': len(users_db),
                    'homeworks': len(homeworks_db),
                    'lessons': len(lessons_db),
                    'active_homeworks': len([h for h in homeworks_db if not h.get('is_completed')])
                }
            }
            import json
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.debug(f"HTTP {self.address_string()} - {format % args}")


def run_http_server():
    """Запуск HTTP сервера в отдельном потоке"""
    server = HTTPServer(('0.0.0.0', PORT), SimpleHTTPRequestHandler)
    logger.info(f"🌐 HTTP сервер запущен на порту {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


async def start_web_server():
    """Запуск веб-сервера (aiohttp если доступен, иначе простой HTTP)"""
    global web_runner

    if HAS_AIOHTTP:
        # Используем aiohttp если установлен
        app = web.Application()

        async def health_check(request):
            status = {
                'status': 'ok',
                'timestamp': datetime.now().isoformat(),
                'service': 'helper-tutor-bot',
                'stats': {
                    'users': len(users_db),
                    'homeworks': len(homeworks_db),
                    'lessons': len(lessons_db),
                    'active_homeworks': len([h for h in homeworks_db if not h.get('is_completed')])
                }
            }
            return web.json_response(status)

        app.router.add_get('/health', health_check)
        app.router.add_get('/', health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()

        web_runner = runner
        logger.info(f"🌐 aiohttp сервер запущен на порту {PORT}")
        return runner
    else:
        # Запускаем простой HTTP сервер в отдельном потоке
        thread = threading.Thread(target=run_http_server, daemon=True)
        thread.start()
        logger.info(f"🌐 Простой HTTP сервер запущен на порту {PORT}")
        return thread


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def get_next_id():
    global next_id
    next_id += 1
    return next_id - 1


def get_user(telegram_id: int):
    return users_db.get(telegram_id)


def register_user(telegram_id: int, username: str, full_name: str, role: str = 'student'):
    if telegram_id not in users_db:
        users_db[telegram_id] = {
            'id': telegram_id,
            'telegram_id': telegram_id,
            'username': username,
            'full_name': full_name,
            'role': role,
            'created_at': datetime.now().isoformat()
        }
        return True
    return False


def is_tutor(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    if user:
        return user['role'] == 'tutor'
    return telegram_id == TUTOR_ID


def format_datetime(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime('%d.%m.%Y %H:%M')
    except:
        return dt_str


def parse_datetime(dt_str):
    try:
        return datetime.strptime(dt_str, '%d.%m.%Y %H:%M')
    except ValueError:
        try:
            return datetime.strptime(dt_str, '%d.%m.%Y')
        except:
            return None


def get_students():
    return [u for u in users_db.values() if u.get('role') == 'student']


def get_homeworks_for_student(student_id):
    return [h for h in homeworks_db if h['student_id'] == student_id and not h.get('is_completed')]


def get_active_homeworks():
    now = datetime.now().isoformat()
    return [h for h in homeworks_db if h['deadline'] > now and not h.get('is_completed')]


def get_upcoming_lessons():
    now = datetime.now().isoformat()
    return [l for l in lessons_db if l['lesson_time'] > now]


# ====================== НАПОМИНАНИЯ ======================
def schedule_reminders():
    """Запланировать напоминания для активных ДЗ и занятий"""
    # Сбрасываем старые задания
    if scheduler:
        scheduler.remove_all_jobs()

    now = datetime.now()

    # Напоминания о ДЗ
    for hw in get_active_homeworks():
        try:
            deadline = datetime.fromisoformat(hw['deadline'])
            student = get_user(hw['student_id'])

            if not student:
                continue

            # За 24 часа
            reminder_24h = deadline - timedelta(hours=24)
            if reminder_24h > now:
                scheduler.add_job(
                    send_reminder,
                    'date',
                    run_date=reminder_24h,
                    args=[student['telegram_id'],
                          f"⏰ Напоминание: ДЗ через 24 часа!\n📝 {hw['task_text'][:50]}...\n📅 Дедлайн: {format_datetime(hw['deadline'])}"],
                    id=f"hw_24h_{hw['id']}"
                )

            # За 1 час
            reminder_1h = deadline - timedelta(hours=1)
            if reminder_1h > now:
                scheduler.add_job(
                    send_reminder,
                    'date',
                    run_date=reminder_1h,
                    args=[student['telegram_id'],
                          f"⏰ СРОЧНО: ДЗ через 1 час!\n📝 {hw['task_text'][:50]}..."],
                    id=f"hw_1h_{hw['id']}"
                )
        except Exception as e:
            logger.error(f"Ошибка планирования напоминания ДЗ: {e}")

    # Напоминания о занятиях
    for lesson in get_upcoming_lessons():
        try:
            lesson_time = datetime.fromisoformat(lesson['lesson_time'])
            student = get_user(lesson['student_id'])

            if not student or not lesson.get('notify_student', True):
                continue

            # За 1 час
            reminder_1h = lesson_time - timedelta(hours=1)
            if reminder_1h > now:
                topic = f" по теме: {lesson['topic']}" if lesson.get('topic') else ""
                scheduler.add_job(
                    send_reminder,
                    'date',
                    run_date=reminder_1h,
                    args=[student['telegram_id'],
                          f"👨‍🏫 Напоминание: занятие через 1 час{topic}\n🕐 Начало: {format_datetime(lesson['lesson_time'])}"],
                    id=f"lesson_{lesson['id']}"
                )
        except Exception as e:
            logger.error(f"Ошибка планирования напоминания занятия: {e}")


async def send_reminder(chat_id: int, message: str):
    """Отправить напоминание"""
    try:
        if application:
            await application.bot.send_message(chat_id=chat_id, text=message)
            logger.info(f"Напоминание отправлено {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания: {e}")


# ====================== КОМАНДЫ БОТА ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    user_id = user.id

    if is_tutor(user_id):
        role = 'tutor'
        welcome_text = f"""
👨‍🏫 Добро пожаловать, репетитор {user.full_name}!

Ваш ID: {user.id}
Используйте /menu для управления
"""
        reply_markup = get_tutor_main_keyboard()
    else:
        role = 'student'
        welcome_text = f"""
👨‍🎓 Привет, {user.full_name}!

Я бот-помощник репетитора HelperTutor.

Я помогу вам:
• Следить за домашними заданиями
• Отмечать выполненные работы
• Не пропускать занятия
• Получать напоминания
"""
        reply_markup = get_student_main_keyboard()

    register_user(user_id, user.username, user.full_name, role)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню репетитора"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    await update.message.reply_text(
        "📊 Панель управления репетитора:",
        reply_markup=get_tutor_main_keyboard()
    )


async def tutor_add_hw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать добавление ДЗ"""
    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id

    if not is_tutor(user_id):
        if update.callback_query:
            await query.edit_message_text("Доступно только репетитору!")
        return

    students = get_students()
    if not students:
        await update.callback_query.edit_message_text(
            "Нет зарегистрированных учеников.",
            reply_markup=get_tutor_main_keyboard()
        )
        return ConversationHandler.END

    keyboard = []
    for student in students:
        keyboard.append([InlineKeyboardButton(
            f"👤 {student['full_name']}",
            callback_data=f"select_student_hw:{student['telegram_id']}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    if update.callback_query:
        await query.edit_message_text(
            "👥 Выберите ученика для ДЗ:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return WAITING_HW_STUDENT


async def tutor_select_student_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрать ученика для ДЗ"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    context.user_data['selected_student'] = student_id

    await query.edit_message_text(
        "✏️ Введите текст домашнего задания:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    )

    return WAITING_HW_TEXT


async def tutor_hw_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить текст ДЗ"""
    text = update.message.text
    context.user_data['hw_text'] = text

    await update.message.reply_text(
        "📅 Введите дедлайн (формат: ДД.ММ.ГГГГ ЧЧ:ММ):",
        reply_markup=ReplyKeyboardRemove()
    )

    return WAITING_HW_DEADLINE


async def tutor_hw_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить дедлайн ДЗ и сохранить"""
    deadline_str = update.message.text
    deadline = parse_datetime(deadline_str)

    if not deadline:
        await update.message.reply_text(
            "❌ Неверный формат! Используйте ДД.ММ.ГГГГ ЧЧ:ММ\nПопробуйте еще раз:"
        )
        return WAITING_HW_DEADLINE

    student_id = context.user_data.get('selected_student')
    hw_text = context.user_data.get('hw_text')
    tutor_id = update.effective_user.id

    if not all([student_id, hw_text, tutor_id]):
        await update.message.reply_text("❌ Ошибка данных. Начните заново.")
        return ConversationHandler.END

    # Сохраняем ДЗ
    hw_id = get_next_id()
    homeworks_db.append({
        'id': hw_id,
        'student_id': student_id,
        'tutor_id': tutor_id,
        'task_text': hw_text,
        'deadline': deadline.isoformat(),
        'is_completed': False,
        'completed_at': None,
        'created_at': datetime.now().isoformat()
    })

    # Обновляем напоминания
    schedule_reminders()

    # Отправляем уведомление ученику
    student = get_user(student_id)
    if student:
        try:
            await update._bot.send_message(
                chat_id=student_id,
                text=f"📚 Новое домашнее задание!\n\n📝 {hw_text}\n📅 Дедлайн: {deadline_str}\n\nНажмите '✅ ДЗ выполнено' когда выполните."
            )
        except:
            pass

    await update.message.reply_text(
        f"✅ ДЗ успешно добавлено для {student['full_name'] if student else 'ученика'}!\n"
        f"Дедлайн: {deadline_str}",
        reply_markup=get_tutor_main_keyboard()
    )

    # Очищаем временные данные
    context.user_data.clear()

    return ConversationHandler.END


async def tutor_list_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список ДЗ"""
    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
    else:
        query = None

    active_hws = get_active_homeworks()

    if not active_hws:
        text = "📭 Нет активных домашних заданий."
    else:
        text = "📚 Активные домашние задания:\n\n"
        for hw in active_hws[:10]:  # Показываем первые 10
            student = get_user(hw['student_id'])
            tutor = get_user(hw['tutor_id'])
            status = "✅ Выполнено" if hw.get('is_completed') else "⏳ В процессе"
            text += f"👤 Ученик: {student['full_name'] if student else 'Неизвестен'}\n"
            text += f"👨‍🏫 Репетитор: {tutor['full_name'] if tutor else 'Неизвестен'}\n"
            text += f"📝 {hw['task_text'][:50]}...\n"
            text += f"📅 Дедлайн: {format_datetime(hw['deadline'])}\n"
            text += f"{status}\n\n"

    if query:
        await query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_list_students(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список учеников"""
    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
    else:
        query = None

    students = get_students()

    if not students:
        text = "👥 Нет зарегистрированных учеников."
    else:
        text = f"👥 Список учеников ({len(students)}):\n\n"
        for student in students:
            username = f"(@{student['username']})" if student['username'] else ""
            hws = get_homeworks_for_student(student['telegram_id'])
            completed = len(
                [h for h in homeworks_db if h['student_id'] == student['telegram_id'] and h.get('is_completed')])
            text += f"• {student['full_name']} {username}\n"
            text += f"  📊 Активных ДЗ: {len(hws)}, Выполнено: {completed}\n\n"

    if query:
        await query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_add_lesson_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать добавление занятия"""
    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id

    if not is_tutor(user_id):
        if update.callback_query:
            await query.edit_message_text("Доступно только репетитору!")
        return

    students = get_students()
    if not students:
        await update.callback_query.edit_message_text(
            "Нет зарегистрированных учеников.",
            reply_markup=get_tutor_main_keyboard()
        )
        return ConversationHandler.END

    keyboard = []
    for student in students:
        keyboard.append([InlineKeyboardButton(
            f"👤 {student['full_name']}",
            callback_data=f"select_student_lesson:{student['telegram_id']}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    if update.callback_query:
        await query.edit_message_text(
            "👥 Выберите ученика для занятия:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return WAITING_LESSON_STUDENT


async def tutor_select_student_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрать ученика для занятия"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    context.user_data['selected_student'] = student_id

    await query.edit_message_text(
        "🕐 Введите дату и время занятия (формат: ДД.ММ.ГГГГ ЧЧ:ММ):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    )

    return WAITING_LESSON_TIME


async def tutor_lesson_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить время занятия"""
    time_str = update.message.text
    lesson_time = parse_datetime(time_str)

    if not lesson_time:
        await update.message.reply_text(
            "❌ Неверный формат! Используйте ДД.ММ.ГГГГ ЧЧ:ММ\nПопробуйте еще раз:"
        )
        return WAITING_LESSON_TIME

    context.user_data['lesson_time'] = lesson_time.isoformat()

    await update.message.reply_text(
        "📌 Введите тему занятия (или отправьте '-' чтобы пропустить):",
        reply_markup=ReplyKeyboardRemove()
    )

    return WAITING_LESSON_TOPIC


async def tutor_lesson_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить тему занятия и сохранить"""
    topic = update.message.text if update.message.text != '-' else None
    student_id = context.user_data.get('selected_student')
    lesson_time = context.user_data.get('lesson_time')
    tutor_id = update.effective_user.id

    if not all([student_id, lesson_time, tutor_id]):
        await update.message.reply_text("❌ Ошибка данных. Начните заново.")
        return ConversationHandler.END

    # Сохраняем занятие
    lesson_id = get_next_id()
    lessons_db.append({
        'id': lesson_id,
        'student_id': student_id,
        'tutor_id': tutor_id,
        'lesson_time': lesson_time,
        'topic': topic,
        'notify_student': True,
        'created_at': datetime.now().isoformat()
    })

    # Обновляем напоминания
    schedule_reminders()

    # Отправляем уведомление ученику
    student = get_user(student_id)
    if student:
        try:
            await update._bot.send_message(
                chat_id=student_id,
                text=f"📅 Новое занятие!\n\n🕐 {format_datetime(lesson_time)}\n"
                     f"📌 Тема: {topic if topic else 'Не указана'}"
            )
        except:
            pass

    await update.message.reply_text(
        f"✅ Занятие успешно добавлено для {student['full_name'] if student else 'ученика'}!\n"
        f"Время: {format_datetime(lesson_time)}\n"
        f"Тема: {topic if topic else 'Не указана'}",
        reply_markup=get_tutor_main_keyboard()
    )

    # Очищаем временные данные
    context.user_data.clear()

    return ConversationHandler.END


async def tutor_list_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список занятий"""
    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
    else:
        query = None

    upcoming_lessons = get_upcoming_lessons()

    if not upcoming_lessons:
        text = "🗓 Нет запланированных занятий."
    else:
        text = "🗓 Ближайшие занятия:\n\n"
        for lesson in upcoming_lessons[:10]:  # Показываем первые 10
            student = get_user(lesson['student_id'])
            tutor = get_user(lesson['tutor_id'])
            notify = "🔔" if lesson.get('notify_student', True) else "🔕"
            text += f"👤 Ученик: {student['full_name'] if student else 'Неизвестен'}\n"
            text += f"👨‍🏫 Репетитор: {tutor['full_name'] if tutor else 'Неизвестен'}\n"
            text += f"🕐 {format_datetime(lesson['lesson_time'])}\n"
            text += f"📌 Тема: {lesson.get('topic', 'Не указана')}\n"
            text += f"{notify} Уведомления\n\n"

    if query:
        await query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление напоминаниями"""
    schedule_reminders()

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "🔄 Напоминания обновлены!",
            reply_markup=get_tutor_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "🔄 Напоминания обновлены!",
            reply_markup=get_tutor_main_keyboard()
        )


async def student_hw_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ученик отмечает ДЗ выполненным"""
    user_id = update.effective_user.id

    # Находим активные ДЗ для ученика
    student_hws = get_homeworks_for_student(user_id)

    if not student_hws:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "📭 У вас нет активных домашних заданий.",
                reply_markup=get_student_main_keyboard()
            )
        return

    # Берем первое невыполненное ДЗ
    hw = student_hws[0]
    hw['is_completed'] = True
    hw['completed_at'] = datetime.now().isoformat()

    # Отправляем уведомление репетитору
    tutor = get_user(hw['tutor_id'])
    student = get_user(user_id)

    if tutor and TUTOR_ID:
        try:
            await update._bot.send_message(
                chat_id=TUTOR_ID,
                text=f"🎉 Ученик {student['full_name'] if student else 'Неизвестен'} выполнил ДЗ!\n\n"
                     f"📝 {hw['task_text'][:100]}...\n"
                     f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
        except:
            pass

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"✅ Отлично! Вы выполнили задание:\n\n"
            f"📝 {hw['task_text'][:200]}...\n\n"
            f"Репетитор получил уведомление.",
            reply_markup=get_student_main_keyboard()
        )


async def student_my_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мои ДЗ"""
    user_id = update.effective_user.id

    all_hws = [h for h in homeworks_db if h['student_id'] == user_id]
    active_hws = [h for h in all_hws if not h.get('is_completed')]
    completed_hws = [h for h in all_hws if h.get('is_completed')]

    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
    else:
        query = None

    if not active_hws and not completed_hws:
        text = "📭 У вас нет домашних заданий."
    else:
        text = "📚 Ваши домашние задания:\n\n"

        if active_hws:
            text += "⏳ Активные:\n"
            for hw in active_hws[:5]:  # Показываем первые 5
                text += f"• {hw['task_text'][:50]}...\n"
                text += f"  📅 Дедлайн: {format_datetime(hw['deadline'])}\n\n"

        if completed_hws:
            text += "✅ Выполненные:\n"
            for hw in completed_hws[-3:]:  # Показываем последние 3
                completed_at = format_datetime(hw.get('completed_at', ''))
                text += f"• {hw['task_text'][:50]}...\n"
                text += f"  🏁 Выполнено: {completed_at}\n\n"

    if query:
        await query.edit_message_text(text, reply_markup=get_student_main_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_student_main_keyboard())


async def student_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мое расписание"""
    user_id = update.effective_user.id

    student_lessons = [l for l in lessons_db if l['student_id'] == user_id]
    upcoming_lessons = [l for l in student_lessons if l['lesson_time'] > datetime.now().isoformat()]
    past_lessons = [l for l in student_lessons if l['lesson_time'] <= datetime.now().isoformat()]

    if update.callback_query:
        await update.callback_query.answer()
        query = update.callback_query
    else:
        query = None

    if not upcoming_lessons and not past_lessons:
        text = "🗓 У вас нет запланированных занятий."
    else:
        text = "🗓 Ваше расписание:\n\n"

        if upcoming_lessons:
            text += "📅 Предстоящие:\n"
            for lesson in upcoming_lessons[:5]:  # Показываем первые 5
                text += f"• {format_datetime(lesson['lesson_time'])}\n"
                text += f"  📌 {lesson.get('topic', 'Без темы')}\n"
                text += f"  🔔 {'Уведомление включено' if lesson.get('notify_student', True) else 'Уведомление выключено'}\n\n"

        if past_lessons:
            text += "📜 Прошедшие:\n"
            for lesson in past_lessons[-3:]:  # Показываем последние 3
                text += f"• {format_datetime(lesson['lesson_time'])}\n"
                text += f"  📌 {lesson.get('topic', 'Без темы')}\n\n"

    if query:
        await query.edit_message_text(text, reply_markup=get_student_main_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=get_student_main_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Справка"""
    help_text = """
📚 HelperTutor - Бот-помощник репетитора

👨‍🏫 Для репетитора:
/menu - Панель управления
• Добавление ДЗ и занятий
• Просмотр учеников
• Управление напоминаниями

👨‍🎓 Для учеников:
• ✅ ДЗ выполнено - отметка выполнения
• 📚 Мои ДЗ - список заданий
• 🗓 Расписание - занятия

🔔 Функции:
• Автоматические напоминания
• Уведомления репетитору
• История заданий
• Управление расписанием

💡 Совет: Регулярно проверяйте ДЗ и расписание!
"""

    if update.message:
        await update.message.reply_text(help_text)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(help_text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена"""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Действие отменено.",
            reply_markup=get_tutor_main_keyboard() if is_tutor(update.callback_query.from_user.id)
            else get_student_main_keyboard()
        )
    elif update.message:
        await update.message.reply_text(
            "❌ Действие отменено.",
            reply_markup=get_tutor_main_keyboard() if is_tutor(update.effective_user.id)
            else get_student_main_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных сообщений"""
    if update.message:
        user_id = update.effective_user.id
        if is_tutor(user_id):
            await update.message.reply_text(
                "Используйте /menu для управления.",
                reply_markup=get_tutor_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "Используйте кнопки ниже:",
                reply_markup=get_student_main_keyboard()
            )


# ====================== КЛАВИАТУРЫ ======================
def get_tutor_main_keyboard():
    """Основная клавиатура репетитора"""
    keyboard = [
        [InlineKeyboardButton("📝 Добавить ДЗ", callback_data='tutor_add_hw')],
        [InlineKeyboardButton("📋 Список ДЗ", callback_data='tutor_list_hw')],
        [InlineKeyboardButton("📅 Добавить занятие", callback_data='tutor_add_lesson')],
        [InlineKeyboardButton("🗓 Расписание", callback_data='tutor_list_lessons')],
        [InlineKeyboardButton("👥 Ученики", callback_data='tutor_list_students')],
        [InlineKeyboardButton("🔄 Обновить напоминания", callback_data='tutor_reminders')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_student_main_keyboard():
    """Основная клавиатура ученика"""
    keyboard = [
        [InlineKeyboardButton("✅ ДЗ выполнено", callback_data='student_hw_done')],
        [InlineKeyboardButton("📚 Мои ДЗ", callback_data='student_my_hw')],
        [InlineKeyboardButton("🗓 Расписание", callback_data='student_schedule')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    return InlineKeyboardMarkup(keyboard)


# ====================== ОБРАБОТЧИКИ ОШИБОК ======================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)

    # Обработка конфликта (когда запущено несколько ботов)
    if "Conflict" in str(context.error) and "getUpdates" in str(context.error):
        logger.error("⚠️ Обнаружен конфликт! Возможно запущено несколько экземпляров бота.")

    # Отправляем сообщение пользователю при ошибке
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже или обратитесь к администратору."
            )
    except:
        pass


# ====================== GRACEFUL SHUTDOWN ======================
def shutdown_handler(signum=None, frame=None):
    """Обработчик завершения работы"""
    logger.info("🚫 Получен сигнал завершения...")

    global scheduler, application, web_runner

    # Останавливаем планировщик
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("✅ Планировщик остановлен")

    # Останавливаем бота
    if application:
        try:
            # Останавливаем polling
            if application.updater and application.updater.running:
                application.updater.stop()

            # Останавливаем application
            application.stop()
            application.shutdown()
            logger.info("✅ Бот остановлен")
        except Exception as e:
            logger.error(f"❌ Ошибка при остановке бота: {e}")

    # Останавливаем веб-сервер
    if HAS_AIOHTTP and web_runner:
        try:
            import asyncio as async_lib
            loop = async_lib.new_event_loop()
            async_lib.set_event_loop(loop)
            loop.run_until_complete(web_runner.cleanup())
            logger.info("✅ Веб-сервер остановлен")
        except Exception as e:
            logger.error(f"❌ Ошибка при остановке веб-сервера: {e}")

    logger.info("👋 Бот завершил работу")
    sys.exit(0)


def register_shutdown_handlers():
    """Регистрация обработчиков завершения"""
    # Для Ctrl+C
    signal.signal(signal.SIGINT, shutdown_handler)

    # Для системных сигналов
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, shutdown_handler)

    # При выходе
    atexit.register(shutdown_handler)


# ====================== ОСНОВНАЯ ФУНКЦИЯ ======================
async def main_async():
    """Асинхронная основная функция"""
    global application, scheduler

    logger.info("=" * 60)
    logger.info("🚀 ЗАПУСК HELPER TUTOR BOT")
    logger.info("=" * 60)

    # Проверка токена
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        logger.info("💡 Добавьте на Render: TELEGRAM_BOT_TOKEN = ваш_токен")
        return

    logger.info(f"✅ Токен: установлен")
    logger.info(f"✅ Репетитор ID: {TUTOR_ID if TUTOR_ID else 'не установлен'}")
    logger.info(f"✅ Порт веб-сервера: {PORT}")

    # Запускаем веб-сервер для health checks
    await start_web_server()

    try:
        # Для Windows
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        # Создаем приложение
        application = Application.builder().token(TOKEN).build()

        # Добавляем обработчик ошибок
        application.add_error_handler(error_handler)

        # Создаем планировщик
        scheduler = AsyncIOScheduler(timezone=timezone(TIMEZONE))
        scheduler.start()

        # Conversation Handler для добавления ДЗ
        conv_hw_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(tutor_select_student_hw, pattern='^select_student_hw:')],
            states={
                WAITING_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_hw_text)],
                WAITING_HW_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_hw_deadline)],
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        )

        # Conversation Handler для добавления занятия
        conv_lesson_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(tutor_select_student_lesson, pattern='^select_student_lesson:')],
            states={
                WAITING_LESSON_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_lesson_time)],
                WAITING_LESSON_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_lesson_topic)],
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        )

        # Регистрируем обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu))
        application.add_handler(CommandHandler("help", help_command))

        # Обработчики кнопок репетитора
        application.add_handler(CallbackQueryHandler(tutor_add_hw_start, pattern='^tutor_add_hw$'))
        application.add_handler(CallbackQueryHandler(tutor_list_hw, pattern='^tutor_list_hw$'))
        application.add_handler(CallbackQueryHandler(tutor_add_lesson_start, pattern='^tutor_add_lesson$'))
        application.add_handler(CallbackQueryHandler(tutor_list_lessons, pattern='^tutor_list_lessons$'))
        application.add_handler(CallbackQueryHandler(tutor_list_students, pattern='^tutor_list_students$'))
        application.add_handler(CallbackQueryHandler(tutor_reminders, pattern='^tutor_reminders$'))

        # Обработчики кнопок ученика
        application.add_handler(CallbackQueryHandler(student_hw_done, pattern='^student_hw_done$'))
        application.add_handler(CallbackQueryHandler(student_my_hw, pattern='^student_my_hw$'))
        application.add_handler(CallbackQueryHandler(student_schedule, pattern='^student_schedule$'))

        # Conversation handlers
        application.add_handler(conv_hw_handler)
        application.add_handler(conv_lesson_handler)

        # Общий обработчик кнопок
        application.add_handler(CallbackQueryHandler(help_command, pattern='^help$'))
        application.add_handler(CallbackQueryHandler(cancel, pattern='^cancel$'))

        # Обработчик неизвестных сообщений
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

        logger.info("✅ Обработчики зарегистрированы")

        # Запускаем планировщик напоминаний
        schedule_reminders()
        logger.info("✅ Планировщик запущен")

        logger.info("🤖 Бот запускается...")

        # Запускаем бота
        await application.initialize()
        await application.start()
        await application.updater.start_polling()

        logger.info("✅ Бот успешно запущен!")
        logger.info("👉 Напишите боту /start в Telegram")
        logger.info(f"🌐 Health check доступен по адресу: http://0.0.0.0:{PORT}/health")

        # Бесконечный цикл чтобы приложение не завершалось
        while True:
            await asyncio.sleep(3600)  # Спим 1 час

    except asyncio.CancelledError:
        logger.info("🛑 Получен сигнал отмены")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
    finally:
        # Останавливаем бота
        if application:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
                logger.info("✅ Бот остановлен")
            except Exception as e:
                logger.error(f"❌ Ошибка при остановке бота: {e}")

        # Останавливаем планировщик
        if scheduler and scheduler.running:
            scheduler.shutdown()
            logger.info("✅ Планировщик остановлен")


def main():
    """Точка входа"""
    # Регистрируем обработчики завершения
    register_shutdown_handlers()

    # Запускаем асинхронную main
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("👋 Бот завершен пользователем")
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")


if __name__ == '__main__':
    main()