import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pprint import pprint

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.callback_data import CallbackData
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import keyboards as kb
import messages as msg
import sheets
from database import (create_tables, get_schedule_db_connection,
                      get_users_db_connection)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


users_db = get_users_db_connection()
users_cursor = users_db.cursor()

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv('PORT', 8000))
ADMIN_ID = os.getenv('ADMIN_ID', None)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

teachers = ''

# Конструируем WEBHOOK_URL
RENDER_EXTERNAL_HOSTNAME = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if RENDER_EXTERNAL_HOSTNAME:
    if RENDER_EXTERNAL_HOSTNAME.startswith("http://") or RENDER_EXTERNAL_HOSTNAME.startswith("https://"):
        RENDER_EXTERNAL_HOSTNAME = RENDER_EXTERNAL_HOSTNAME.split("://")[1]
    WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}{WEBHOOK_PATH}"
else:
    # Для локальной разработки используйте ngrok URL если необходимо
    # Замените на ваш реальный ngrok URL
    WEBHOOK_URL = os.getenv(
        'WEBHOOK_URL', "https://6f06-95-26-82-58.ngrok-free.app/webhook")


app = FastAPI(lifespan=lambda app: lifespan(app))
# ==========================
# Маршруты FastAPI
# ==========================


@app.get("/health")
async def health_check():
    return JSONResponse(content={"status": "ok"}, status_code=200)


@app.get("/webhook/status")
async def webhook_status():
    try:
        # Получаем информацию о текущем вебхуке
        webhook_info = await bot.get_webhook_info()

        return {
            "url": webhook_info.url,
            "is_set": bool(webhook_info.url),
            "pending_update_count": webhook_info.pending_update_count,
            "last_error_date": webhook_info.last_error_date,
            "last_error_message": webhook_info.last_error_message,
            "max_connections": webhook_info.max_connections
        }
    except Exception as e:
        logger.error(f"Error checking webhook status: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to retrieve webhook status"}
        )


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        update_data = await request.json()
        update = types.Update(**update_data)

        Dispatcher.set_current(dp)
        Bot.set_current(bot)

        await dp.process_update(update)

        return JSONResponse(content={"status": "ok"}, status_code=200)
    except Exception as e:
        logger.error(f"Не удалось обработать обновление: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=400)

# ==========================
# Lifespan Event Handlers
# ==========================


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.start()
    await on_startup()
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook установлен на {WEBHOOK_URL}")

    yield
    scheduler.shutdown()
    await bot.session.close()


async def read_data_start():
    global classes, schedule
    class_data = await sheets.read_from_sheets('B1:V1', 'ROWS')
    classes = [class_name.strip() for class_name in class_data['values'][0]]
    schedule = await sheets.get_schedule_start()
    print("Классы:", classes)
    pprint(schedule)


def get_all_users():
    """Получить список всех пользователей и их классов."""
    conn = get_users_db_connection()
    cursor = conn.cursor()
    # Таблица users должна хранить классы
    cursor.execute('SELECT tg_id, class_id FROM users')
    users = cursor.fetchall()
    conn.close()
    return users  # Список кортежей (user_id, class_id)


async def update_data():
    global classes, schedule, teachers
    previous_schedule_hash = None
    previous_teachers_hash = None

    while True:
        current_schedule_data = await sheets.read_from_sheets(
            range_of_read="Расписание!A2:V", type_of_dimension="ROWS"
        )
        current_schedule_hash = await sheets.get_data_hash(current_schedule_data)

        current_teachers_data = await sheets.read_from_sheets(
            range_of_read="Учителя!A:B", type_of_dimension="ROWS"
        )
        current_teachers_hash = await sheets.get_data_hash(current_teachers_data)

        if previous_schedule_hash != current_schedule_hash:
            print("Расписание обновлено!")
            schedule = await sheets.parse_schedule(current_schedule_data, classes)

            users = get_all_users()
            for user_id, user_class in users:
                if user_class:
                    await save_user_schedule(user_id, user_class, schedule)

            previous_schedule_hash = current_schedule_hash
        else:
            print("Расписание не изменилось.")

        if previous_teachers_hash != current_teachers_hash:
            print("Список учителей обновлен!")
            teachers = await sheets.format_teachers_list()
            previous_teachers_hash = current_teachers_hash
        else:
            print("Список учителей не изменился.")

        await asyncio.sleep(60)


async def save_user_schedule(user_id: int, user_class: str, schedule: dict):
    """Сохранить или обновить расписание пользователя в базе данных с учетом его класса."""
    conn = get_schedule_db_connection()
    cursor = conn.cursor()

    # Фильтруем расписание по классу пользователя
    class_schedule = await get_class_schedule(schedule, user_class)

    cursor.execute('''
        INSERT INTO user_schedules (user_id, schedule)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET schedule = ?
    ''', (user_id, json.dumps(class_schedule), json.dumps(class_schedule)))

    conn.commit()
    conn.close()


def get_user_schedule(user_id: int):
    """Retrieve a user's schedule from SQLite."""
    conn = get_schedule_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT schedule FROM user_schedules WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


@dp.message_handler(commands=['get_schedule'])
async def handle_get_schedule(message: types.Message):
    global teachers
    pprint(get_user_schedule(message.chat.id))
    # teachers_data = await sheets.read_from_sheets('Учителя!A:B', 'ROWS')
    pprint(teachers)

    # await bot.send_message(chat_id=message.chat.id, text=(get_user_schedule(message.chat.id)))
    # await bot.send_message(chat_id=message.chat.id, text=(msg.return_schedule(get_user_schedule(message.chat.id), 'monday')))


async def get_class_schedule(schedule, user_class):
    class_schedule = {}
    for day, classes in schedule.items():
        if user_class in classes:
            lessons = classes[user_class]
            formatted_lessons = []
            for i, lesson in enumerate(lessons):
                subject = lesson['предмет']
                room = lesson['кабинет']
                if subject is None:
                    if any(lessons[j]['предмет'] is not None for j in range(i + 1, len(lessons))):
                        formatted_lessons.append((day, "Нет", "—"))
                else:
                    formatted_lessons.append((day, f"{subject} {room}"))
            if formatted_lessons:
                class_schedule[day] = formatted_lessons
    return class_schedule


async def on_startup():
    create_tables()
    await read_data_start()
    asyncio.create_task(update_data())
    logger.info("Бот запущен!")


async def add_user_to_db(id: int, username: str):
    user = users_cursor.execute(
        'SELECT * FROM users WHERE tg_id == {key}'.format(key=id)).fetchone()
    username_name = users_cursor.execute(
        'SELECT * FROM users WHERE username == "{key}"'.format(key=username))
    if not user and username_name:
        users_cursor.execute(
            'INSERT INTO users (tg_id, username) VALUES (?, ?)', (id, username))
        users_db.commit()
        logger.info(f'Пользователь {username} добавлен в базу данных!')


@dp.message_handler(commands=["push"])
async def push_to_users(message: types.Message):
    if ADMIN_ID is not None:
        if message.chat.id == int(ADMIN_ID):
            keyboard = types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton(
                    'Отправить сообщение', callback_data='send_push_message')
            )
            await message.answer('Выберите действие:', reply_markup=keyboard)
        else:
            await message.answer('Я тебя не понимаю... Используй /help')
    else:
        await message.answer('Я тебя не понимаю... Используй /help')


