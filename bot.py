import os
import sys
import logging
import asyncio
import signal
import atexit
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv
from pytz import timezone, all_timezones, utc
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
(WAITING_HW_TEXT, WAITING_HW_DEADLINE, WAITING_HW_STUDENT,
 WAITING_LESSON_TIME, WAITING_LESSON_TOPIC, WAITING_LESSON_STUDENT,
 WAITING_DELETE_STUDENT, WAITING_SETTINGS_CHOICE, WAITING_NOTIFICATION_SETTINGS,
 WAITING_LIVES_SETTINGS, WAITING_TIMEZONE_SETTINGS) = range(11)


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
users_db = {}  # telegram_id -> user_data
homeworks_db = []  # список домашних заданий
lessons_db = []  # список занятий
next_id = 1
settings_db = {
    'notifications': {
        'homework_reminders': True,
        'lesson_reminders': True,
        'late_homework_alerts': True,
        'homework_24h': True,
        'homework_1h': True,
        'lesson_1h': True
    },
    'lives_system': {
        'enabled': True,
        'max_lives': 5,
        'penalty_for_late_hw': 1,
        'penalty_for_missed_lesson': 2,
        'reward_for_early_hw': 1,
        'auto_reset_days': 7
    },
    'timezone': TIMEZONE
}


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
            'created_at': datetime.now().isoformat(),
            'lives': settings_db['lives_system']['max_lives'],
            'last_life_reset': datetime.now().isoformat(),
            'timezone': settings_db['timezone']
        }
        return True
    return False


def is_tutor(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    if user:
        return user['role'] == 'tutor'
    return telegram_id == TUTOR_ID


def get_local_time(dt_str=None, user_tz=None):
    """Конвертирует время в локальное время пользователя"""
    try:
        if dt_str is None:
            dt = datetime.now()
        else:
            dt = datetime.fromisoformat(dt_str)

        # Используем таймзону пользователя или дефолтную
        tz = timezone(user_tz or settings_db['timezone'])
        local_dt = dt.astimezone(tz)
        return local_dt.strftime('%d.%m.%Y %H:%M')
    except Exception as e:
        logger.error(f"Ошибка конвертации времени: {e}")
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str)
                return dt.strftime('%d.%m.%Y %H:%M')
            except:
                return dt_str
        return datetime.now().strftime('%d.%m.%Y %H:%M')


def parse_datetime(dt_str, user_tz=None):
    """Парсит дату с учетом таймзоны пользователя"""
    try:
        # Парсим как локальное время
        dt = datetime.strptime(dt_str, '%d.%m.%Y %H:%M')

        # Если указана таймзона пользователя, применяем ее
        if user_tz:
            tz = timezone(user_tz)
            dt = tz.localize(dt)
        else:
            # Иначе используем дефолтную таймзону
            tz = timezone(settings_db['timezone'])
            dt = tz.localize(dt)

        # Конвертируем в UTC для хранения
        dt_utc = dt.astimezone(utc)
        return dt_utc
    except ValueError:
        try:
            dt = datetime.strptime(dt_str, '%d.%m.%Y')
            if user_tz:
                tz = timezone(user_tz)
                dt = tz.localize(dt.replace(hour=23, minute=59))
            else:
                tz = timezone(settings_db['timezone'])
                dt = tz.localize(dt.replace(hour=23, minute=59))
            dt_utc = dt.astimezone(utc)
            return dt_utc
        except Exception as e:
            logger.error(f"Ошибка парсинга даты: {e}")
            return None


def get_students():
    return [u for u in users_db.values() if u.get('role') == 'student']


def get_homeworks_for_student(student_id):
    return [h for h in homeworks_db if h['student_id'] == student_id and not h.get('is_completed')]


def get_active_homeworks():
    now_utc = datetime.now(utc).isoformat()
    return [h for h in homeworks_db if h['deadline'] > now_utc and not h.get('is_completed')]


def get_late_homeworks():
    now_utc = datetime.now(utc).isoformat()
    late_hws = []
    for hw in homeworks_db:
        if hw['deadline'] < now_utc and not hw.get('is_completed') and not hw.get('late_notified'):
            late_hws.append(hw)
    return late_hws


def get_upcoming_lessons():
    now_utc = datetime.now(utc).isoformat()
    return [l for l in lessons_db if l['lesson_time'] > now_utc]


def update_lives(student_id: int, delta: int):
    """Обновляет количество жизней ученика"""
    student = get_user(student_id)
    if student and settings_db['lives_system']['enabled']:
        current_lives = student.get('lives', settings_db['lives_system']['max_lives'])
        new_lives = max(0, min(current_lives + delta, settings_db['lives_system']['max_lives']))
        student['lives'] = new_lives

        # Отправляем уведомление при изменении жизней
        if delta < 0:
            try:
                asyncio.create_task(
                    application.bot.send_message(
                        chat_id=student_id,
                        text=f"⚠️ Снято {-delta} жизней! Осталось: {new_lives}/{settings_db['lives_system']['max_lives']}"
                    )
                )
            except:
                pass

        return new_lives
    return None


def check_and_reset_lives():
    """Проверяет и сбрасывает жизни по расписанию"""
    now = datetime.now(utc)
    for user in users_db.values():
        if user.get('role') == 'student':
            last_reset_str = user.get('last_life_reset')
            if last_reset_str:
                try:
                    last_reset = datetime.fromisoformat(last_reset_str).astimezone(utc)
                    days_passed = (now - last_reset).days
                    if days_passed >= settings_db['lives_system']['auto_reset_days']:
                        user['lives'] = settings_db['lives_system']['max_lives']
                        user['last_life_reset'] = now.isoformat()

                        # Уведомляем ученика
                        try:
                            asyncio.create_task(
                                application.bot.send_message(
                                    chat_id=user['telegram_id'],
                                    text=f"🎉 Жизни сброшены! Теперь у вас {settings_db['lives_system']['max_lives']}/{settings_db['lives_system']['max_lives']} жизней."
                                )
                            )
                        except:
                            pass
                except:
                    pass


