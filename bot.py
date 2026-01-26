import os
import sys
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv
from pytz import timezone, utc
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ====================== НАСТРОЙКИ ======================
load_dotenv()

# Состояния для ConversationHandler
WAITING_HW_STUDENT, WAITING_HW_TEXT, WAITING_HW_DEADLINE = range(3)
WAITING_LESSON_STUDENT, WAITING_LESSON_TOPIC, WAITING_LESSON_DATE, WAITING_LESSON_HOUR, WAITING_LESSON_MINUTE = range(3,
                                                                                                                      8)
WAITING_SETTINGS_CHOICE, WAITING_NOTIFICATION_SETTINGS, WAITING_LIVES_SETTINGS, WAITING_TIMEZONE_SETTINGS = range(8, 12)
WAITING_DELETE_STUDENT = 12


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

# ====================== ХРАНИЛИЩЕ ======================
users_db = {}
homeworks_db = []
lessons_db = []
next_id = 1

# Настройки
settings = {
    'timezone': TIMEZONE,
    'notifications': {
        'homework_reminders': True,
        'lesson_reminders': True,
        'late_homework_alerts': True,
        'homework_times': [24, 12, 2],
        'lesson_times': [24, 2]
    },
    'lives': {
        'enabled': True,
        'max_lives': 5,
        'penalty_late': 1,
        'penalty_lesson': 2,
        'reward_early': 1,
        'auto_reset_days': 7,
        'show_to_student': True
    }
}


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def get_next_id():
    global next_id
    next_id += 1
    return next_id - 1


def get_user(telegram_id):
    return users_db.get(telegram_id)


def register_user(telegram_id, username, full_name, role='student'):
    if telegram_id not in users_db:
        users_db[telegram_id] = {
            'id': telegram_id,
            'telegram_id': telegram_id,
            'username': username or '',
            'full_name': full_name,
            'role': role,
            'created_at': datetime.now().isoformat(),
            'lives': settings['lives']['max_lives'],
            'last_life_reset': datetime.now().isoformat(),
            'timezone': settings['timezone']
        }
        return True
    return False


def is_tutor(telegram_id):
    user = get_user(telegram_id)
    if user:
        return user['role'] == 'tutor'
    return telegram_id == TUTOR_ID


def get_local_time(dt_str=None, user_tz=None):
    try:
        if dt_str is None:
            dt = datetime.now(utc)
        else:
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))

        tz = timezone(user_tz or settings['timezone'])
        local_dt = dt.astimezone(tz)
        return local_dt.strftime('%d.%m.%Y %H:%M')
    except Exception as e:
        logger.error(f"Ошибка конвертации времени: {e}")
        return datetime.now().strftime('%d.%m.%Y %H:%M')


def parse_datetime(dt_str, user_tz=None):
    try:
        dt = datetime.strptime(dt_str, '%d.%m.%Y %H:%M')

        tz = timezone(user_tz or settings['timezone'])
        dt = tz.localize(dt)

        dt_utc = dt.astimezone(utc)
        return dt_utc
    except ValueError:
        try:
            dt = datetime.strptime(dt_str, '%d.%m.%Y')
            tz = timezone(user_tz or settings['timezone'])
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
    return [h for h in homeworks_db if
            h['deadline'] < now_utc and not h.get('is_completed') and not h.get('late_notified')]


def get_upcoming_lessons():
    now_utc = datetime.now(utc).isoformat()
    return [l for l in lessons_db if l['lesson_time'] > now_utc]