@dp.callback_query_handler(lambda c: c.data == 'send_push_message')
async def handle_push_callback(callback_query: types.CallbackQuery):
    """Handles the 'Отправить сообщение' button."""
    await callback_query.answer()  # Closes the loading animation on the button
    await bot.send_message(callback_query.from_user.id, 'Какое сообщение вы хотите отправить?\n\nНапишите его здесь:')
    await PushMessage.push_message.set()


async def push_message_to_all_users(message_text: str):
    users = get_all_users()
    if not users:
        logger.warning("Нет пользователей для отправки сообщения.")
        return

    success_count, error_count = 0, 0

    for user in users:
        user_id = int(user[0])
        try:
            await bot.send_message(chat_id=user_id, text=f'<b>❕ Информация</b>\n\n{message_text}', parse_mode='html')
            success_count += 1
        except Exception as e:
            logger.error(
                f"Не удалось отправить сообщение пользователю {user_id}: {e}")
            error_count += 1
            continue

    logger.info(
        f"Рассылка завершена: {success_count} успешно, {error_count} с ошибками.")


class PushMessage(StatesGroup):
    push_message = State()


@dp.message_handler(state=PushMessage.push_message)
async def process_push_message(message: types.Message, state: FSMContext):
    message_text = message.text.strip()

    if not message_text:
        await message.answer('Сообщение не может быть пустым. Попробуйте снова.')
        return

    await push_message_to_all_users(message_text)
    await message.answer(f'✅ Сообщение успешно отправлено всем пользователям.')
    await state.finish()


