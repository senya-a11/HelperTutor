import os
import sys
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ====================== НАСТРОЙКИ ======================
load_dotenv()


# Безопасное получение переменных
def safe_getenv(key, default=None):
    value = os.getenv(key, default)
    if value:
        # Очищаем от невалидных символов
        try:
            return value.encode('utf-8').decode('utf-8')
        except:
            # Оставляем только ASCII символы
            return ''.join(c for c in str(value) if ord(c) < 128)
    return value


TOKEN = safe_getenv('TELEGRAM_BOT_TOKEN')
TUTOR_ID = int(safe_getenv('TUTOR_ID', '0') or 0)

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== ХРАНИЛИЩЕ ======================
users_db = {}
homeworks_db = []


# ====================== КОМАНДЫ БОТА ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    user_id = user.id

    # Сохраняем пользователя
    users_db[user_id] = {
        'id': user_id,
        'username': user.username,
        'full_name': user.full_name,
        'is_tutor': user_id == TUTOR_ID,
        'registered_at': datetime.now().isoformat()
    }

    if user_id == TUTOR_ID:
        # Репетитор
        welcome_text = f"""
👨‍🏫 Добро пожаловать, репетитор {user.full_name}!

📊 Панель управления активна.
Используйте кнопки ниже:
"""
        reply_markup = get_tutor_keyboard()
    else:
        # Ученик
        welcome_text = f"""
👨‍🎓 Привет, {user.full_name}!

Я бот-помощник репетитора HelperTutor.

🚀 Бот готов к работе!
Используйте кнопки ниже:
"""
        reply_markup = get_student_keyboard()

    await update.message.reply_text(welcome_text, reply_markup=reply_markup)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # Репетитор
    if data.startswith('tutor_'):
        if user_id != TUTOR_ID:
            await query.edit_message_text("❌ Доступно только репетитору!")
            return

        if data == 'tutor_add_hw':
            await query.edit_message_text(
                "📝 Добавление ДЗ\n\n"
                "В разработке. Скоро будет доступно!",
                reply_markup=get_tutor_keyboard()
            )

        elif data == 'tutor_list_hw':
            text = "📚 Домашние задания\n\n"
            if homeworks_db:
                for hw in homeworks_db[-3:]:
                    status = "✅" if hw.get('completed') else "⏳"
                    text += f"{status} {hw.get('student', 'Ученик')}: {hw.get('task', 'Задание')[:30]}...\n"
            else:
                text += "Пока нет заданий"

            await query.edit_message_text(text, reply_markup=get_tutor_keyboard())

        elif data == 'tutor_students':
            students = [u for u in users_db.values() if not u.get('is_tutor')]
            text = f"👥 Ученики: {len(students)}\n\n"
            for student in students[-5:]:
                text += f"• {student['full_name']}\n"

            await query.edit_message_text(text, reply_markup=get_tutor_keyboard())

    # Ученик
    elif data.startswith('student_'):
        if data == 'student_hw_done':
            # Создаем тестовое задание
            if not homeworks_db:
                homeworks_db.append({
                    'student_id': user_id,
                    'student': users_db.get(user_id, {}).get('full_name', 'Ученик'),
                    'task': 'Первое тестовое задание',
                    'completed': False
                })

            # Помечаем как выполненное
            for hw in homeworks_db:
                if hw['student_id'] == user_id and not hw['completed']:
                    hw['completed'] = True

                    # Уведомление репетитору
                    if TUTOR_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=TUTOR_ID,
                                text=f"🎉 Ученик выполнил ДЗ!"
                            )
                        except:
                            pass
                    break

            await query.edit_message_text(
                "✅ Задание отмечено как выполненное!",
                reply_markup=get_student_keyboard()
            )

        elif data == 'student_my_hw':
            await query.edit_message_text(
                "📚 Ваши задания:\n\n"
                "1. Тестовое задание - В процессе\n"
                "2. Новое задание - Скоро\n\n"
                "Нажмите '✅ ДЗ выполнено' когда закончите.",
                reply_markup=get_student_keyboard()
            )

        elif data == 'student_schedule':
            await query.edit_message_text(
                "🗓 Расписание:\n\n"
                "Понедельник: 14:00-15:30\n"
                "Среда: 15:00-16:30\n"
                "Пятница: 13:00-14:30\n\n"
                "Бот напомнит за 30 минут.",
                reply_markup=get_student_keyboard()
            )

    # Помощь
    elif data == 'help':
        await query.edit_message_text(
            "❓ Помощь\n\n"
            "/start - начать\n"
            "Кнопки - управление\n\n"
            "Бот в активной разработке.",
            reply_markup=get_student_keyboard() if user_id != TUTOR_ID else get_tutor_keyboard()
        )


def get_tutor_keyboard():
    """Клавиатура для репетитора"""
    keyboard = [
        [InlineKeyboardButton("📝 Добавить ДЗ", callback_data='tutor_add_hw')],
        [InlineKeyboardButton("📋 Список ДЗ", callback_data='tutor_list_hw')],
        [InlineKeyboardButton("👥 Ученики", callback_data='tutor_students')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_student_keyboard():
    """Клавиатура для ученика"""
    keyboard = [
        [InlineKeyboardButton("✅ ДЗ выполнено", callback_data='student_hw_done')],
        [InlineKeyboardButton("📚 Мои задания", callback_data='student_my_hw')],
        [InlineKeyboardButton("🗓 Расписание", callback_data='student_schedule')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    return InlineKeyboardMarkup(keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    await start(update, context)


# ====================== ЗАПУСК БОТА (ИСПРАВЛЕННЫЙ) ======================
def main():
    """Главная функция (синхронная)"""
    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК HELPER TUTOR BOT")
    logger.info("=" * 50)

    # Проверка токена
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
        logger.info("💡 Как получить токен:")
        logger.info("1. Найдите @BotFather в Telegram")
        logger.info("2. Отправьте /newbot")
        logger.info("3. Следуйте инструкциям")
        logger.info("4. Скопируйте токен")
        logger.info("5. На Render: TELEGRAM_BOT_TOKEN = ваш_токен")
        return

    logger.info(f"✅ Токен: установлен ({len(TOKEN)} символов)")
    logger.info(f"✅ Репетитор ID: {TUTOR_ID if TUTOR_ID else 'не установлен'}")

    # Для Windows нужно настроить event loop policy
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        # Создаем приложение
        app = Application.builder().token(TOKEN).build()

        # Регистрируем обработчики
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CallbackQueryHandler(button_handler))

        # Обработчик текста
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            lambda update, ctx: update.message.reply_text(
                "Используйте /start",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Начать", callback_data='start')]
                ])
            )
        ))

        logger.info("✅ Обработчики зарегистрированы")
        logger.info("🤖 Бот запускается...")

        # Запускаем бота
        app.run_polling()

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")


if __name__ == '__main__':
    main()  # Только main() без asyncio.run()