# ====================== ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECKS ======================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    """Простой HTTP обработчик для health checks"""

    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            health_data = {
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'users_count': len(users_db),
                'homeworks_count': len(homeworks_db),
                'lessons_count': len(lessons_db)
            }
            self.wfile.write(json.dumps(health_data).encode())
        elif self.path == '/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            stats = {
                'users': len(users_db),
                'students': len(get_students()),
                'active_homeworks': len(get_active_homeworks()),
                'upcoming_lessons': len(get_upcoming_lessons()),
                'late_homeworks': len(get_late_homeworks())
            }
            self.wfile.write(json.dumps(stats).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.info(f"HTTP {self.address_string()} - {format % args}")


def run_http_server():
    """Запуск HTTP сервера в отдельном потоке"""
    server = HTTPServer(('0.0.0.0', PORT), SimpleHTTPRequestHandler)
    logger.info(f"🌐 HTTP сервер запущен на порту {PORT}")
    server.serve_forever()


async def start_web_server():
    """Запуск веб-сервера (aiohttp если доступен, иначе простой HTTP)"""
    global web_runner

    if HAS_AIOHTTP:
        # Используем aiohttp если установлен
        app = web.Application()

        async def health_check(request):
            return web.json_response({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'service': 'HelperTutor Bot'
            })

        async def stats_check(request):
            return web.json_response({
                'users': len(users_db),
                'students': len(get_students()),
                'active_homeworks': len(get_active_homeworks()),
                'upcoming_lessons': len(get_upcoming_lessons()),
                'settings': settings_db
            })

        app.router.add_get('/health', health_check)
        app.router.add_get('/', health_check)
        app.router.add_get('/stats', stats_check)

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


# ====================== НАПОМИНАНИЯ И УВЕДОМЛЕНИЯ ======================
def schedule_reminders():
    """Запланировать напоминания для активных ДЗ и занятий"""
    if not scheduler:
        return

    scheduler.remove_all_jobs()

    now_utc = datetime.now(utc)

    # Запланировать проверку просроченных ДЗ каждые 6 часов
    scheduler.add_job(
        check_late_homeworks,
        'interval',
        hours=6,
        id='check_late_homeworks'
    )

    # Запланировать сброс жизней каждые 24 часа
    scheduler.add_job(
        check_and_reset_lives,
        'interval',
        hours=24,
        id='reset_lives_check'
    )

    # Напоминания о ДЗ (если включены в настройках)
    if settings_db['notifications']['homework_reminders']:
        for hw in get_active_homeworks():
            try:
                deadline = datetime.fromisoformat(hw['deadline']).astimezone(utc)
                student = get_user(hw['student_id'])

                if not student:
                    continue

                # Получаем таймзону ученика
                student_tz = student.get('timezone', settings_db['timezone'])

                # За 24 часа (если включено)
                if settings_db['notifications']['homework_24h']:
                    reminder_24h = deadline - timedelta(hours=24)
                    if reminder_24h > now_utc:
                        scheduler.add_job(
                            send_reminder,
                            'date',
                            run_date=reminder_24h,
                            args=[student['telegram_id'],
                                  f"⏰ Напоминание: ДЗ через 24 часа!\n📝 {hw['task_text'][:50]}...\n📅 Дедлайн: {get_local_time(hw['deadline'], student_tz)}"],
                            id=f"hw_24h_{hw['id']}"
                        )

                # За 1 час (если включено)
                if settings_db['notifications']['homework_1h']:
                    reminder_1h = deadline - timedelta(hours=1)
                    if reminder_1h > now_utc:
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

    # Напоминания о занятиях (если включены)
    if settings_db['notifications']['lesson_reminders']:
        for lesson in get_upcoming_lessons():
            try:
                lesson_time = datetime.fromisoformat(lesson['lesson_time']).astimezone(utc)
                student = get_user(lesson['student_id'])

                if not student:
                    continue

                # Получаем таймзону ученика
                student_tz = student.get('timezone', settings_db['timezone'])

                # За 1 час (если включено)
                if settings_db['notifications']['lesson_1h']:
                    reminder_1h = lesson_time - timedelta(hours=1)
                    if reminder_1h > now_utc:
                        topic = f" по теме: {lesson['topic']}" if lesson.get('topic') else ""
                        scheduler.add_job(
                            send_reminder,
                            'date',
                            run_date=reminder_1h,
                            args=[student['telegram_id'],
                                  f"👨‍🏫 Напоминание: занятие через 1 час{topic}\n🕐 Начало: {get_local_time(lesson['lesson_time'], student_tz)}"],
                            id=f"lesson_{lesson['id']}"
                        )
            except Exception as e:
                logger.error(f"Ошибка планирования напоминания занятия: {e}")


async def check_late_homeworks():
    """Проверка просроченных ДЗ и отправка уведомлений"""
    late_hws = get_late_homeworks()

    for hw in late_hws:
        try:
            student = get_user(hw['student_id'])
            tutor = get_user(hw['tutor_id'])

            if not student or not tutor:
                continue

            # Отмечаем как уведомленное
            hw['late_notified'] = True

            # Если включены уведомления о просрочках
            if settings_db['notifications']['late_homework_alerts']:
                # Уведомляем репетитора
                await application.bot.send_message(
                    chat_id=tutor['telegram_id'],
                    text=f"⚠️ ПРОСРОЧКА ДЗ!\n\n👤 Ученик: {student['full_name']}\n📝 {hw['task_text'][:100]}...\n📅 Был дедлайн: {get_local_time(hw['deadline'])}"
                )

            # Если включена система жизней
            if settings_db['lives_system']['enabled']:
                penalty = settings_db['lives_system']['penalty_for_late_hw']
                new_lives = update_lives(student['telegram_id'], -penalty)

                # Уведомляем репетитора о снятии жизней
                await application.bot.send_message(
                    chat_id=tutor['telegram_id'],
                    text=f"👤 {student['full_name']} потерял {penalty} жизней за просрочку ДЗ\nОсталось жизней: {new_lives}"
                )

        except Exception as e:
            logger.error(f"Ошибка обработки просроченного ДЗ: {e}")


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
Ваша таймзона: {settings_db['timezone']}
Текущее время: {get_local_time()}

Используйте /menu для управления
"""
        reply_markup = get_tutor_main_keyboard()
    else:
        role = 'student'
        welcome_text = f"""
👨‍🎓 Привет, {user.full_name}!

Я бот-помощник репетитора HelperTutor.

📊 Ваши жизни: {settings_db['lives_system']['max_lives']}/{settings_db['lives_system']['max_lives']}
🕐 Текущее время: {get_local_time()}

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

    user_tz = settings_db['timezone']
    current_time = get_local_time()

    await update.message.reply_text(
        f"📊 Панель управления репетитора\n\n"
        f"🕐 Таймзона: {user_tz}\n"
        f"⏰ Текущее время: {current_time}\n\n"
        f"👥 Учеников: {len(get_students())}\n"
        f"📚 Активных ДЗ: {len(get_active_homeworks())}\n"
        f"🗓 Занятий: {len(get_upcoming_lessons())}",
        reply_markup=get_tutor_main_keyboard()
    )


async def tutor_add_hw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать добавление ДЗ"""
    await update.callback_query.answer()

    students = get_students()
    if not students:
        await update.callback_query.edit_message_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"👤 {s['full_name']} ({s.get('lives', 0)}❤️)",
                                      callback_data=f"hw_student:{s['telegram_id']}")] for s in students]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.callback_query.edit_message_text(
        "Выберите ученика для ДЗ:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_HW_STUDENT


async def tutor_select_student_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрать ученика"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    context.user_data['selected_student'] = student_id

    await query.edit_message_text("Введите текст ДЗ:", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    return WAITING_HW_TEXT


async def tutor_hw_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст ДЗ"""
    context.user_data['hw_text'] = update.message.text

    # Получаем таймзону ученика для корректного отображения
    student_id = context.user_data['selected_student']
    student = get_user(student_id)
    student_tz = student.get('timezone', settings_db['timezone'])

    await update.message.reply_text(
        f"Введите дедлайн (ДД.ММ.ГГГГ ЧЧ:ММ)\n"
        f"Таймзона ученика: {student_tz}\n"
        f"Пример: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    return WAITING_HW_DEADLINE


async def tutor_hw_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Дедлайн ДЗ"""
    student_id = context.user_data['selected_student']
    student = get_user(student_id)
    student_tz = student.get('timezone', settings_db['timezone'])

    deadline = parse_datetime(update.message.text, student_tz)
    if not deadline:
        await update.message.reply_text("Неверный формат! Используйте ДД.ММ.ГГГГ ЧЧ:ММ\nПопробуйте снова:")
        return WAITING_HW_DEADLINE

    hw_text = context.user_data['hw_text']

    hw_id = get_next_id()
    homeworks_db.append({
        'id': hw_id,
        'student_id': student_id,
        'tutor_id': update.effective_user.id,
        'task_text': hw_text,
        'deadline': deadline.isoformat(),
        'is_completed': False,
        'late_notified': False,
        'created_at': datetime.now(utc).isoformat()
    })

    await update.message.reply_text(
        f"✅ ДЗ добавлено для {student['full_name']}!\n"
        f"📅 Дедлайн: {get_local_time(deadline.isoformat(), student_tz)}\n"
        f"⏰ По таймзоне: {student_tz}",
        reply_markup=get_tutor_main_keyboard()
    )

    context.user_data.clear()
    schedule_reminders()
    return ConversationHandler.END


async def tutor_list_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список ДЗ"""
    active = get_active_homeworks()

    if not active:
        text = "📭 Нет активных ДЗ."
    else:
        text = "📚 Активные ДЗ:\n\n"
        for hw in active[:10]:
            student = get_user(hw['student_id'])
            student_tz = student.get('timezone', settings_db['timezone']) if student else settings_db['timezone']
            text += f"👤 {student['full_name'] if student else '???'} ({student.get('lives', 0)}❤️)\n"
            text += f"📝 {hw['task_text'][:50]}...\n"
            text += f"📅 {get_local_time(hw['deadline'], student_tz)}\n"
            text += f"⏰ Таймзона: {student_tz}\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_list_students(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список учеников с жизнями"""
    students = get_students()

    if not students:
        text = "👥 Нет учеников."
    else:
        text = f"👥 Ученики ({len(students)}):\n\n"
        for s in students:
            active_hws = len(get_homeworks_for_student(s['telegram_id']))
            completed_hws = len(
                [h for h in homeworks_db if h['student_id'] == s['telegram_id'] and h.get('is_completed')])
            text += f"• {s['full_name']}\n"
            text += f"  ❤️ Жизни: {s.get('lives', 0)}/{settings_db['lives_system']['max_lives']}\n"
            text += f"  📊 ДЗ: {active_hws} активных, {completed_hws} выполнено\n"
            text += f"  🕐 Таймзона: {s.get('timezone', 'Не указана')}\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_delete_student_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать удаление ученика"""
    await update.callback_query.answer()

    students = get_students()
    if not students:
        await update.callback_query.edit_message_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"🗑 {s['full_name']}",
                                      callback_data=f"delete_student:{s['telegram_id']}")] for s in students]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.callback_query.edit_message_text(
        "Выберите ученика для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_DELETE_STUDENT


async def tutor_delete_student_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления ученика"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    student = get_user(student_id)

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete:{student_id}")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data="cancel")]
    ]

    await query.edit_message_text(
        f"⚠️ ВНИМАНИЕ! Вы собираетесь удалить ученика:\n\n"
        f"👤 {student['full_name']}\n"
        f"📱 ID: {student_id}\n"
        f"📊 Активных ДЗ: {len(get_homeworks_for_student(student_id))}\n\n"
        f"Все его данные (ДЗ, занятия) будут удалены!\n"
        f"Вы уверены?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


async def tutor_delete_student_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнить удаление ученика"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    student = get_user(student_id)

    if student:
        # Удаляем ученика
        del users_db[student_id]

        # Удаляем его ДЗ
        global homeworks_db
        homeworks_db = [h for h in homeworks_db if h['student_id'] != student_id]

        # Удаляем его занятия
        global lessons_db
        lessons_db = [l for l in lessons_db if l['student_id'] != student_id]

        await query.edit_message_text(
            f"✅ Ученик {student['full_name']} удален!\n"
            f"🗑 Удалены все связанные данные.",
            reply_markup=get_tutor_main_keyboard()
        )
    else:
        await query.edit_message_text("❌ Ученик не найден.", reply_markup=get_tutor_main_keyboard())


async def tutor_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки репетитора"""
    await update.callback_query.answer()

    keyboard = [
        [InlineKeyboardButton("🔔 Настройки уведомлений", callback_data="settings_notifications")],
        [InlineKeyboardButton("❤️ Система жизней", callback_data="settings_lives")],
        [InlineKeyboardButton("🕐 Настройки времени", callback_data="settings_time")],
        [InlineKeyboardButton("📊 Статистика", callback_data="settings_stats")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cancel")]
    ]

    await update.callback_query.edit_message_text(
        f"⚙️ Настройки репетитора\n\n"
        f"📊 Текущие настройки:\n"
        f"• 🔔 Уведомления: {'Вкл' if settings_db['notifications']['homework_reminders'] else 'Выкл'}\n"
        f"• ❤️ Система жизней: {'Вкл' if settings_db['lives_system']['enabled'] else 'Выкл'}\n"
        f"• 🕐 Таймзона: {settings_db['timezone']}\n",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SETTINGS_CHOICE


async def tutor_settings_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки уведомлений"""
    await update.callback_query.answer()

    notifications = settings_db['notifications']

    keyboard = [
        [InlineKeyboardButton(
            f"{'🔔' if notifications['homework_reminders'] else '🔕'} Уведомления о ДЗ: {'Вкл' if notifications['homework_reminders'] else 'Выкл'}",
            callback_data="toggle_hw_reminders"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if notifications['lesson_reminders'] else '🔕'} Уведомления о занятиях: {'Вкл' if notifications['lesson_reminders'] else 'Выкл'}",
            callback_data="toggle_lesson_reminders"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if notifications['late_homework_alerts'] else '🔕'} Оповещения о просрочках: {'Вкл' if notifications['late_homework_alerts'] else 'Выкл'}",
            callback_data="toggle_late_alerts"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if notifications['homework_24h'] else '🔕'} Напоминания за 24ч: {'Вкл' if notifications['homework_24h'] else 'Выкл'}",
            callback_data="toggle_hw_24h"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if notifications['homework_1h'] else '🔕'} Напоминания за 1ч: {'Вкл' if notifications['homework_1h'] else 'Выкл'}",
            callback_data="toggle_hw_1h"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if notifications['lesson_1h'] else '🔕'} Напоминания о занятиях: {'Вкл' if notifications['lesson_1h'] else 'Выкл'}",
            callback_data="toggle_lesson_1h"
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")]
    ]

    await update.callback_query.edit_message_text(
        "🔔 Настройки уведомлений:\n\n"
        "Вы можете включать/выключать различные типы уведомлений.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_NOTIFICATION_SETTINGS


async def toggle_notification_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение настройки уведомлений"""
    query = update.callback_query
    await query.answer()

    setting_map = {
        'toggle_hw_reminders': ('homework_reminders', 'Уведомления о ДЗ'),
        'toggle_lesson_reminders': ('lesson_reminders', 'Уведомления о занятиях'),
        'toggle_late_alerts': ('late_homework_alerts', 'Оповещения о просрочках'),
        'toggle_hw_24h': ('homework_24h', 'Напоминания за 24ч'),
        'toggle_hw_1h': ('homework_1h', 'Напоминания за 1ч'),
        'toggle_lesson_1h': ('lesson_1h', 'Напоминания о занятиях')
    }

    setting_key, setting_name = setting_map[query.data]
    settings_db['notifications'][setting_key] = not settings_db['notifications'][setting_key]

    new_state = 'Вкл' if settings_db['notifications'][setting_key] else 'Выкл'
    await query.answer(f"{setting_name}: {new_state}", show_alert=True)

    # Обновляем напоминания при изменении настроек
    schedule_reminders()

    # Возвращаемся к настройкам уведомлений
    await tutor_settings_notifications(update, context)


async def tutor_settings_lives(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки системы жизней"""
    await update.callback_query.answer()

    lives_settings = settings_db['lives_system']

    keyboard = [
        [InlineKeyboardButton(
            f"{'❤️' if lives_settings['enabled'] else '💔'} Система жизней: {'Вкл' if lives_settings['enabled'] else 'Выкл'}",
            callback_data="toggle_lives_system"
        )],
        [InlineKeyboardButton(
            f"🔢 Макс. жизней: {lives_settings['max_lives']}",
            callback_data="set_max_lives"
        )],
        [InlineKeyboardButton(
            f"➖ Штраф за просрочку: {lives_settings['penalty_for_late_hw']}",
            callback_data="set_penalty_late"
        )],
        [InlineKeyboardButton(
            f"➖ Штраф за пропуск занятия: {lives_settings['penalty_for_missed_lesson']}",
            callback_data="set_penalty_lesson"
        )],
        [InlineKeyboardButton(
            f"➕ Награда за раннее ДЗ: {lives_settings['reward_for_early_hw']}",
            callback_data="set_reward_early"
        )],
        [InlineKeyboardButton(
            f"🔄 Авто-сброс дней: {lives_settings['auto_reset_days']}",
            callback_data="set_reset_days"
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")]
    ]

    await update.callback_query.edit_message_text(
        "❤️ Настройки системы жизней:\n\n"
        "Система жизней мотивирует учеников выполнять задания вовремя.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_LIVES_SETTINGS


async def toggle_lives_system(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включение/выключение системы жизней"""
    query = update.callback_query
    await query.answer()

    settings_db['lives_system']['enabled'] = not settings_db['lives_system']['enabled']
    new_state = 'Вкл' if settings_db['lives_system']['enabled'] else 'Выкл'

    await query.answer(f"Система жизней: {new_state}", show_alert=True)
    await tutor_settings_lives(update, context)


async def tutor_settings_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки времени"""
    await update.callback_query.answer()

    # Создаем клавиатуру с популярными таймзонами России и СНГ
    popular_timezones = [
        'Europe/Moscow',  # Москва
        'Europe/Kaliningrad',  # Калининград
        'Europe/Samara',  # Самара
        'Asia/Yekaterinburg',  # Екатеринбург
        'Asia/Omsk',  # Омск
        'Asia/Krasnoyarsk',  # Красноярск
        'Asia/Irkutsk',  # Иркутск
        'Asia/Yakutsk',  # Якутск
        'Asia/Vladivostok',  # Владивосток
        'Europe/Kiev',  # Киев
        'Europe/Minsk',  # Минск
        'Asia/Almaty',  # Алматы
    ]

    keyboard = []
    for tz in popular_timezones:
        display_name = tz.split('/')[-1].replace('_', ' ')
        if tz == settings_db['timezone']:
            keyboard.append([InlineKeyboardButton(f"✅ {display_name}", callback_data=f"timezone:{tz}")])
        else:
            keyboard.append([InlineKeyboardButton(f"   {display_name}", callback_data=f"timezone:{tz}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")])

    current_time = get_local_time()

    await update.callback_query.edit_message_text(
        f"🕐 Настройки времени\n\n"
        f"Текущая таймзона: {settings_db['timezone']}\n"
        f"Текущее время: {current_time}\n\n"
        f"Выберите таймзону:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_TIMEZONE_SETTINGS


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка таймзоны"""
    query = update.callback_query
    await query.answer()

    new_timezone = query.data.split(':')[1]
    settings_db['timezone'] = new_timezone

    # Обновляем время для всех пользователей
    for user in users_db.values():
        if user.get('role') == 'student' and not user.get('timezone'):
            user['timezone'] = new_timezone

    current_time = get_local_time()

    await query.edit_message_text(
        f"✅ Таймзона изменена на: {new_timezone}\n"
        f"🕐 Текущее время: {current_time}",
        reply_markup=get_tutor_main_keyboard()
    )

    # Обновляем напоминания с новой таймзоной
    schedule_reminders()

    return ConversationHandler.END


async def tutor_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    await update.callback_query.answer()

    students = get_students()
    active_hws = get_active_homeworks()
    upcoming_lessons = get_upcoming_lessons()
    late_hws = get_late_homeworks()

    # Статистика по жизням
    lives_stats = {
        'full': sum(1 for s in students if s.get('lives', 0) == settings_db['lives_system']['max_lives']),
        'half': sum(1 for s in students if 0 < s.get('lives', 0) < settings_db['lives_system']['max_lives']),
        'zero': sum(1 for s in students if s.get('lives', 0) == 0),
    }

    text = f"📊 Статистика системы\n\n"
    text += f"👥 Учеников: {len(students)}\n"
    text += f"📚 Активных ДЗ: {len(active_hws)}\n"
    text += f"⚠️ Просроченных ДЗ: {len(late_hws)}\n"
    text += f"🗓 Ближайших занятий: {len(upcoming_lessons)}\n\n"

    if settings_db['lives_system']['enabled']:
        text += f"❤️ Статистика жизней:\n"
        text += f"• Полные жизни: {lives_stats['full']}\n"
        text += f"• Частичные: {lives_stats['half']}\n"
        text += f"• Нет жизней: {lives_stats['zero']}\n\n"

    text += f"🕐 Таймзона: {settings_db['timezone']}\n"
    text += f"🔔 Уведомления: {'Вкл' if settings_db['notifications']['homework_reminders'] else 'Выкл'}\n"
    text += f"❤️ Система жизней: {'Вкл' if settings_db['lives_system']['enabled'] else 'Выкл'}"

    await update.callback_query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())


async def student_hw_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ученик отмечает ДЗ"""
    user_id = update.effective_user.id
    student_hws = [h for h in homeworks_db if h['student_id'] == user_id and not h.get('is_completed')]

    if not student_hws:
        await update.callback_query.edit_message_text("📭 Нет активных ДЗ.", reply_markup=get_student_main_keyboard())
        return

    # Создаем клавиатуру с выбором ДЗ
    keyboard = []
    for hw in student_hws[:5]:  # Показываем до 5 ДЗ
        deadline = datetime.fromisoformat(hw['deadline']).astimezone(utc)
        now = datetime.now(utc)
        is_early = deadline > now

        emoji = "✅" if is_early else "⚠️"
        status = " (досрочно)" if is_early else " (с опозданием)"

        keyboard.append([InlineKeyboardButton(
            f"{emoji} {hw['task_text'][:30]}...{status}",
            callback_data=f"complete_hw:{hw['id']}"
        )])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    student = get_user(user_id)
    await update.callback_query.edit_message_text(
        f"📚 Выберите ДЗ для отметки:\n\n"
        f"❤️ Ваши жизни: {student.get('lives', 0)}/{settings_db['lives_system']['max_lives']}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def complete_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметить конкретное ДЗ как выполненное"""
    query = update.callback_query
    await query.answer()

    hw_id = int(query.data.split(':')[1])
    user_id = update.effective_user.id

    # Находим ДЗ
    hw = next((h for h in homeworks_db if h['id'] == hw_id and h['student_id'] == user_id), None)

    if not hw:
        await query.edit_message_text("❌ ДЗ не найдено.", reply_markup=get_student_main_keyboard())
        return

    # Отмечаем как выполненное
    hw['is_completed'] = True
    hw['completed_at'] = datetime.now(utc).isoformat()

    # Проверяем, было ли ДЗ сдано вовремя
    deadline = datetime.fromisoformat(hw['deadline']).astimezone(utc)
    now = datetime.now(utc)
    is_early = deadline > now

    student = get_user(user_id)
    tutor = get_user(hw['tutor_id'])

    # Начисляем/снимаем жизни
    lives_change = 0
    if settings_db['lives_system']['enabled']:
        if is_early:
            # Награда за досрочное выполнение
            reward = settings_db['lives_system']['reward_for_early_hw']
            new_lives = update_lives(user_id, reward)
            lives_change = reward
        else:
            # Штраф уже был снят при просрочке
            lives_change = 0

    # Уведомляем репетитора
    if tutor:
        time_status = "досрочно" if is_early else "с опозданием"
        await application.bot.send_message(
            chat_id=tutor['telegram_id'],
            text=f"🎉 {student['full_name']} выполнил ДЗ {time_status}!\n\n"
                 f"📝 {hw['task_text'][:100]}...\n"
                 f"{'❤️ +' + str(lives_change) if lives_change > 0 else ''}"
        )

    # Формируем ответ ученику
    response = f"✅ ДЗ отмечено как выполненное!\n\n"
    if is_early:
        response += f"🎉 Вы сдали работу досрочно!\n"
        if lives_change > 0:
            response += f"❤️ +{lives_change} жизней\n"
    else:
        response += f"⚠️ Вы сдали работу с опозданием\n"

    if student:
        response += f"\n❤️ Ваши жизни: {student.get('lives', 0)}/{settings_db['lives_system']['max_lives']}"

    await query.edit_message_text(response, reply_markup=get_student_main_keyboard())


async def student_my_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мои ДЗ"""
    user_id = update.effective_user.id
    student_hws = [h for h in homeworks_db if h['student_id'] == user_id]

    student = get_user(user_id)
    student_tz = student.get('timezone', settings_db['timezone']) if student else settings_db['timezone']

    if not student_hws:
        text = "📭 У вас нет ДЗ."
    else:
        active = [h for h in student_hws if not h.get('is_completed')]
        completed = [h for h in student_hws if h.get('is_completed')]

        text = f"📚 Ваши ДЗ\n\n"
        text += f"❤️ Ваши жизни: {student.get('lives', 0)}/{settings_db['lives_system']['max_lives']}\n\n"

        if active:
            text += "⏳ Активные:\n"
            for hw in active[:3]:
                deadline_str = get_local_time(hw['deadline'], student_tz)
                text += f"• {hw['task_text'][:40]}...\n"
                text += f"  📅 {deadline_str}\n\n"

        if completed:
            text += "✅ Выполненные:\n"
            for hw in completed[-3:]:
                completed_at = get_local_time(hw.get('completed_at'), student_tz)
                text += f"• {hw['task_text'][:40]}...\n"
                text += f"  🏁 {completed_at}\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_student_main_keyboard())


async def student_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Расписание"""
    user_id = update.effective_user.id
    student_lessons = [l for l in lessons_db if
                       l['student_id'] == user_id and l['lesson_time'] > datetime.now(utc).isoformat()]

    student = get_user(user_id)
    student_tz = student.get('timezone', settings_db['timezone']) if student else settings_db['timezone']

    if not student_lessons:
        text = "🗓 Нет предстоящих занятий."
    else:
        text = "🗓 Ваше расписание:\n\n"
        for lesson in student_lessons[:5]:
            lesson_time = get_local_time(lesson['lesson_time'], student_tz)
            text += f"📅 {lesson_time}\n"
            text += f"📌 {lesson.get('topic', 'Без темы')}\n"
            text += f"{'🔔' if lesson.get('notify_student', True) else '🔕'} Уведомления\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_student_main_keyboard())


async def student_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Профиль ученика"""
    user_id = update.effective_user.id
    student = get_user(user_id)

    if not student:
        await update.callback_query.edit_message_text("❌ Профиль не найден.", reply_markup=get_student_main_keyboard())
        return

    student_tz = student.get('timezone', settings_db['timezone'])
    active_hws = len(get_homeworks_for_student(user_id))
    completed_hws = len([h for h in homeworks_db if h['student_id'] == user_id and h.get('is_completed')])

    # Следующий сброс жизней
    next_reset = "Не настроено"
    if settings_db['lives_system']['enabled'] and student.get('last_life_reset'):
        try:
            last_reset = datetime.fromisoformat(student['last_life_reset']).astimezone(utc)
            next_reset_date = last_reset + timedelta(days=settings_db['lives_system']['auto_reset_days'])
            next_reset = get_local_time(next_reset_date.isoformat(), student_tz)
        except:
            pass

    text = f"👤 Ваш профиль\n\n"
    text += f"📝 Имя: {student['full_name']}\n"
    text += f"🆔 ID: {user_id}\n"
    text += f"🕐 Таймзона: {student_tz}\n"
    text += f"📅 Зарегистрирован: {get_local_time(student['created_at'], student_tz)}\n\n"

    text += f"📊 Статистика:\n"
    text += f"• Активных ДЗ: {active_hws}\n"
    text += f"• Выполнено ДЗ: {completed_hws}\n\n"

    if settings_db['lives_system']['enabled']:
        text += f"❤️ Система жизней:\n"
        text += f"• Текущие жизни: {student.get('lives', 0)}/{settings_db['lives_system']['max_lives']}\n"
        text += f"• Следующий сброс: {next_reset}\n\n"

    text += f"🕐 Текущее время: {get_local_time(None, student_tz)}"

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить профиль", callback_data="student_profile")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
    ]

    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    help_text = """
📚 HelperTutor - Умный бот-помощник репетитора

👨‍🏫 Для репетитора (/menu):
• 📝 Добавление ДЗ с учетом таймзоны ученика
• 👥 Управление учениками (удаление)
• ⚙️ Настройки системы (уведомления, жизни, время)
• 📊 Статистика и мониторинг

👨‍🎓 Для учеников:
• ✅ Отметка выполнения ДЗ с системой жизней
• 📚 Просмотр своих ДЗ и дедлайнов
• 🗓 Расписание занятий
• 👤 Профиль с информацией о жизнях

❤️ Система жизней:
• Жизни отнимаются за просроченные ДЗ
• Начисляются за досрочное выполнение
• Автоматически сбрасываются раз в неделю

🕐 Умное время:
• Поддержка всех таймзон
• Автоматическая конвертация времени
• Напоминания в локальном времени

🔔 Уведомления:
• Настраиваемые напоминания
• Оповещения о просрочках
• Уведомления репетитору

💡 Совет: Устанавливайте реалистичные дедлайны!
"""
    if update.message:
        await update.message.reply_text(help_text)
    else:
        await update.callback_query.edit_message_text(help_text, reply_markup=get_student_main_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена"""
    context.user_data.clear()
    user_id = update.effective_user.id
    if update.callback_query:
        if is_tutor(user_id):
            await update.callback_query.edit_message_text("❌ Действие отменено.",
                                                          reply_markup=get_tutor_main_keyboard())
        else:
            await update.callback_query.edit_message_text("❌ Действие отменено.",
                                                          reply_markup=get_student_main_keyboard())
    return ConversationHandler.END


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    user_id = update.effective_user.id
    if is_tutor(user_id):
        await menu(update, context)
    else:
        keyboard = get_student_main_keyboard()
        await update.callback_query.edit_message_text(
            "Главное меню ученика:",
            reply_markup=keyboard
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)

    if "Conflict" in str(context.error) and "getUpdates" in str(context.error):
        logger.error("Обнаружен конфликт! Возможно запущено несколько ботов.")

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
    except:
        pass


# ====================== КЛАВИАТУРЫ ======================
def get_tutor_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📝 Добавить ДЗ", callback_data='tutor_add_hw')],
        [InlineKeyboardButton("📋 Список ДЗ", callback_data='tutor_list_hw')],
        [InlineKeyboardButton("👥 Ученики", callback_data='tutor_list_students')],
        [InlineKeyboardButton("🗑 Удалить ученика", callback_data='tutor_delete_student')],
        [InlineKeyboardButton("⚙️ Настройки", callback_data='tutor_settings')],
        [InlineKeyboardButton("📊 Статистика", callback_data='tutor_stats')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_student_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ ДЗ выполнено", callback_data='student_hw_done')],
        [InlineKeyboardButton("📚 Мои ДЗ", callback_data='student_my_hw')],
        [InlineKeyboardButton("🗓 Расписание", callback_data='student_schedule')],
        [InlineKeyboardButton("👤 Мой профиль", callback_data='student_profile')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    return InlineKeyboardMarkup(keyboard)


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
            application.stop()
            application.shutdown()
            logger.info("✅ Бот остановлен")
        except:
            pass

    # Останавливаем веб-сервер
    if HAS_AIOHTTP and web_runner:
        import asyncio as async_lib
        try:
            loop = async_lib.new_event_loop()
            async_lib.set_event_loop(loop)
            loop.run_until_complete(web_runner.cleanup())
            logger.info("✅ Веб-сервер остановлен")
        except:
            pass

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

    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК HELPER TUTOR BOT v2.0")
    logger.info("=" * 50)

    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        logger.info("💡 Добавьте на Render: TELEGRAM_BOT_TOKEN = ваш_токен")
        return

    logger.info(f"✅ Токен: установлен")
    logger.info(f"✅ Репетитор ID: {TUTOR_ID if TUTOR_ID else 'не установлен'}")
    logger.info(f"✅ Порт: {PORT}")
    logger.info(f"✅ Таймзона: {settings_db['timezone']}")
    logger.info(f"✅ Система жизней: {'Включена' if settings_db['lives_system']['enabled'] else 'Выключена'}")

    # Запускаем веб-сервер для health checks
    await start_web_server()

    try:
        # Создаем приложение Telegram
        application = Application.builder().token(TOKEN).build()

        # Добавляем обработчик ошибок
        application.add_error_handler(error_handler)

        # Создаем планировщик
        scheduler = AsyncIOScheduler(timezone=timezone(settings_db['timezone']))
        scheduler.start()

        # Conversation Handler для ДЗ
        conv_hw_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(tutor_select_student_hw, pattern='^hw_student:')],
            states={
                WAITING_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_hw_text)],
                WAITING_HW_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_hw_deadline)],
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        )

        # Conversation Handler для удаления учеников
        conv_delete_student = ConversationHandler(
            entry_points=[CallbackQueryHandler(tutor_delete_student_confirm, pattern='^delete_student:')],
            states={},
            fallbacks=[
                CallbackQueryHandler(tutor_delete_student_execute, pattern='^confirm_delete:'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
        )

        # Conversation Handler для настроек
        conv_settings = ConversationHandler(
            entry_points=[CallbackQueryHandler(tutor_settings_start, pattern='^tutor_settings$')],
            states={
                WAITING_SETTINGS_CHOICE: [
                    CallbackQueryHandler(tutor_settings_notifications, pattern='^settings_notifications$'),
                    CallbackQueryHandler(tutor_settings_lives, pattern='^settings_lives$'),
                    CallbackQueryHandler(tutor_settings_time, pattern='^settings_time$'),
                    CallbackQueryHandler(back_to_main, pattern='^cancel$'),
                    CallbackQueryHandler(tutor_stats, pattern='^settings_stats$'),
                ],
                WAITING_NOTIFICATION_SETTINGS: [
                    CallbackQueryHandler(toggle_notification_setting, pattern='^toggle_.*'),
                    CallbackQueryHandler(tutor_settings_start, pattern='^settings_back$'),
                ],
                WAITING_LIVES_SETTINGS: [
                    CallbackQueryHandler(toggle_lives_system, pattern='^toggle_lives_system$'),
                    CallbackQueryHandler(tutor_settings_start, pattern='^settings_back$'),
                ],
                WAITING_TIMEZONE_SETTINGS: [
                    CallbackQueryHandler(set_timezone, pattern='^timezone:'),
                    CallbackQueryHandler(tutor_settings_start, pattern='^settings_back$'),
                ],
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern='^cancel$')],
        )

        # Регистрируем обработчики команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu))
        application.add_handler(CommandHandler("help", help_command))

        # Обработчики кнопок репетитора
        application.add_handler(CallbackQueryHandler(tutor_add_hw_start, pattern='^tutor_add_hw$'))
        application.add_handler(CallbackQueryHandler(tutor_list_hw, pattern='^tutor_list_hw$'))
        application.add_handler(CallbackQueryHandler(tutor_list_students, pattern='^tutor_list_students$'))
        application.add_handler(CallbackQueryHandler(tutor_delete_student_start, pattern='^tutor_delete_student$'))
        application.add_handler(CallbackQueryHandler(tutor_stats, pattern='^tutor_stats$'))

        # Обработчики кнопок ученика
        application.add_handler(CallbackQueryHandler(student_hw_done, pattern='^student_hw_done$'))
        application.add_handler(CallbackQueryHandler(complete_homework, pattern='^complete_hw:'))
        application.add_handler(CallbackQueryHandler(student_my_hw, pattern='^student_my_hw$'))
        application.add_handler(CallbackQueryHandler(student_schedule, pattern='^student_schedule$'))
        application.add_handler(CallbackQueryHandler(student_profile, pattern='^student_profile$'))

        # Общие обработчики
        application.add_handler(CallbackQueryHandler(help_command, pattern='^help$'))
        application.add_handler(CallbackQueryHandler(cancel, pattern='^cancel$'))
        application.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_to_main$'))

        # Conversation handlers
        application.add_handler(conv_hw_handler)
        application.add_handler(conv_delete_student)
        application.add_handler(conv_settings)

        logger.info("✅ Обработчики зарегистрированы")

        # Обновляем напоминания
        schedule_reminders()

        logger.info("🤖 Бот запускается...")

        # Запускаем бота
        await application.initialize()
        await application.start()
        await application.updater.start_polling()

        logger.info("✅ Бот успешно запущен!")
        logger.info(f"🕐 Текущее время: {get_local_time()}")
        logger.info(f"👥 Зарегистрировано учеников: {len(get_students())}")
        logger.info("👉 Напишите боту /start в Telegram")

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
            except:
                pass

        # Останавливаем планировщик
        if scheduler and scheduler.running:
            scheduler.shutdown()
            logger.info("✅ Планировщик остановлен")


def main():
    """Точка входа"""
    # Для Windows
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Регистрируем обработчики завершения
    register_shutdown_handlers()

    # Запускаем асинхронную main
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("👋 Бот завершен")
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")


if __name__ == '__main__':
    main()