@dp.message_handler(commands=["start"])
async def start_command(message: types.Message):
    logger.info(f'Команда /start от пользователя {message.chat.id}')
    user = users_cursor.execute('SELECT tg_id, class_id FROM users WHERE tg_id = ?',
                                (message.chat.id,)).fetchone()
    if user is None:
        await add_user_to_db(message.chat.id, f'@{message.from_user.username}')
        await select_class(message)

    elif user[1] == "":
        await select_class(message)
    else:
        await bot.send_message(chat_id=message.chat.id, text='Ты уже зарегистрирован! Для сброса регистрации используй <b>/unregister</b>', parse_mode='html')


async def select_class(message: types.Message):
    global class_id
    class_id = await bot.send_message(chat_id=message.chat.id, text=f"Привет <b>{message.from_user.first_name}</b>, это бот для удобного просмотра расписаний занятий в Гимназии №33 г.Казань! \n\nНапиши класс в формате: <b>11Т</b>\nТеперь напиши свой класс: ", parse_mode='html')
    await Class_id.wait_for_class.set()
    logger.info(
        f'Ожидание ввода класса от пользователя {message.chat.id}')


class Class_id(StatesGroup):
    wait_for_class = State()


@dp.message_handler(state=Class_id.wait_for_class)
async def proccess_select_class(message: types.Message, state: FSMContext):
    user_class = message.text.upper()
    async with state.proxy() as data:
        data['class_id'] = user_class

    user_schedule = await get_class_schedule(schedule, user_class)

    if user_schedule:
        await save_user_schedule(message.chat.id, user_class, schedule)
        await complete_class(message)
        logger.info(
            f'Пользователь {message.chat.id} ввел класс {user_class} и расписание сохранено.')
    else:
        await bot.send_message(chat_id=message.chat.id, text="Расписание для указанного класса не найдено. Пожалуйста, проверьте правильность ввода и повторите /start.")
        logger.warning(
            f'Расписание не найдено для класса {user_class} от пользователя {message.chat.id}.')

    await state.finish()

class_id = None  # Инициализируем переменную для хранения объекта сообщения


async def complete_class(message: types.Message):
    global class_id, classes
    if isinstance(message.text, str) and 2 <= len(message.text) <= 3 and message.text.upper() in classes:
        await set_class(message.chat.id, message.text.upper())

        # Редактируем сообщение с запросом класса, а не удаляем его
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=class_id.message_id,
            text=f'✅ Успешно! Ваш класс: <b>{message.text.upper()}</b>',
            parse_mode='html'
        )

        await asyncio.sleep(0.3)
        await start_schedule(message)
    else:
        await bot.send_message(chat_id=message.chat.id, text='Ошибка! Введите корректный класс: \nПример: 10Т')
        await Class_id.wait_for_class.set()
        logger.warning(
            f'Ошибка ввода класса от пользователя {message.chat.id}: {message.text}')


async def set_class(id: int, class_id: str):
    users_cursor.execute('UPDATE users SET class_id = "{class_name}" WHERE tg_id = "{id}"'.format(
        class_name=class_id, id=id))
    users_db.commit()


async def start_schedule(message: types.Message):
    await bot.send_message(chat_id=message.chat.id,
                           text='Теперь можешь пользоваться ботом! Для подробной информации о командах используй <b>/help</b>', reply_markup=kb.main,
                           parse_mode='html')


@dp.message_handler(commands=['help'])
async def help(message: types.Message):
    await bot.send_message(chat_id=message.chat.id, text='''Привет! Вот список доступных команд:

1. <b>/start</b> — Регистрация и начало работы с ботом. После использования этой команды вы сможете взаимодействовать с ботом.

2. <b>/donate</b> — Поддержите разработчика материально. Ваша поддержка помогает нам улучшать бот!

3. <b>Кнопка: На сегодня</b> — Узнайте расписание на сегодня. Все уроки и мероприятия будут отображены для текущего дня.

4. <b>Кнопка: На завтра</b> — Узнайте расписание на завтра. Проверьте, что вас ожидает на следующий день.

5. <b>Кнопка: Полностью</b> — Просмотр расписания на все дни недели. Удобно, чтобы не искать расписание на каждый день по отдельности.

6. <b>Кнопка: По дням</b> — Получите расписание для конкретного дня.

7. <b>/notify</b> — Система оповещений. Включите уведомления и бот сам будет присылать вам уведомления о расписании каждый день в 7:45 по МСК.

8. <b>/unregister</b> - Сбросить регистрацию или поменять класс.

Если вам нужно больше информации по какой-либо команде, просто напишите её название!''', parse_mode='html')