def update_lives(student_id, delta, reason=""):
    student = get_user(student_id)
    if student and settings['lives']['enabled']:
        current_lives = student.get('lives', settings['lives']['max_lives'])
        new_lives = max(0, min(current_lives + delta, settings['lives']['max_lives']))
        student['lives'] = new_lives

        if delta != 0 and settings['lives']['show_to_student']:
            try:
                asyncio.create_task(
                    application.bot.send_message(
                        chat_id=student_id,
                        text=f"{'❤️' if delta > 0 else '💔'} {reason}\nОсталось жизней: {new_lives}/{settings['lives']['max_lives']}"
                    )
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления о жизнях: {e}")

        return new_lives
    return None


def check_and_reset_lives():
    now = datetime.now(utc)
    for user in users_db.values():
        if user.get('role') == 'student':
            last_reset_str = user.get('last_life_reset')
            if last_reset_str:
                try:
                    last_reset = datetime.fromisoformat(last_reset_str.replace('Z', '+00:00'))
                    days_passed = (now - last_reset).days
                    if days_passed >= settings['lives']['auto_reset_days']:
                        user['lives'] = settings['lives']['max_lives']
                        user['last_life_reset'] = now.isoformat()

                        try:
                            asyncio.create_task(
                                application.bot.send_message(
                                    chat_id=user['telegram_id'],
                                    text=f"🎉 Жизни сброшены! Теперь у вас {settings['lives']['max_lives']}❤️"
                                )
                            )
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления о сбросе: {e}")
                except Exception as e:
                    logger.error(f"Ошибка обработки сброса жизней: {e}")


# ====================== НАПОМИНАНИЯ ======================
def schedule_reminders():
    if not scheduler:
        return

    scheduler.remove_all_jobs()
    now_utc = datetime.now(utc)

    scheduler.add_job(check_late_homeworks, 'interval', hours=6, id='check_late_homeworks')
    scheduler.add_job(check_and_reset_lives, 'interval', hours=24, id='reset_lives_check')

    if settings['notifications']['homework_reminders']:
        for hw in get_active_homeworks():
            try:
                deadline = datetime.fromisoformat(hw['deadline'].replace('Z', '+00:00'))
                student = get_user(hw['student_id'])

                if not student:
                    continue

                for hours_before in settings['notifications']['homework_times']:
                    reminder_time = deadline - timedelta(hours=hours_before)
                    if reminder_time > now_utc:
                        scheduler.add_job(
                            send_reminder,
                            'date',
                            run_date=reminder_time,
                            args=[student['telegram_id'],
                                  f"⏰ Напоминание: ДЗ через {hours_before} {'час' if hours_before == 1 else 'часа' if 2 <= hours_before <= 4 else 'часов'}!\n"
                                  f"📝 {hw['task_text'][:50]}...\n"
                                  f"📅 Дедлайн: {get_local_time(hw['deadline'], student.get('timezone'))}"],
                            id=f"hw_{hours_before}h_{hw['id']}"
                        )
            except Exception as e:
                logger.error(f"Ошибка планирования напоминания ДЗ: {e}")

    if settings['notifications']['lesson_reminders']:
        for lesson in get_upcoming_lessons():
            try:
                lesson_time = datetime.fromisoformat(lesson['lesson_time'].replace('Z', '+00:00'))
                student = get_user(lesson['student_id'])

                if not student or not lesson.get('notify_student', True):
                    continue

                for hours_before in settings['notifications']['lesson_times']:
                    reminder_time = lesson_time - timedelta(hours=hours_before)
                    if reminder_time > now_utc:
                        scheduler.add_job(
                            send_reminder,
                            'date',
                            run_date=reminder_time,
                            args=[student['telegram_id'],
                                  f"👨‍🏫 Напоминание: занятие через {hours_before} {'час' if hours_before == 1 else 'часа' if 2 <= hours_before <= 4 else 'часов'}!\n"
                                  f"📌 Тема: {lesson.get('topic', 'Без темы')}\n"
                                  f"🕐 Начало: {get_local_time(lesson['lesson_time'], student.get('timezone'))}"],
                            id=f"lesson_{hours_before}h_{lesson['id']}"
                        )
            except Exception as e:
                logger.error(f"Ошибка планирования напоминания занятия: {e}")


async def check_late_homeworks():
    late_hws = get_late_homeworks()

    for hw in late_hws:
        try:
            student = get_user(hw['student_id'])
            tutor = get_user(hw['tutor_id'])

            if not student or not tutor:
                continue

            hw['late_notified'] = True

            if settings['notifications']['late_homework_alerts']:
                await application.bot.send_message(
                    chat_id=tutor['telegram_id'],
                    text=f"⚠️ ПРОСРОЧКА ДЗ!\n\n"
                         f"👤 Ученик: {student['full_name']}\n"
                         f"📝 {hw['task_text'][:100]}...\n"
                         f"📅 Был дедлайн: {get_local_time(hw['deadline'], student.get('timezone'))}"
                )

            if settings['lives']['enabled']:
                penalty = settings['lives']['penalty_late']
                new_lives = update_lives(student['telegram_id'], -penalty, f"Снято {penalty}❤️ за просрочку ДЗ")

                await application.bot.send_message(
                    chat_id=tutor['telegram_id'],
                    text=f"👤 {student['full_name']} потерял {penalty}❤️ за просрочку ДЗ\n"
                         f"Осталось жизней: {new_lives}/{settings['lives']['max_lives']}"
                )

        except Exception as e:
            logger.error(f"Ошибка обработки просроченного ДЗ: {e}")


async def send_reminder(chat_id, message):
    try:
        if application:
            await application.bot.send_message(chat_id=chat_id, text=message)
    except Exception as e:
        logger.error(f"Ошибка отправки напоминания: {e}")


# ====================== ОБРАБОТЧИК ОШИБОК ======================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.")
    except:
        pass


# ====================== КОМАНДЫ ДЛЯ РЕПЕТИТОРА ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    user_id = user.id

    # Очищаем контекст пользователя
    context.user_data.clear()

    if is_tutor(user_id):
        role = 'tutor'
        welcome_text = f"""
👨‍🏫 Добро пожаловать, репетитор {user.full_name}!

Ваш ID: {user.id}
Таймзона: {settings['timezone']}
Текущее время: {get_local_time()}

Используйте /menu для управления
"""
        reply_markup = get_tutor_main_keyboard()
    else:
        role = 'student'
        student = get_user(user_id)
        lives_text = f"❤️ Ваши жизни: {student.get('lives', settings['lives']['max_lives'])}/{settings['lives']['max_lives']}" if student and \
                                                                                                                                  settings[
                                                                                                                                      'lives'][
                                                                                                                                      'enabled'] else ""

        welcome_text = f"""
👨‍🎓 Привет, {user.full_name}!

Я бот-помощник репетитора HelperTutor.

{lives_text}
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
    """Команда /menu - меню репетитора"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    # Очищаем контекст пользователя
    context.user_data.clear()

    await update.message.reply_text(
        f"📊 Панель управления репетитора\n\n"
        f"🕐 Таймзона: {settings['timezone']}\n"
        f"⏰ Текущее время: {get_local_time()}\n\n"
        f"👥 Учеников: {len(get_students())}\n"
        f"📚 Активных ДЗ: {len(get_active_homeworks())}\n"
        f"🗓 Занятий: {len(get_upcoming_lessons())}",
        reply_markup=get_tutor_main_keyboard()
    )


async def add_hw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add_hw - добавить ДЗ"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    # Очищаем контекст перед началом
    context.user_data.clear()

    await tutor_add_hw_start(update, context)


async def add_lesson_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add_lesson - добавить занятие"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()
    await tutor_add_lesson_start(update, context)


async def list_hw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /list_hw - список ДЗ"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    # Очищаем контекст
    context.user_data.clear()

    # Вызываем напрямую логику списка ДЗ
    active = get_active_homeworks()

    if not active:
        text = "📭 Нет активных ДЗ."
    else:
        text = "📚 Активные ДЗ:\n\n"
        for hw in active[:10]:
            student = get_user(hw['student_id'])
            student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']
            text += f"👤 {student['full_name'] if student else '???'} ({student.get('lives', 0)}❤️)\n"
            text += f"📝 {hw['task_text'][:50]}...\n"
            text += f"📅 {get_local_time(hw['deadline'], student_tz)}\n"
            text += f"⏰ Таймзона: {student_tz}\n\n"

    await update.message.reply_text(text, reply_markup=get_tutor_main_keyboard())


async def list_students_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /students - список учеников"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()

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
            text += f"  ❤️ Жизни: {s.get('lives', 0)}/{settings['lives']['max_lives']}\n"
            text += f"  📊 ДЗ: {active_hws} активных, {completed_hws} выполнено\n"
            text += f"  🕐 Таймзона: {s.get('timezone', 'Не указана')}\n\n"

    await update.message.reply_text(text, reply_markup=get_tutor_main_keyboard())


async def delete_student_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /delete_student - удалить ученика"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()
    await tutor_delete_student_start(update, context)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /settings - настройки"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("🔔 Уведомления", callback_data="settings_notifications")],
        [InlineKeyboardButton("❤️ Жизни", callback_data="settings_lives")],
        [InlineKeyboardButton("🕐 Время", callback_data="settings_time")],
        [InlineKeyboardButton("📊 Статистика", callback_data="settings_stats")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cancel")]
    ]

    await update.message.reply_text(
        f"⚙️ Настройки\n\n"
        f"Таймзона: {settings['timezone']}\n"
        f"Уведомления: {'✅' if settings['notifications']['homework_reminders'] else '❌'}\n"
        f"Жизни: {'✅' if settings['lives']['enabled'] else '❌'}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats - статистика"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()

    students = get_students()
    active_hws = get_active_homeworks()
    upcoming_lessons = get_upcoming_lessons()
    late_hws = get_late_homeworks()

    lives_stats = {
        'full': sum(1 for s in students if s.get('lives', 0) == settings['lives']['max_lives']),
        'half': sum(1 for s in students if 0 < s.get('lives', 0) < settings['lives']['max_lives']),
        'zero': sum(1 for s in students if s.get('lives', 0) == 0),
    }

    text = f"📊 Статистика\n\n"
    text += f"👥 Учеников: {len(students)}\n"
    text += f"📚 Активных ДЗ: {len(active_hws)}\n"
    text += f"⚠️ Просроченных: {len(late_hws)}\n"
    text += f"🗓 Занятий: {len(upcoming_lessons)}\n\n"

    if settings['lives']['enabled']:
        text += f"❤️ Жизни:\n"
        text += f"• Полные: {lives_stats['full']}\n"
        text += f"• Частичные: {lives_stats['half']}\n"
        text += f"• Нет: {lives_stats['zero']}\n\n"

    text += f"🕐 Таймзона: {settings['timezone']}"

    await update.message.reply_text(text, reply_markup=get_tutor_main_keyboard())


async def reset_lives_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /reset_lives - сбросить жизни всем ученикам"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()

    students = get_students()
    for student in students:
        student['lives'] = settings['lives']['max_lives']
        student['last_life_reset'] = datetime.now(utc).isoformat()

    await update.message.reply_text(
        f"✅ Жизни сброшены для всех {len(students)} учеников!",
        reply_markup=get_tutor_main_keyboard()
    )


async def clear_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /clear_all - очистить все данные (осторожно!)"""
    if not is_tutor(update.effective_user.id):
        await update.message.reply_text("Доступно только репетитору!")
        return

    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("✅ Да, очистить всё", callback_data="clear_all_confirm")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data="cancel")]
    ]

    await update.message.reply_text(
        "⚠️ ВНИМАНИЕ! Вы собираетесь очистить ВСЕ данные:\n\n"
        f"👥 Учеников: {len(get_students())}\n"
        f"📚 ДЗ: {len(homeworks_db)}\n"
        f"🗓 Занятий: {len(lessons_db)}\n\n"
        "Это действие НЕОБРАТИМО! Вы уверены?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def clear_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение очистки всех данных"""
    query = update.callback_query
    await query.answer()

    global users_db, homeworks_db, lessons_db, next_id

    students_count = len(get_students())
    hw_count = len(homeworks_db)
    lessons_count = len(lessons_db)

    # Сохраняем только репетитора
    tutor_id = TUTOR_ID
    tutor_data = None
    for user_id, user_data in list(users_db.items()):
        if user_id == tutor_id:
            tutor_data = user_data
            break

    # Очищаем все данные
    users_db.clear()
    homeworks_db.clear()
    lessons_db.clear()
    next_id = 1

    # Восстанавливаем репетитора
    if tutor_data:
        users_db[tutor_id] = tutor_data

    await query.edit_message_text(
        f"✅ Все данные очищены!\n\n"
        f"🗑 Удалено:\n"
        f"• Учеников: {students_count}\n"
        f"• ДЗ: {hw_count}\n"
        f"• Занятий: {lessons_count}\n\n"
        f"Репетитор сохранён.",
        reply_markup=get_tutor_main_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help - помощь"""
    context.user_data.clear()

    if is_tutor(update.effective_user.id):
        help_text = """
📚 HelperTutor - Умный бот-помощник репетитора

👨‍🏫 Команды репетитора:
/start - Начать работу
/menu - Панель управления
/add_hw - Добавить ДЗ
/add_lesson - Добавить занятие
/list_hw - Список ДЗ
/students - Список учеников
/delete_student - Удалить ученика
/settings - Настройки
/stats - Статистика
/reset_lives - Сбросить жизни всем
/clear_all - Очистить все данные (осторожно!)
/help - Эта справка

📝 Управление через кнопки:
• Добавление ДЗ и занятий
• Просмотр статистики
• Настройки уведомлений
• Управление учениками

❤️ Система жизней:
• Настройка штрафов и наград
• Авто-сброс по расписанию
• Уведомления ученикам

🕐 Умное время:
• Поддержка всех таймзон
• Автоматическая конвертация
"""
    else:
        help_text = """
👨‍🎓 Команды ученика:
/start - Начать работу
/help - Эта справка

📝 Управление через кнопки:
• Отметка выполнения ДЗ
• Просмотр своих ДЗ
• Расписание занятий
• Профиль ученика

❤️ Система жизней:
• Жизни отнимаются за просрочки
• Начисляются за досрочное выполнение
• Автоматический сброс

🔔 Уведомления:
• Напоминания о ДЗ
• Уведомления о занятиях
"""

    if update.message:
        await update.message.reply_text(help_text)
    else:
        await update.callback_query.edit_message_text(help_text)


# ====================== КОЛБЭКИ ДЛЯ КНОПОК ======================
async def tutor_add_hw_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Добавить ДЗ"""
    await update.callback_query.answer()

    # Очищаем контекст перед началом
    context.user_data.clear()

    await tutor_add_hw_start(update, context)


async def tutor_add_hw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск добавления ДЗ"""
    students = get_students()
    if not students:
        if update.callback_query:
            await update.callback_query.edit_message_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        else:
            await update.message.reply_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"👤 {s['full_name']} ({s.get('lives', 0)}❤️)",
                                      callback_data=f"hw_student:{s['telegram_id']}")]
                for s in students]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "Выберите ученика для ДЗ:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "Выберите ученика для ДЗ:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return WAITING_HW_STUDENT


async def tutor_add_lesson_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Добавить занятие"""
    await update.callback_query.answer()

    context.user_data.clear()
    await tutor_add_lesson_start(update, context)


async def tutor_add_lesson_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск добавления занятия"""
    students = get_students()
    if not students:
        if update.callback_query:
            await update.callback_query.edit_message_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        else:
            await update.message.reply_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"👤 {s['full_name']}", callback_data=f"lesson_student:{s['telegram_id']}")]
                for s in students]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "Выберите ученика для занятия:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "Выберите ученика для занятия:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return WAITING_LESSON_STUDENT


async def tutor_list_hw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Список ДЗ"""
    await update.callback_query.answer()

    context.user_data.clear()

    active = get_active_homeworks()

    if not active:
        text = "📭 Нет активных ДЗ."
    else:
        text = "📚 Активные ДЗ:\n\n"
        for hw in active[:10]:
            student = get_user(hw['student_id'])
            student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']
            text += f"👤 {student['full_name'] if student else '???'} ({student.get('lives', 0)}❤️)\n"
            text += f"📝 {hw['task_text'][:50]}...\n"
            text += f"📅 {get_local_time(hw['deadline'], student_tz)}\n"
            text += f"⏰ Таймзона: {student_tz}\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_list_students_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Список учеников"""
    await update.callback_query.answer()

    context.user_data.clear()

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
            text += f"  ❤️ Жизни: {s.get('lives', 0)}/{settings['lives']['max_lives']}\n"
            text += f"  📊 ДЗ: {active_hws} активных, {completed_hws} выполнено\n"
            text += f"  🕐 Таймзона: {s.get('timezone', 'Не указана')}\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())


async def tutor_delete_student_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Удалить ученика"""
    await update.callback_query.answer()

    context.user_data.clear()
    await tutor_delete_student_start(update, context)


async def tutor_delete_student_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск удаления ученика"""
    students = get_students()
    if not students:
        if update.callback_query:
            await update.callback_query.edit_message_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        else:
            await update.message.reply_text("Нет учеников.", reply_markup=get_tutor_main_keyboard())
        return

    keyboard = [[InlineKeyboardButton(f"🗑 {s['full_name']}", callback_data=f"delete_student:{s['telegram_id']}")]
                for s in students]
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "Выберите ученика для удаления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "Выберите ученика для удаления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return WAITING_DELETE_STUDENT


async def tutor_settings_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Настройки"""
    await update.callback_query.answer()

    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("🔔 Уведомления", callback_data="settings_notifications")],
        [InlineKeyboardButton("❤️ Жизни", callback_data="settings_lives")],
        [InlineKeyboardButton("🕐 Время", callback_data="settings_time")],
        [InlineKeyboardButton("📊 Статистика", callback_data="settings_stats")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cancel")]
    ]

    await update.callback_query.edit_message_text(
        f"⚙️ Настройки\n\n"
        f"Таймзона: {settings['timezone']}\n"
        f"Уведомления: {'✅' if settings['notifications']['homework_reminders'] else '❌'}\n"
        f"Жизни: {'✅' if settings['lives']['enabled'] else '❌'}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SETTINGS_CHOICE


async def tutor_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Статистика"""
    await update.callback_query.answer()

    context.user_data.clear()

    students = get_students()
    active_hws = get_active_homeworks()
    upcoming_lessons = get_upcoming_lessons()
    late_hws = get_late_homeworks()

    lives_stats = {
        'full': sum(1 for s in students if s.get('lives', 0) == settings['lives']['max_lives']),
        'half': sum(1 for s in students if 0 < s.get('lives', 0) < settings['lives']['max_lives']),
        'zero': sum(1 for s in students if s.get('lives', 0) == 0),
    }

    text = f"📊 Статистика\n\n"
    text += f"👥 Учеников: {len(students)}\n"
    text += f"📚 Активных ДЗ: {len(active_hws)}\n"
    text += f"⚠️ Просроченных: {len(late_hws)}\n"
    text += f"🗓 Занятий: {len(upcoming_lessons)}\n\n"

    if settings['lives']['enabled']:
        text += f"❤️ Жизни:\n"
        text += f"• Полные: {lives_stats['full']}\n"
        text += f"• Частичные: {lives_stats['half']}\n"
        text += f"• Нет: {lives_stats['zero']}\n\n"

    text += f"🕐 Таймзона: {settings['timezone']}"

    await update.callback_query.edit_message_text(text, reply_markup=get_tutor_main_keyboard())


# ====================== ДОБАВЛЕНИЕ ДЗ ======================
async def tutor_select_student_hw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор ученика для ДЗ"""
    query = update.callback_query
    await query.answer()

    # Очищаем контекст пользователя
    context.user_data.clear()

    student_id = int(query.data.split(':')[1])
    context.user_data['selected_student'] = student_id

    await query.edit_message_text(
        "Введите текст ДЗ:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    )
    return WAITING_HW_TEXT


async def tutor_hw_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод текста ДЗ"""
    context.user_data['hw_text'] = update.message.text

    student_id = context.user_data['selected_student']
    student = get_user(student_id)
    student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']

    await update.message.reply_text(
        f"Введите дедлайн (ДД.ММ.ГГГГ ЧЧ:ММ)\n"
        f"Таймзона ученика: {student_tz}\n"
        f"Пример: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    return WAITING_HW_DEADLINE


async def tutor_hw_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод дедлайна ДЗ"""
    student_id = context.user_data['selected_student']
    student = get_user(student_id)
    student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']

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

    if student:
        try:
            await application.bot.send_message(
                chat_id=student_id,
                text=f"📚 Новое домашнее задание!\n\n"
                     f"📝 {hw_text[:200]}...\n"
                     f"📅 Дедлайн: {get_local_time(deadline.isoformat(), student_tz)}\n"
                     f"⏰ Таймзона: {student_tz}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления ученику: {e}")

    await update.message.reply_text(
        f"✅ ДЗ добавлено для {student['full_name'] if student else 'ученика'}!\n"
        f"📅 Дедлайн: {get_local_time(deadline.isoformat(), student_tz)}\n"
        f"⏰ По таймзоне: {student_tz}",
        reply_markup=get_tutor_main_keyboard()
    )

    context.user_data.clear()
    schedule_reminders()
    return ConversationHandler.END


# ====================== ДОБАВЛЕНИЕ ЗАНЯТИЯ ======================
async def tutor_select_student_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор ученика для занятия"""
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    student_id = int(query.data.split(':')[1])
    context.user_data['selected_student'] = student_id

    await query.edit_message_text(
        "Введите тему занятия:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    )
    return WAITING_LESSON_TOPIC


async def tutor_lesson_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод темы занятия"""
    context.user_data['lesson_topic'] = update.message.text

    today = datetime.now()
    keyboard = []

    for i in range(7):
        date = today + timedelta(days=i)
        date_str = date.strftime('%d.%m.%Y')
        weekday = date.strftime('%A')
        if i == 0:
            display = f"{date_str} (сегодня)"
        elif i == 1:
            display = f"{date_str} (завтра)"
        elif i == 2:
            display = f"{date_str} (послезавтра)"
        else:
            display = f"{date_str} ({weekday})"
        keyboard.append([InlineKeyboardButton(display, callback_data=f"lesson_date:{date_str}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.message.reply_text(
        "Выберите дату занятия:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_LESSON_DATE


async def tutor_lesson_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор даты занятия"""
    query = update.callback_query
    await query.answer()

    date_str = query.data.split(':')[1]
    context.user_data['lesson_date'] = date_str

    keyboard = []
    row = []
    for hour in range(8, 22):
        row.append(InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"lesson_hour:{hour}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await query.edit_message_text(
        f"Выберите время начала занятия ({date_str}):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_LESSON_HOUR


async def tutor_lesson_hour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор часа занятия"""
    query = update.callback_query
    await query.answer()

    hour = int(query.data.split(':')[1])
    context.user_data['lesson_hour'] = hour

    keyboard = []
    row = []
    for minute in [0, 15, 30, 45]:
        row.append(InlineKeyboardButton(f"{hour:02d}:{minute:02d}", callback_data=f"lesson_minute:{minute}"))
    keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await query.edit_message_text(
        "Выберите минуты:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_LESSON_MINUTE


async def tutor_lesson_minute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор минут занятия"""
    query = update.callback_query
    await query.answer()

    minute = int(query.data.split(':')[1])

    student_id = context.user_data['selected_student']
    topic = context.user_data['lesson_topic']
    date_str = context.user_data['lesson_date']
    hour = context.user_data['lesson_hour']

    student = get_user(student_id)
    student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']

    dt_str = f"{date_str} {hour:02d}:{minute:02d}"
    lesson_time = parse_datetime(dt_str, student_tz)

    if not lesson_time:
        await query.edit_message_text("Ошибка при создании времени занятия.", reply_markup=get_tutor_main_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    lesson_id = get_next_id()
    lessons_db.append({
        'id': lesson_id,
        'student_id': student_id,
        'tutor_id': update.effective_user.id,
        'topic': topic,
        'lesson_time': lesson_time.isoformat(),
        'duration_minutes': 60,
        'notify_student': True,
        'created_at': datetime.now(utc).isoformat()
    })

    if student:
        try:
            await application.bot.send_message(
                chat_id=student_id,
                text=f"📅 Новое занятие!\n\n"
                     f"📌 Тема: {topic}\n"
                     f"🕐 Время: {get_local_time(lesson_time.isoformat(), student_tz)}\n"
                     f"⏰ Таймзона: {student_tz}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления ученику: {e}")

    await query.edit_message_text(
        f"✅ Занятие добавлено!\n\n"
        f"👤 Ученик: {student['full_name'] if student else '???'}\n"
        f"📌 Тема: {topic}\n"
        f"🕐 Время: {get_local_time(lesson_time.isoformat(), student_tz)}\n"
        f"⏰ Таймзона: {student_tz}",
        reply_markup=get_tutor_main_keyboard()
    )

    context.user_data.clear()
    schedule_reminders()
    return ConversationHandler.END


# ====================== НАСТРОЙКИ ======================
async def tutor_settings_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки уведомлений"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(
            f"{'🔔' if settings['notifications']['homework_reminders'] else '🔕'} ДЗ",
            callback_data="toggle_hw_reminders"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if settings['notifications']['lesson_reminders'] else '🔕'} Занятия",
            callback_data="toggle_lesson_reminders"
        )],
        [InlineKeyboardButton(
            f"{'🔔' if settings['notifications']['late_homework_alerts'] else '🔕'} Просрочки",
            callback_data="toggle_late_alerts"
        )],
        [InlineKeyboardButton(
            "⏰ Время ДЗ",
            callback_data="hw_notification_times"
        )],
        [InlineKeyboardButton(
            "⏰ Время занятий",
            callback_data="lesson_notification_times"
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")]
    ]

    await query.edit_message_text(
        "🔔 Настройки уведомлений:",
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
        'toggle_late_alerts': ('late_homework_alerts', 'Оповещения о просрочках')
    }

    setting_key, setting_name = setting_map[query.data]
    settings['notifications'][setting_key] = not settings['notifications'][setting_key]

    new_state = '✅' if settings['notifications'][setting_key] else '❌'
    await query.answer(f"{setting_name}: {new_state}")

    schedule_reminders()
    await tutor_settings_notifications(update, context)


async def hw_notification_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка времени уведомлений о ДЗ"""
    query = update.callback_query
    await query.answer()

    times = [2, 12, 24]

    keyboard = []
    for time in times:
        is_active = time in settings['notifications']['homework_times']
        emoji = "✅" if is_active else "☑️"
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {time}ч",
                callback_data=f"toggle_hw_time:{time}"
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_notifications")])

    await query.edit_message_text(
        "📚 Уведомления о ДЗ:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def lesson_notification_times(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка времени уведомлений о занятиях"""
    query = update.callback_query
    await query.answer()

    times = [2, 12, 24]

    keyboard = []
    for time in times:
        is_active = time in settings['notifications']['lesson_times']
        emoji = "✅" if is_active else "☑️"
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {time}ч",
                callback_data=f"toggle_lesson_time:{time}"
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_notifications")])

    await query.edit_message_text(
        "🗓 Уведомления о занятиях:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def toggle_notification_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение времени уведомления"""
    query = update.callback_query
    await query.answer()

    data = query.data.split(':')
    time_type = data[0]
    hours = int(data[1])

    if time_type == "toggle_hw_time":
        if hours in settings['notifications']['homework_times']:
            settings['notifications']['homework_times'].remove(hours)
        else:
            settings['notifications']['homework_times'].append(hours)
            settings['notifications']['homework_times'].sort()
    elif time_type == "toggle_lesson_time":
        if hours in settings['notifications']['lesson_times']:
            settings['notifications']['lesson_times'].remove(hours)
        else:
            settings['notifications']['lesson_times'].append(hours)
            settings['notifications']['lesson_times'].sort()

    await query.answer(
        f"Напоминание за {hours}ч: {'✅' if hours in settings['notifications']['homework_times'] else '❌'}")

    schedule_reminders()

    if time_type == "toggle_hw_time":
        await hw_notification_times(update, context)
    else:
        await lesson_notification_times(update, context)


# ====================== НАСТРОЙКИ ЖИЗНЕЙ ======================
async def tutor_settings_lives(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки системы жизней"""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(
            f"{'❤️' if settings['lives']['enabled'] else '💔'} Система",
            callback_data="toggle_lives_system"
        )],
        [InlineKeyboardButton(
            f"🔢 Макс: {settings['lives']['max_lives']}",
            callback_data="set_max_lives"
        )],
        [InlineKeyboardButton(
            f"➖ Просрочка: {settings['lives']['penalty_late']}",
            callback_data="set_penalty_late"
        )],
        [InlineKeyboardButton(
            f"➖ Занятие: {settings['lives']['penalty_lesson']}",
            callback_data="set_penalty_lesson"
        )],
        [InlineKeyboardButton(
            f"➕ Досрочно: {settings['lives']['reward_early']}",
            callback_data="set_reward_early"
        )],
        [InlineKeyboardButton(
            f"🔄 Сброс: {settings['lives']['auto_reset_days']}д",
            callback_data="set_reset_days"
        )],
        [InlineKeyboardButton(
            f"{'👁️' if settings['lives']['show_to_student'] else '🙈'} Показ",
            callback_data="toggle_show_lives"
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")]
    ]

    await query.edit_message_text(
        "❤️ Настройки жизней:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_LIVES_SETTINGS


async def toggle_lives_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение настройки системы жизней"""
    query = update.callback_query
    await query.answer()

    if query.data == "toggle_lives_system":
        settings['lives']['enabled'] = not settings['lives']['enabled']
        new_state = '✅' if settings['lives']['enabled'] else '❌'
        await query.answer(f"Система жизней: {new_state}")
    elif query.data == "toggle_show_lives":
        settings['lives']['show_to_student'] = not settings['lives']['show_to_student']
        new_state = '✅' if settings['lives']['show_to_student'] else '❌'
        await query.answer(f"Показывать жизни: {new_state}")

    await tutor_settings_lives(update, context)


async def set_lives_value_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало установки значения для системы жизней"""
    query = update.callback_query
    await query.answer()

    setting_map = {
        'set_max_lives': ('max_lives', 'Максимальное количество жизней'),
        'set_penalty_late': ('penalty_late', 'Штраф за просрочку ДЗ'),
        'set_penalty_lesson': ('penalty_lesson', 'Штраф за пропуск занятия'),
        'set_reward_early': ('reward_early', 'Награда за раннее ДЗ'),
        'set_reset_days': ('auto_reset_days', 'Дней до авто-сброса')
    }

    setting_key, setting_name = setting_map[query.data]
    current_value = settings['lives'][setting_key]

    context.user_data['setting_to_change'] = setting_key
    context.user_data['setting_name'] = setting_name

    await query.edit_message_text(
        f"Введите новое значение для '{setting_name}':\n"
        f"Текущее: {current_value}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="settings_lives")]])
    )
    return WAITING_LIVES_SETTINGS


async def set_lives_value_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение значения системы жизней"""
    try:
        new_value = int(update.message.text)

        if new_value < 0:
            await update.message.reply_text("❌ Не может быть отрицательным. Попробуйте снова:")
            return WAITING_LIVES_SETTINGS

        setting_key = context.user_data['setting_to_change']
        settings['lives'][setting_key] = new_value

        if setting_key == 'max_lives':
            for user in users_db.values():
                if user.get('role') == 'student':
                    user['lives'] = min(user.get('lives', new_value), new_value)

        await update.message.reply_text(
            f"✅ Сохранено: {new_value}",
            reply_markup=get_tutor_main_keyboard()
        )

        context.user_data.clear()
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❌ Введите целое число:")
        return WAITING_LIVES_SETTINGS


# ====================== НАСТРОЙКИ ВРЕМЕНИ ======================
async def tutor_settings_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки времени"""
    query = update.callback_query
    await query.answer()

    popular_timezones = [
        'Europe/Moscow', 'Europe/Kaliningrad',
        'Asia/Yekaterinburg', 'Asia/Omsk',
        'Asia/Vladivostok', 'Europe/Kiev',
        'Europe/Minsk', 'Asia/Almaty'
    ]

    keyboard = []
    for tz in popular_timezones:
        display_name = tz.split('/')[-1].replace('_', ' ')
        if tz == settings['timezone']:
            keyboard.append([InlineKeyboardButton(f"✅ {display_name}", callback_data=f"timezone:{tz}")])
        else:
            keyboard.append([InlineKeyboardButton(f"{display_name}", callback_data=f"timezone:{tz}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")])

    await query.edit_message_text(
        f"🕐 Настройки времени\n\n"
        f"Текущая: {settings['timezone']}\n"
        f"Время: {get_local_time()}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_TIMEZONE_SETTINGS


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка таймзоны"""
    query = update.callback_query
    await query.answer()

    new_timezone = query.data.split(':')[1]
    settings['timezone'] = new_timezone

    for user in users_db.values():
        if user.get('role') == 'student' and not user.get('timezone'):
            user['timezone'] = new_timezone

    await query.edit_message_text(
        f"✅ Таймзона: {new_timezone}\n"
        f"🕐 Время: {get_local_time()}",
        reply_markup=get_tutor_main_keyboard()
    )

    schedule_reminders()
    return ConversationHandler.END


# ====================== ОБРАБОТЧИКИ УДАЛЕНИЯ ======================
async def tutor_delete_student_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления ученика"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    student = get_user(student_id)

    if not student:
        await query.edit_message_text("❌ Ученик не найден.", reply_markup=get_tutor_main_keyboard())
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("✅ Да", callback_data=f"confirm_delete:{student_id}")],
        [InlineKeyboardButton("❌ Нет", callback_data="cancel")]
    ]

    await query.edit_message_text(
        f"⚠️ Удалить ученика?\n\n"
        f"👤 {student['full_name']}\n"
        f"📊 ДЗ: {len(get_homeworks_for_student(student_id))}\n\n"
        f"Все данные будут удалены!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def tutor_delete_student_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнить удаление ученика"""
    query = update.callback_query
    await query.answer()

    student_id = int(query.data.split(':')[1])
    student = get_user(student_id)

    if student:
        del users_db[student_id]

        global homeworks_db
        homeworks_db = [h for h in homeworks_db if h['student_id'] != student_id]

        global lessons_db
        lessons_db = [l for l in lessons_db if l['student_id'] != student_id]

        await query.edit_message_text(
            f"✅ Ученик {student['full_name']} удален!",
            reply_markup=get_tutor_main_keyboard()
        )
    else:
        await query.edit_message_text("❌ Ученик не найден.", reply_markup=get_tutor_main_keyboard())

    return ConversationHandler.END


# ====================== КОМАНДЫ УЧЕНИКА ======================
async def student_hw_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: ДЗ выполнено"""
    await update.callback_query.answer()

    context.user_data.clear()

    user_id = update.effective_user.id
    student_hws = [h for h in homeworks_db if h['student_id'] == user_id and not h.get('is_completed')]

    if not student_hws:
        await update.callback_query.edit_message_text("📭 Нет активных ДЗ.", reply_markup=get_student_main_keyboard())
        return

    keyboard = []
    for hw in student_hws[:5]:
        deadline = datetime.fromisoformat(hw['deadline'].replace('Z', '+00:00'))
        now = datetime.now(utc)
        is_early = deadline > now

        emoji = "✅" if is_early else "⚠️"
        status = " (досрочно)" if is_early else " (просрочено)"

        keyboard.append([InlineKeyboardButton(
            f"{emoji} {hw['task_text'][:30]}...{status}",
            callback_data=f"complete_hw:{hw['id']}"
        )])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    student = get_user(user_id)
    lives_text = f"\n❤️ Жизни: {student.get('lives', 0)}/{settings['lives']['max_lives']}" if settings['lives'][
        'enabled'] else ""

    await update.callback_query.edit_message_text(
        f"📚 Выберите ДЗ:{lives_text}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def complete_homework(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отметить ДЗ как выполненное"""
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    hw_id = int(query.data.split(':')[1])
    user_id = update.effective_user.id

    hw = next((h for h in homeworks_db if h['id'] == hw_id and h['student_id'] == user_id), None)

    if not hw:
        await query.edit_message_text("❌ ДЗ не найдено.", reply_markup=get_student_main_keyboard())
        return

    hw['is_completed'] = True
    hw['completed_at'] = datetime.now(utc).isoformat()

    deadline = datetime.fromisoformat(hw['deadline'].replace('Z', '+00:00'))
    now = datetime.now(utc)
    is_early = deadline > now

    student = get_user(user_id)
    tutor = get_user(hw['tutor_id'])

    lives_change = 0
    if settings['lives']['enabled']:
        if is_early:
            reward = settings['lives']['reward_early']
            if reward > 0:
                new_lives = update_lives(user_id, reward, f"Начислено {reward}❤️ за досрочное выполнение")
                lives_change = reward

    if tutor:
        time_status = "досрочно" if is_early else "с опозданием"
        message = f"🎉 {student['full_name']} выполнил ДЗ {time_status}!\n\n📝 {hw['task_text'][:100]}..."
        if lives_change > 0:
            message += f"\n❤️ +{lives_change} жизней"

        await application.bot.send_message(chat_id=tutor['telegram_id'], text=message)

    response = "✅ ДЗ выполнено!\n\n"
    if is_early:
        response += "🎉 Вы сдали досрочно!\n"
        if lives_change > 0:
            response += f"❤️ +{lives_change} жизней\n"
    else:
        response += "⚠️ Вы сдали с опозданием\n"

    if student and settings['lives']['enabled']:
        response += f"\n❤️ Жизни: {student.get('lives', 0)}/{settings['lives']['max_lives']}"

    await query.edit_message_text(response, reply_markup=get_student_main_keyboard())


async def student_my_hw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Мои ДЗ"""
    await update.callback_query.answer()

    context.user_data.clear()

    user_id = update.effective_user.id
    student_hws = [h for h in homeworks_db if h['student_id'] == user_id]

    student = get_user(user_id)
    student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']

    if not student_hws:
        text = "📭 У вас нет ДЗ."
    else:
        active = [h for h in student_hws if not h.get('is_completed')]
        completed = [h for h in student_hws if h.get('is_completed')]

        text = f"📚 Ваши ДЗ\n\n"
        if settings['lives']['enabled']:
            text += f"❤️ Жизни: {student.get('lives', 0)}/{settings['lives']['max_lives']}\n\n"

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


async def student_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Расписание"""
    await update.callback_query.answer()

    context.user_data.clear()

    user_id = update.effective_user.id
    student_lessons = [l for l in lessons_db
                       if l['student_id'] == user_id and l['lesson_time'] > datetime.now(utc).isoformat()]

    student = get_user(user_id)
    student_tz = student.get('timezone', settings['timezone']) if student else settings['timezone']

    if not student_lessons:
        text = "🗓 Нет занятий."
    else:
        text = "🗓 Расписание:\n\n"
        for lesson in student_lessons[:5]:
            lesson_time = get_local_time(lesson['lesson_time'], student_tz)
            text += f"📅 {lesson_time}\n"
            text += f"📌 {lesson.get('topic', 'Без темы')}\n\n"

    await update.callback_query.edit_message_text(text, reply_markup=get_student_main_keyboard())


async def student_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка: Профиль"""
    await update.callback_query.answer()

    context.user_data.clear()

    user_id = update.effective_user.id
    student = get_user(user_id)

    if not student:
        await update.callback_query.edit_message_text("❌ Профиль не найден.", reply_markup=get_student_main_keyboard())
        return

    active_hws = len(get_homeworks_for_student(user_id))
    completed_hws = len([h for h in homeworks_db if h['student_id'] == user_id and h.get('is_completed')])

    next_reset = "Не настроено"
    if settings['lives']['enabled'] and student.get('last_life_reset'):
        try:
            last_reset = datetime.fromisoformat(student['last_life_reset'].replace('Z', '+00:00'))
            next_reset_date = last_reset + timedelta(days=settings['lives']['auto_reset_days'])
            next_reset = get_local_time(next_reset_date.isoformat(), student.get('timezone'))
        except:
            pass

    text = f"👤 Профиль\n\n"
    text += f"📝 {student['full_name']}\n"
    text += f"🕐 Таймзона: {student.get('timezone', settings['timezone'])}\n\n"

    text += f"📊 Статистика:\n"
    text += f"• Активных ДЗ: {active_hws}\n"
    text += f"• Выполнено: {completed_hws}\n\n"

    if settings['lives']['enabled']:
        text += f"❤️ Жизни:\n"
        text += f"• Текущие: {student.get('lives', 0)}/{settings['lives']['max_lives']}\n"
        text += f"• След. сброс: {next_reset}\n\n"

    text += f"🕐 Время: {get_local_time(None, student.get('timezone'))}"

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="student_profile")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
    ]

    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ====================== ОБРАБОТЧИКИ ОТМЕНЫ ======================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена"""
    context.user_data.clear()
    user_id = update.effective_user.id

    if update.callback_query:
        await update.callback_query.answer()
        if is_tutor(user_id):
            await update.callback_query.edit_message_text(
                "❌ Отменено",
                reply_markup=get_tutor_main_keyboard()
            )
        else:
            await update.callback_query.edit_message_text(
                "❌ Отменено",
                reply_markup=get_student_main_keyboard()
            )
    elif update.message:
        if is_tutor(user_id):
            await update.message.reply_text(
                "❌ Отменено",
                reply_markup=get_tutor_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "❌ Отменено",
                reply_markup=get_student_main_keyboard()
            )

    return ConversationHandler.END


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    user_id = update.effective_user.id
    context.user_data.clear()

    if update.callback_query:
        await update.callback_query.answer()
        if is_tutor(user_id):
            await update.callback_query.edit_message_text(
                "📊 Панель управления:",
                reply_markup=get_tutor_main_keyboard()
            )
        else:
            await update.callback_query.edit_message_text(
                "Главное меню:",
                reply_markup=get_student_main_keyboard()
            )
    elif update.message:
        if is_tutor(user_id):
            await update.message.reply_text(
                "📊 Панель управления:",
                reply_markup=get_tutor_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "Главное меню:",
                reply_markup=get_student_main_keyboard()
            )

    return ConversationHandler.END


async def settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назад в меню настроек"""
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("🔔 Уведомления", callback_data="settings_notifications")],
        [InlineKeyboardButton("❤️ Жизни", callback_data="settings_lives")],
        [InlineKeyboardButton("🕐 Время", callback_data="settings_time")],
        [InlineKeyboardButton("📊 Статистика", callback_data="settings_stats")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cancel")]
    ]

    await query.edit_message_text(
        f"⚙️ Настройки\n\n"
        f"Таймзона: {settings['timezone']}\n"
        f"Уведомления: {'✅' if settings['notifications']['homework_reminders'] else '❌'}\n"
        f"Жизни: {'✅' if settings['lives']['enabled'] else '❌'}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_SETTINGS_CHOICE


# ====================== КЛАВИАТУРЫ ======================
def get_tutor_main_keyboard():
    """Клавиатура репетитора"""
    keyboard = [
        [
            InlineKeyboardButton("📝 ДЗ", callback_data='tutor_add_hw'),
            InlineKeyboardButton("🗓 Занятие", callback_data='tutor_add_lesson')
        ],
        [
            InlineKeyboardButton("📋 Список", callback_data='tutor_list_hw'),
            InlineKeyboardButton("👥 Ученики", callback_data='tutor_list_students')
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data='tutor_settings'),
            InlineKeyboardButton("📊 Статистика", callback_data='tutor_stats')
        ],
        [
            InlineKeyboardButton("❌ Удалить", callback_data='tutor_delete_student'),
            InlineKeyboardButton("❓ Помощь", callback_data='help')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_student_main_keyboard():
    """Клавиатура ученика"""
    keyboard = [
        [
            InlineKeyboardButton("✅ ДЗ", callback_data='student_hw_done'),
            InlineKeyboardButton("📚 Мои ДЗ", callback_data='student_my_hw')
        ],
        [
            InlineKeyboardButton("🗓 Расписание", callback_data='student_schedule'),
            InlineKeyboardButton("👤 Профиль", callback_data='student_profile')
        ],
        [
            InlineKeyboardButton("❓ Помощь", callback_data='help')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ====================== ОСНОВНАЯ ФУНКЦИЯ ======================
async def main_async():
    global application, scheduler

    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК HELPER TUTOR BOT")
    logger.info("=" * 50)

    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        return

    logger.info(f"✅ Токен: установлен")
    logger.info(f"✅ Репетитор ID: {TUTOR_ID if TUTOR_ID else 'не установлен'}")
    logger.info(f"✅ Таймзона: {settings['timezone']}")

    try:
        application = Application.builder().token(TOKEN).build()
        application.add_error_handler(error_handler)

        scheduler = AsyncIOScheduler(timezone=timezone(settings['timezone']))
        scheduler.start()

        # Conversation Handler для ДЗ
        conv_hw_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(tutor_add_hw_start, pattern='^tutor_add_hw$'),
                CallbackQueryHandler(tutor_select_student_hw, pattern='^hw_student:')
            ],
            states={
                WAITING_HW_STUDENT: [
                    CallbackQueryHandler(tutor_select_student_hw, pattern='^hw_student:')
                ],
                WAITING_HW_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_hw_text)
                ],
                WAITING_HW_DEADLINE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_hw_deadline)
                ],
            },
            fallbacks=[
                CallbackQueryHandler(cancel, pattern='^cancel$'),
                CommandHandler('cancel', cancel)
            ],
            allow_reentry=True  # Разрешаем повторный вход
        )

        # Conversation Handler для занятий
        conv_lesson_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(tutor_add_lesson_start, pattern='^tutor_add_lesson$'),
                CallbackQueryHandler(tutor_select_student_lesson, pattern='^lesson_student:')
            ],
            states={
                WAITING_LESSON_STUDENT: [
                    CallbackQueryHandler(tutor_select_student_lesson, pattern='^lesson_student:')
                ],
                WAITING_LESSON_TOPIC: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, tutor_lesson_topic)
                ],
                WAITING_LESSON_DATE: [
                    CallbackQueryHandler(tutor_lesson_date, pattern='^lesson_date:')
                ],
                WAITING_LESSON_HOUR: [
                    CallbackQueryHandler(tutor_lesson_hour, pattern='^lesson_hour:')
                ],
                WAITING_LESSON_MINUTE: [
                    CallbackQueryHandler(tutor_lesson_minute, pattern='^lesson_minute:')
                ],
            },
            fallbacks=[
                CallbackQueryHandler(cancel, pattern='^cancel$'),
                CommandHandler('cancel', cancel)
            ],
            allow_reentry=True
        )

        # Conversation Handler для удаления учеников
        conv_delete_student = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(tutor_delete_student_start, pattern='^tutor_delete_student$'),
                CallbackQueryHandler(tutor_delete_student_confirm, pattern='^delete_student:')
            ],
            states={},
            fallbacks=[
                CallbackQueryHandler(tutor_delete_student_execute, pattern='^confirm_delete:'),
                CallbackQueryHandler(cancel, pattern='^cancel$')
            ],
            allow_reentry=True
        )

        # Conversation Handler для настроек
        conv_settings = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(tutor_settings_start_callback, pattern='^tutor_settings$'),
                CallbackQueryHandler(tutor_settings_notifications, pattern='^settings_notifications$'),
                CallbackQueryHandler(tutor_settings_lives, pattern='^settings_lives$'),
                CallbackQueryHandler(tutor_settings_time, pattern='^settings_time$')
            ],
            states={
                WAITING_SETTINGS_CHOICE: [
                    CallbackQueryHandler(tutor_settings_notifications, pattern='^settings_notifications$'),
                    CallbackQueryHandler(tutor_settings_lives, pattern='^settings_lives$'),
                    CallbackQueryHandler(tutor_settings_time, pattern='^settings_time$'),
                    CallbackQueryHandler(back_to_main, pattern='^cancel$'),
                    CallbackQueryHandler(tutor_stats_callback, pattern='^settings_stats$'),
                ],
                WAITING_NOTIFICATION_SETTINGS: [
                    CallbackQueryHandler(toggle_notification_setting,
                                         pattern='^toggle_(hw_reminders|lesson_reminders|late_alerts)$'),
                    CallbackQueryHandler(hw_notification_times, pattern='^hw_notification_times$'),
                    CallbackQueryHandler(lesson_notification_times, pattern='^lesson_notification_times$'),
                    CallbackQueryHandler(toggle_notification_time, pattern='^toggle_(hw|lesson)_time:'),
                    CallbackQueryHandler(settings_back, pattern='^settings_back$'),
                ],
                WAITING_LIVES_SETTINGS: [
                    CallbackQueryHandler(toggle_lives_setting, pattern='^toggle_(lives_system|show_lives)$'),
                    CallbackQueryHandler(set_lives_value_start,
                                         pattern='^set_(max_lives|penalty_late|penalty_lesson|reward_early|reset_days)$'),
                    CallbackQueryHandler(settings_back, pattern='^settings_back$'),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, set_lives_value_save),
                ],
                WAITING_TIMEZONE_SETTINGS: [
                    CallbackQueryHandler(set_timezone, pattern='^timezone:'),
                    CallbackQueryHandler(settings_back, pattern='^settings_back$'),
                ],
            },
            fallbacks=[
                CallbackQueryHandler(cancel, pattern='^cancel$'),
                CommandHandler('cancel', cancel)
            ],
            allow_reentry=True
        )

        # Команды репетитора
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu))
        application.add_handler(CommandHandler("add_hw", add_hw_command))
        application.add_handler(CommandHandler("add_lesson", add_lesson_command))
        application.add_handler(CommandHandler("list_hw", list_hw_command))
        application.add_handler(CommandHandler("students", list_students_command))
        application.add_handler(CommandHandler("delete_student", delete_student_command))
        application.add_handler(CommandHandler("settings", settings_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CommandHandler("reset_lives", reset_lives_command))
        application.add_handler(CommandHandler("clear_all", clear_all_command))
        application.add_handler(CommandHandler("help", help_command))

        # Кнопки репетитора
        application.add_handler(CallbackQueryHandler(tutor_list_hw_callback, pattern='^tutor_list_hw$'))
        application.add_handler(CallbackQueryHandler(tutor_list_students_callback, pattern='^tutor_list_students$'))
        application.add_handler(CallbackQueryHandler(tutor_stats_callback, pattern='^tutor_stats$'))

        # Кнопки ученика
        application.add_handler(CallbackQueryHandler(student_hw_done_callback, pattern='^student_hw_done$'))
        application.add_handler(CallbackQueryHandler(complete_homework, pattern='^complete_hw:'))
        application.add_handler(CallbackQueryHandler(student_my_hw_callback, pattern='^student_my_hw$'))
        application.add_handler(CallbackQueryHandler(student_schedule_callback, pattern='^student_schedule$'))
        application.add_handler(CallbackQueryHandler(student_profile_callback, pattern='^student_profile$'))

        # Общие кнопки
        application.add_handler(CallbackQueryHandler(help_command, pattern='^help$'))
        application.add_handler(CallbackQueryHandler(clear_all_confirm, pattern='^clear_all_confirm$'))
        application.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_to_main$'))

        # Conversation handlers
        application.add_handler(conv_hw_handler)
        application.add_handler(conv_lesson_handler)
        application.add_handler(conv_delete_student)
        application.add_handler(conv_settings)

        logger.info("✅ Обработчики зарегистрированы")

        schedule_reminders()

        logger.info("🤖 Бот запускается...")

        await application.initialize()
        await application.start()
        await application.updater.start_polling()

        logger.info("✅ Бот успешно запущен!")
        logger.info(f"🕐 Текущее время: {get_local_time()}")
        logger.info(f"👥 Зарегистрировано учеников: {len(get_students())}")
        logger.info("👉 Напишите боту /start в Telegram")

        while True:
            await asyncio.sleep(3600)

    except asyncio.CancelledError:
        logger.info("🛑 Получен сигнал отмены")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
    finally:
        if application:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
                logger.info("✅ Бот остановлен")
            except:
                pass

        if scheduler and scheduler.running:
            scheduler.shutdown()
            logger.info("✅ Планировщик остановлен")


def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("👋 Бот завершен")
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")


if __name__ == '__main__':
    main()