import os
import time
import json
from datetime import datetime
import asyncio
from functools import wraps
import html

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# -- НАСТРОЙКИ --
TELEGRAM_BOT_TOKEN = "5989400727:AAFGCBxOkUfLc_qsjLJ97l45twvw0TOCvw4"
BOT_ADMIN_CHAT_ID = 652406317

# -- ИМЕНА ФАЙЛОВ --
PIZZERIAS_FILE = 'ID пиццерий.txt'
REVIEWS_DATA_FILE = 'reviews_data.json'
USERS_FILE = 'users.json'
SUBSCRIPTIONS_FILE = 'subscriptions.json'
LAST_SENT_REVIEWS_FILE = 'last_sent_reviews.json'
# ИЗМЕНЕНО: Бот теперь работает с .xlsx файлом
LABOUR_COST_REPORT_FILE = 'labour_cost_report.xlsx'
REQUEST_FILE = 'report_request.json'

# -- Глобальные переменные --
pizzerias_map = {}
GET_START_DATE, GET_END_DATE = range(2)


# --- ОСНОВНАЯ ЛОГИКА БОТА ---

def load_data(filename, default={}):
    if not os.path.exists(filename): return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default


def save_data(filename, data):
    with open(filename, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)


def load_pizzerias():
    global pizzerias_map
    pizzerias_map = {}
    try:
        with open(PIZZERIAS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if ' - ' in line:
                    p_id, p_name = line.strip().split(' - ', 1)
                    pizzerias_map[p_id.lower()] = p_name
        print(f"[Бот] Загружено {len(pizzerias_map)} пиццерий в память.")
    except FileNotFoundError:
        print(f"[Бот][Ошибка] Файл '{PIZZERIAS_FILE}' не найден. Запустите 'data_fetcher.py'.")


def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != BOT_ADMIN_CHAT_ID:
            await update.message.reply_text("У вас нет прав для выполнения этой команды.")
            return
        await func(update, context, *args, **kwargs)

    return wrapped


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(user.id)
    users = load_data(USERS_FILE)
    if chat_id not in users:
        users[chat_id] = {"first_name": user.first_name, "username": user.username, "is_blocked": False}
        save_data(USERS_FILE, users)
    await update.message.reply_text("Привет! Я бот для Dodo. Доступные команды в меню.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subscriptions = load_data(SUBSCRIPTIONS_FILE, {})
    user_subs = subscriptions.get(chat_id, [])

    if not pizzerias_map:
        await update.message.reply_text("Список пиццерий еще не загружен. Попробуйте через минуту.")
        return

    buttons = []
    for pid, name in sorted(pizzerias_map.items(), key=lambda item: item[1]):
        text = f"{'✅ ' if pid in user_subs else ''}{name}"
        buttons.append([InlineKeyboardButton(text, callback_data=f"sub_{pid}")])

    await update.message.reply_text('Выберите пиццерии для подписки/отписки:',
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def my_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subscriptions = load_data(SUBSCRIPTIONS_FILE, {})
    user_subs = subscriptions.get(chat_id, [])

    if not user_subs:
        await update.message.reply_text("У вас нет активных подписок. Используйте /subscribe.")
        return

    message = "📄 Ваши подписки:\n\n"
    sorted_subs = sorted([pid for pid in user_subs], key=lambda pid: pizzerias_map.get(pid, ''))
    for pid in sorted_subs:
        pizzeria_name = pizzerias_map.get(pid, f"Неизвестная пиццерия ({pid})")
        message += f"• {pizzeria_name}\n"

    await update.message.reply_text(message)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, pizzeria_id = query.data.split('_', 1)
    chat_id = str(query.message.chat_id)

    subscriptions = load_data(SUBSCRIPTIONS_FILE, {})
    user_subs = subscriptions.get(chat_id, [])
    pizzeria_name = pizzerias_map.get(pizzeria_id, "Неизвестная пиццерия")

    if action == 'sub':
        if pizzeria_id in user_subs:
            user_subs.remove(pizzeria_id)
            await context.bot.send_message(chat_id=chat_id, text=f"Вы отписались от '{pizzeria_name}'.")
        else:
            user_subs.append(pizzeria_id)
            await context.bot.send_message(chat_id=chat_id, text=f"Вы подписались на '{pizzeria_name}'.")
            await send_last_review(context, chat_id, pizzeria_id)

        subscriptions[chat_id] = user_subs
        save_data(SUBSCRIPTIONS_FILE, subscriptions)

        new_user_subs = subscriptions.get(chat_id, [])
        buttons = []
        for pid, name in sorted(pizzerias_map.items(), key=lambda item: item[1]):
            text = f"{'✅ ' if pid in new_user_subs else ''}{name}"
            buttons.append([InlineKeyboardButton(text, callback_data=f"sub_{pid}")])

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def get_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request_data = load_data(REQUEST_FILE)
    status = request_data.get('status')

    if status == 'completed' or status == 'notified':
        try:
            if os.path.exists(LABOUR_COST_REPORT_FILE) and os.path.getsize(LABOUR_COST_REPORT_FILE) > 0:
                mod_time_unix = os.path.getmtime(LABOUR_COST_REPORT_FILE)
                mod_time_str = datetime.fromtimestamp(mod_time_unix).strftime('%d.%m.%Y в %H:%M')
                caption = f"📄 Последний отчет по Labour Cost (сформирован {mod_time_str})."
                await update.message.reply_document(document=open(LABOUR_COST_REPORT_FILE, 'rb'), caption=caption)
            else:
                await update.message.reply_text("✅ Отчет сгенерирован, но он пуст (данные за период не найдены).")
        except FileNotFoundError:
            await update.message.reply_text("❗️ Отчет был сгенерирован, но файл отчета не найден.")
    elif status == 'processing':
        await update.message.reply_text(
            "⏳ Отчет все еще в процессе генерации. Я пришлю его, как только он будет готов.")
    elif status == 'error':
        error_msg = request_data.get('error_message', 'Неизвестная ошибка.')
        await update.message.reply_text(f"❌ При генерации последнего отчета произошла ошибка:\n\n`{error_msg}`",
                                        parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "Не найдено активных или завершенных запросов на отчет. Создайте новый с помощью /labourcost.")


async def labour_cost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    request_data = load_data(REQUEST_FILE)
    if request_data.get('status') in ['pending', 'processing']:
        await update.message.reply_text("⏳ Предыдущий отчет еще в процессе генерации. Пожалуйста, подождите.")
        return ConversationHandler.END

    await update.message.reply_text("Введите начальную дату для отчета (ДД.ММ.ГГГГ).")
    return GET_START_DATE


async def get_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data['start_date'] = datetime.strptime(update.message.text, "%d.%m.%Y")
        await update.message.reply_text("Отлично. Теперь введите конечную дату (ДД.ММ.ГГГГ).")
        return GET_END_DATE
    except ValueError:
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ.ГГГГ.")
        return GET_START_DATE


async def get_end_date_and_request_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        end_date = datetime.strptime(update.message.text, "%d.%m.%Y")
        start_date = context.user_data['start_date']

        request_data = {
            "status": "pending",
            "requested_by": update.effective_user.id,
            "start_date_iso": start_date.strftime('%Y-%m-%dT00:00:00'),
            "end_date_iso": end_date.strftime('%Y-%m-%dT23:59:59'),
        }
        save_data(REQUEST_FILE, request_data)

        await update.message.reply_text(
            "✅ Запрос на формирование отчета отправлен.\n\n"
            "Как только отчет будет готов, я пришлю его вам в этот чат."
        )

    except (ValueError, KeyError):
        await update.message.reply_text("Произошла ошибка. Попробуйте начать заново с /labourcost.")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Действие отменено.")
    context.user_data.clear()
    return ConversationHandler.END


def format_review(review):
    pizzeria_name = pizzerias_map.get(review.get('unitId', '').lower(), f"Неизвестная ({review.get('unitId')})")
    order_date_str = review.get('orderCreatedAt')
    if order_date_str:
        order_date = datetime.fromisoformat(order_date_str).strftime('%d.%m.%Y %H:%M')
    else:
        order_date = "Неизвестно"

    rate = review.get('orderRate', 0)
    rating_stars = "★" * rate + "☆" * (5 - rate)
    comment_text = review.get('feedbackComment')

    message_lines = [f"🍕 <b>Новый отзыв: {pizzeria_name}</b>", "",
                     f"📝 <b>Заказ:</b> №{review.get('orderNumber')} от {order_date}",
                     f"⭐ <b>Оценка:</b> {rating_stars}", "", "💬 <b>Комментарий:</b>"]
    if comment_text:
        message_lines.append(html.escape(comment_text))
    else:
        message_lines.append("<i>(без комментария)</i>")
    return "\n".join(message_lines)


async def send_last_review(context: ContextTypes.DEFAULT_TYPE, chat_id: str, pizzeria_id: str):
    reviews = load_data(REVIEWS_DATA_FILE, [])
    last_review = next((r for r in reversed(reviews) if r.get('unitId', '').lower() == pizzeria_id.lower()), None)
    if last_review:
        message = "Последний отзыв по вашей новой подписке:\n\n" + format_review(last_review)
        await context.bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')
    else:
        await context.bot.send_message(chat_id=chat_id, text="По этой пиццерии пока нет отзывов.")


@admin_only
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_data(USERS_FILE)
    if not users:
        await update.message.reply_text("Список пользователей пуст.")
        return
    message = "👥 Список пользователей:\n\n"
    for chat_id, user_data in users.items():
        status = "❌ Заблокирован" if user_data.get('is_blocked') else "✅ Активен"
        message += f"<b>{user_data.get('first_name', '')}</b> (<code>{chat_id}</code>) - {status}\n"
    await update.message.reply_text(message, parse_mode='HTML')


@admin_only
async def block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_block = context.args[0]
        users = load_data(USERS_FILE)
        if user_id_to_block in users:
            users[user_id_to_block]['is_blocked'] = True
            save_data(USERS_FILE, users)
            await update.message.reply_text(f"Пользователь {user_id_to_block} заблокирован.")
        else:
            await update.message.reply_text("Пользователь не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /block <ID пользователя>")


@admin_only
async def unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id_to_unblock = context.args[0]
        users = load_data(USERS_FILE)
        if user_id_to_unblock in users:
            users[user_id_to_unblock]['is_blocked'] = False
            save_data(USERS_FILE, users)
            await update.message.reply_text(f"Пользователь {user_id_to_unblock} разблокирован.")
        else:
            await update.message.reply_text("Пользователь не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /unblock <ID пользователя>")


# -- ФОНОВЫЕ ЗАДАЧИ БОТА --
async def check_reviews_periodically(context: ContextTypes.DEFAULT_TYPE):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Проверяем новые отзывы...")
    try:
        current_reviews = load_data(REVIEWS_DATA_FILE, [])
        last_sent_reviews = load_data(LAST_SENT_REVIEWS_FILE, [])
        last_sent_ids = {f"{r.get('orderId')}_{r.get('orderRate')}" for r in last_sent_reviews}
        new_reviews = [r for r in current_reviews if f"{r.get('orderId')}_{r.get('orderRate')}" not in last_sent_ids]

        if not new_reviews:
            print("[Отзывы] Новых отзывов не найдено.")
            return

        print(f"[Отзывы] Найдено {len(new_reviews)} новых отзывов. Рассылка...")
        subscriptions = load_data(SUBSCRIPTIONS_FILE, {})
        users = load_data(USERS_FILE, {})

        for review in new_reviews:
            pizzeria_id = review.get('unitId', '').lower()
            message = format_review(review)
            for chat_id, user_subs in subscriptions.items():
                if not users.get(chat_id, {}).get('is_blocked') and pizzeria_id in user_subs:
                    try:
                        await context.bot.send_message(chat_id=int(chat_id), text=message, parse_mode='HTML')
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        print(f"[Рассылка] Ошибка отправки пользователю {chat_id}: {e}")

        save_data(LAST_SENT_REVIEWS_FILE, current_reviews)
        print("[Отзывы] Рассылка завершена.")
    except Exception as e:
        print(f"[Критическая ошибка] Сбой в проверке отзывов: {e}")


async def check_report_status_periodically(context: ContextTypes.DEFAULT_TYPE):
    request_data = load_data(REQUEST_FILE)
    status = request_data.get('status')

    if status in ['completed', 'error']:
        chat_id = request_data.get('requested_by')
        if not chat_id: return

        if status == 'completed':
            try:
                if os.path.exists(LABOUR_COST_REPORT_FILE) and os.path.getsize(LABOUR_COST_REPORT_FILE) > 0:
                    completed_at = datetime.fromisoformat(request_data['completed_at']).strftime('%d.%m.%Y в %H:%M')
                    caption = f"✅ Ваш отчет по Labour Cost готов (сформирован {completed_at})."
                    await context.bot.send_document(chat_id=chat_id, document=open(LABOUR_COST_REPORT_FILE, 'rb'),
                                                    caption=caption)
                else:
                    await context.bot.send_message(chat_id=chat_id,
                                                   text="✅ Отчет сгенерирован, но он пуст (данные за период не найдены).")
            except FileNotFoundError:
                await context.bot.send_message(chat_id=chat_id, text="❗️ Отчет сгенерирован, но файл отчета не найден.")

        elif status == 'error':
            error_msg = request_data.get('error_message', 'Неизвестная ошибка.')
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"❌ При генерации отчета произошла ошибка:\n\n`{error_msg}`",
                                           parse_mode='Markdown')

        request_data['status'] = 'notified'
        save_data(REQUEST_FILE, request_data)


async def post_init(application: Application):
    user_commands = [
        BotCommand("start", "▶️ Запустить бота"),
        BotCommand("subscribe", "➕ Подписаться на отзывы"),
        BotCommand("mysubscriptions", "📄 Мои подписки на отзывы"),
        BotCommand("labourcost", "📊 Запросить отчет по Labour Cost"),
        BotCommand("getreport", "📂 Получить последний отчет"),
    ]
    await application.bot.set_my_commands(user_commands)

    admin_commands = user_commands + [
        BotCommand("listusers", "👥 Список пользователей"),
        BotCommand("block", "❌ Блокировать (ID)"),
        BotCommand("unblock", "✅ Разблокировать (ID)"),
    ]
    await application.bot.set_my_commands(admin_commands, scope={'type': 'chat', 'chat_id': BOT_ADMIN_CHAT_ID})
    print("[Бот] Меню команд обновлено.")


# -- Главная функция запуска бота --
def main() -> None:
    load_pizzerias()

    request_settings = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request_settings).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("labourcost", labour_cost_command)],
        states={
            GET_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_start_date)],
            GET_END_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_end_date_and_request_report)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("mysubscriptions", my_subscriptions))
    application.add_handler(CommandHandler("getreport", get_report_command))
    application.add_handler(CommandHandler("listusers", list_users))
    application.add_handler(CommandHandler("block", block_user))
    application.add_handler(CommandHandler("unblock", unblock_user))
    application.add_handler(CallbackQueryHandler(button_callback))

    application.job_queue.run_repeating(check_reviews_periodically, interval=180, first=10)
    application.job_queue.run_repeating(check_report_status_periodically, interval=15, first=5)

    print("[Бот] Запущен.")
    application.run_polling()


if __name__ == "__main__":
    main()