@dp.message_handler(commands=['notify'])
async def notify(message: types.Message):
    if message.from_user.id == message.chat.id:
        await bot.send_message(chat_id=message.chat.id,
                               text='Чтобы включить или выключить оповещения от бота, нажмите на кнопки ниже.',
                               reply_markup=kb.notify_keyboard)
    else:
        await bot.send_message(chat_id=message.chat.id, text="Данная функция работает только в личных сообщениях!")


@dp.message_handler(commands=['donate'])
async def donate(message: types.Message):
    markup = types.InlineKeyboardMarkup()
    donatee = types.InlineKeyboardButton(
        'Отправить донат', url='https://www.tinkoff.ru/rm/nurislamov.amir8/cktHx65549')
    markup.add(donatee)
    await bot.send_message(chat_id=message.chat.id,
                           text='Если вам нравится работа бота и вы хотите поддержать разработчика материально, можете отправить донат по кнопке ниже :)'.format(
                               message.from_user), reply_markup=markup)


@dp.message_handler(commands=['unregister'])
async def unregister(message: types.Message):
    await bot.send_message(chat_id=message.chat.id, text='Чтобы сбросить регистрацию, нажми на кнопку ниже', reply_markup=kb.unregister_markup)


# сделать изменения в расписании
async def changes_in_schedule(message: types.Message):
    await bot.send_message(chat_id=message.chat.id, text='Пока изменений в расписании не обнаружено.',
                           parse_mode='html')


async def report_bug(message: types.Message):
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
        "Сообщить об ошибке", callback_data='report_about_bug'))
    await bot.send_message(chat_id=message.from_user.id,
                           text='Если вы нашли несовпадение в расписании или списке учителей, нажмите на кнопку ниже, для связи с разработчиком.', reply_markup=markup)


@dp.callback_query_handler(lambda c: c.data == 'report_about_bug')
async def handle_report_callback(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await bot.send_message(callback_query.from_user.id, 'Какое сообщение вы хотите отправить?\n\nНапишите его здесь:')
    await ReportMessage.report_message.set()


async def send_to_admin(message_text: str, username: str):
    await bot.send_message(chat_id=int(ADMIN_ID), text=f'🚨 Новый репорт от @{username} \n\nСообщение: {message_text}')


class ReportMessage(StatesGroup):
    report_message = State()


@dp.message_handler(state=ReportMessage.report_message)
async def process_report_message(message: types.Message, state: FSMContext):
    message_text = message.text.strip()

    if not message_text:
        await message.answer('Сообщение не может быть пустым. Попробуйте снова.')
        return

    await send_to_admin(message_text, message.from_user.username)
    await message.answer(f'✅ Сообщение успешно отправлено разработчику.')
    await state.finish()


@dp.message_handler(content_types=['text'])
async def func(message: types.Message):
    class_id = users_cursor.execute('SELECT class_id FROM users WHERE tg_id = "{id}"'.format(
        id=message.chat.id)).fetchone()[0]
    users_unregister = [row[0] for row in users_cursor.execute(
        'SELECT tg_id FROM users WHERE class_id = 0').fetchall()]
    users_id = [row[0]
                for row in users_cursor.execute('SELECT tg_id FROM users').fetchall()]

    formatted_messages = [
        'На завтра',
        'На сегодня',
        'Полностью',
        'По дням',
        'Профиль',
        'Донат',
        '/notify',
        'Обратная связь',
        'Учителя',
        'Оповещения',
        '/unregister',
        '/start'
    ]

    async def get_schedule_for_day(user_id: int, day: str, msg: str):
        # Проверяем, если запрашивается полное расписание
        global user_schedule, teachers
        if day == 'full':
            # Получаем расписание пользователя
            # Запрашиваем полное расписание
            response = await msg.return_schedule(get_user_schedule(message.chat.id), 'full')
            await bot.send_message(chat_id=user_id, text=response, parse_mode='html')
            return

        # Проверяем, есть ли запрашиваемый день в day_mapping
        day_mapping = {
            "0": 'monday',  # Понедельник
            "1": 'tuesday',  # Вторник
            "2": 'wednesday',  # Среда
            "3": 'thursday',  # Четверг
            "4": 'friday',  # Пятница
            "5": 'saturday',  # Суббота
            "6": 'sunday'   # Воскресенье
        }

        if day in day_mapping:
            # Получаем расписание пользователя
            requested_day = day_mapping[day]  # Используем цифры от 0 до 6
            # Запрашиваем расписание на конкретный день
            response = await msg.return_schedule(get_user_schedule(message.chat.id), requested_day)
            await bot.send_message(chat_id=user_id, text=response, parse_mode='html')
        else:
            await bot.send_message(chat_id=user_id, text='Неверный день недели!')

    if message.text == 'По дням':
        await bot.send_message(chat_id=message.chat.id, text='Здесь вы можете выбрать расписание по дням', reply_markup=kb.days)

    if message.text == 'Полностью':
        await bot.send_message(chat_id=message.chat.id, text=(await msg.return_schedule(get_user_schedule(message.chat.id), 'full')), parse_mode='html')

    if message.text == 'На завтра':
        tomorrow = (datetime.now().weekday() + 1) % 7
        # Преобразуем в строку
        await get_schedule_for_day(message.chat.id, str(tomorrow), msg)

    if message.text == 'На сегодня':
        today = datetime.now().weekday()
        # Преобразуем в строку
        await get_schedule_for_day(message.chat.id, str(today), msg)

    if (message.text == 'Донат'):
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('Отправить донат',
                                                                             url='https://www.tinkoff.ru/rm/r_vpHWsJeqjz.TmlPkWbvLU/5Dvvy70865'))
        await bot.send_message(chat_id=message.from_user.id,
                               text='Если вам нравится работа бота и вы хотите поддержать разработчика материально, можете отправить донат по кнопке ниже :)', reply_markup=markup)

    if (message.text == 'Обратная связь'):
        await report_bug(message)

    if (message.text == 'Профиль'):
        markup = types.InlineKeyboardMarkup(resize_keyboard=True)
        markup.row(kb.donate)
        markup.row(kb.notify)
        markup.row(kb.changes_in_schedule)
        markup.row(kb.unregister)
        await bot.send_message(chat_id=message.from_user.id, text='Профиль', reply_markup=markup)

    if (message.text == 'Оповещения'):
        if message.from_user.id == message.chat.id:
            await bot.send_message(chat_id=message.chat.id,
                                   text='Чтобы включить или выключить оповещения от бота, нажмите на кнопки ниже.',
                                   reply_markup=kb.notify_keyboard)
        else:
            await bot.send_message(chat_id=message.chat.id, text="Данная функция работает только в личных сообщениях!")

    if (message.text == 'Учителя'):
        message_text, keyboard = await create_page()
        await message.answer(message_text, parse_mode='HTML', reply_markup=keyboard)

    if class_id == '':
        if message.text not in formatted_messages and message.chat.id in users_id or message.chat.id in users_unregister:
            await bot.send_message(chat_id=message.chat.id, text='Мы не нашли вас в базе данных, попробуйте <b>/start</b> и повторите попытку! Пишите класс в формате: <b>11Т</b>', parse_mode='html')
    else:
        if message.text not in formatted_messages:
            await bot.send_message(chat_id=message.chat.id, text='Я тебя не понимаю... Используй /help')
pagination_cb = CallbackData('teachers', 'action', 'page')


async def get_teachers_data() -> list:
    """Получает и форматирует данные учителей"""
    teachers_text = await sheets.format_teachers_list()
    teachers = teachers_text.split('—' * 20 + '\n')
    return [t.strip() for t in teachers if t.strip()]


async def create_page(page: int = 0, per_page: int = 10) -> tuple:
    """Создает страницу с учителями и разделителями"""
    all_teachers = await get_teachers_data()
    total_pages = (len(all_teachers) + per_page - 1) // per_page

    start = page * per_page
    end = start + per_page
    page_teachers = all_teachers[start:end]

    # Формируем сообщение с разделителями
    separator = '\n' + '—' * 20 + '\n'
    message_text = f"👨🏫 <b>Список учителей:</b>\n\n"
    message_text += separator.join(page_teachers)
    message_text += f"\n\n<b>Страница {page + 1} из {total_pages}</b>"

    # Создаем клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = []

    if page > 0:
        buttons.append(types.InlineKeyboardButton(
            "⬅ Назад",
            callback_data=pagination_cb.new(action="prev", page=page-1)
        ))
    if page < total_pages - 1:
        buttons.append(types.InlineKeyboardButton(
            "Вперед ➡",
            callback_data=pagination_cb.new(action="next", page=page+1)
        ))

    if buttons:
        keyboard.add(*buttons)

    return message_text, keyboard


@dp.callback_query_handler(pagination_cb.filter(action=['prev', 'next']))
async def pagination_handler(call: types.CallbackQuery, callback_data: dict):
    """Обработчик пагинации"""
    page = int(callback_data['page'])
    action = callback_data['action']

    # Обновляем страницу
    message_text, keyboard = await create_page(page)
    await call.message.edit_text(message_text, parse_mode='HTML', reply_markup=keyboard)
    await call.answer()


async def proccess_unregister(id: int):
    users_cursor.execute(
        'UPDATE users SET class_id = "" WHERE tg_id = "{id}"'.format(id=id))
    users_db.commit()


async def notify_db(id: int, isNotified: int):
    users_cursor.execute('UPDATE users SET isNotified = "{isNotified}" WHERE tg_id = "{id}"'.format(
        isNotified=isNotified, id=id))
    users_db.commit()


async def send_schedule_cron():
    notified_users = [row[0] for row in users_cursor.execute(
        'SELECT tg_id FROM users WHERE isNotified = "{isNotified}"'.format(isNotified=1)).fetchall()]

    weekdays = {
        0: 'monday',
        1: 'tuesday',
        2: 'wednesday',
        3: 'thursday',
        4: 'friday',
        5: 'saturday'
    }

    current_day = weekdays.get(datetime.now().weekday())
    if current_day:
        message_text = await msg.return_schedule(current_day)
        for user in notified_users:
            await bot.send_message(chat_id=user, text=message_text, parse_mode='html')


async def on_notify(message: types.Message):
    await bot.send_message(chat_id=message.chat.id,
                           text='✅ Успешно! Оповещения о расписании <i>включены</i>. <b>Они будут отправляться автоматически каждый день [Понедельник-Суббота] в 7:45.</b>',
                           parse_mode='html')


async def off_notify(message: types.Message):
    await bot.send_message(chat_id=message.chat.id,
                           text='✅ Успешно! Оповещения о расписании <i>выключены</i>. <b>Теперь они не будут больше приходить.</b>',
                           parse_mode='html')


async def start_scheduler():
    scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
    scheduler.add_job(send_schedule_cron, 'cron',
                      day_of_week='mon-sat', hour=7, minute=45)
    scheduler.start()
    logger.info('Запущен планировщик отправки расписания')


@dp.callback_query_handler()
async def callback(call: types.CallbackQuery) -> None:

    # profile
    if call.data == 'donate':
        await donate(call.message)
        await call.answer()
    elif call.data == 'changes_in_schedule':
        await changes_in_schedule(call.message)
        await call.answer()
    elif call.data == 'unreg':
        await call.answer()
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await proccess_unregister(call.from_user.id)
        await bot.send_message(chat_id=call.message.chat.id, text='<b>Вы успешно сбросили регистрацию!</b>\n\n<b>/start</b> - для начала работы бота', parse_mode='html', reply_markup=types.ReplyKeyboardRemove())

    # days
    if call.data in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday']:
        message_text = await msg.return_schedule(get_user_schedule(call.message.chat.id), call.data)
        await call.answer()
        await bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=message_text, parse_mode='html', reply_markup=kb.days)
    # notifications

    elif call.data == 'notify':
        if call.from_user.id == call.message.chat.id:
            await bot.send_message(chat_id=call.message.chat.id,
                                   text='Чтобы включить или выключить оповещения от бота, нажмите на кнопки ниже.',
                                   reply_markup=kb.notify_keyboard)
        else:
            await bot.send_message(chat_id=call.message.chat.id, text="Данная функция работает только в личных сообщениях!")
    elif call.data == 'on_notifications':
        await notify_db(call.from_user.id, 1)
        await start_scheduler()
        await on_notify(call.message)
        await call.answer()
    elif call.data == 'off_notifications':
        await notify_db(call.from_user.id, 0)
        await off_notify(call.message)
        await call.answer()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
    # from aiogram.utils import executor
    # executor.start_polling(
    #     dispatcher=dp, on_startup=on_startup, skip_updates=False)
