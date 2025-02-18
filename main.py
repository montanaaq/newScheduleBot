import asyncio
import json
import logging
import os
from datetime import datetime
from pprint import pprint
from background import keep_alive

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

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

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)

teachers = ''


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


async def on_startup(_):
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


async def select_class(message: types.Message):
    global class_id
    class_id = await bot.send_message(chat_id=message.chat.id, text=f"Привет <b>{message.from_user.first_name}</b>, это бот для удобного просмотра расписаний занятий в Гимназии №33 г.Казань! \n\nНапиши класс в формате: <b>11Т</b>\nТеперь напиши свой класс: ", parse_mode='html')
    await Class_id.wait_for_class.set()
    logger.info(
        f'Ожидание ввода класса от пользователя {message.chat.id}')


async def complete_class(message: types.Message):
    global class_id, classes
    if (isinstance(message.text, str) and 2 <= len(message.text) <= 3 and message.text.upper() in classes):
        await set_class(message.chat.id, message.text.upper())

        # Удаляем сообщение с запросом класса
        await bot.delete_message(chat_id=message.chat.id, message_id=class_id.message_id)

        await bot.send_message(chat_id=message.chat.id, text=f'✅ Успешно! Ваш класс: <b>{message.text.upper()}</b>', parse_mode='html')
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

    async def get_schedule_for_day(user_id, day, msg):
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
        markup = types.InlineKeyboardMarkup()
        donatee = types.InlineKeyboardButton('Отправить донат',
                                             url='https://www.tinkoff.ru/rm/r_vpHWsJeqjz.TmlPkWbvLU/5Dvvy70865')
        markup.add(donatee)
        await bot.send_message(chat_id=message.from_user.id,
                               text='Если вам нравится работа бота и вы хотите поддержать разработчика материально, можете отправить донат по кнопке ниже :)', reply_markup=markup)
    if (message.text == 'Обратная связь'):
        markup = types.InlineKeyboardMarkup()
        razrab = types.InlineKeyboardButton(
            "Продолжить", url='https://t.me/montaanaq')
        markup.add(razrab)
        await bot.send_message(chat_id=message.from_user.id,
                               text='Писать только по работе бота, если нашли баг, без лишнего и спама. Чтобы связаться с разработчиком нажмите на кнопку ниже', reply_markup=markup)

    if (message.text == 'Профиль'):
        markup = types.InlineKeyboardMarkup(resize_keyboard=True)
        markup.row(kb.donate)
        markup.row(kb.notify)
        markup.row(kb.changes_in_schedule)
        markup.row(kb.unregister)
        await bot.send_message(chat_id=message.from_user.id, text='Профиль', reply_markup=markup)
    if (message.text == 'Учителя'):
        await bot.send_message(chat_id=message.chat.id, text=teachers, parse_mode='html')

    if (message.text == 'Оповещения'):
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        if message.from_user.id == message.chat.id:
            await bot.send_message(chat_id=message.chat.id,
                                   text='Чтобы включить или выключить оповещения от бота, нажмите на кнопки ниже.',
                                   reply_markup=kb.notify_keyboard)
        else:
            await bot.send_message(chat_id=message.chat.id, text="Данная функция работает только в личных сообщениях!")

    if class_id == '':
        if message.text not in formatted_messages and message.chat.id in users_id or message.chat.id in users_unregister:
            await bot.send_message(chat_id=message.chat.id, text='Мы не нашли вас в базе данных, попробуйте <b>/start</b> и повторите попытку! Пишите класс в формате: <b>11Т</b>', parse_mode='html')
    else:
        if message.text not in formatted_messages:
            await bot.send_message(chat_id=message.chat.id, text='Я тебя не понимаю... Используй /help')


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
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await donate(call.message)
    elif call.data == 'changes_in_schedule':
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await changes_in_schedule(call.message)
    elif call.data == 'unreg':
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await proccess_unregister(call.from_user.id)
        await bot.send_message(chat_id=call.message.chat.id, text='<b>Вы успешно сбросили регистрацию!</b>\n\n<b>/start</b> - для начала работы бота', parse_mode='html', reply_markup=types.ReplyKeyboardRemove())

    # days
    if call.data in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday']:
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        message_text = await msg.return_schedule(get_user_schedule(call.message.chat.id), call.data)
        await bot.send_message(chat_id=call.message.chat.id, text=message_text, parse_mode='html', reply_markup=kb.days)
    # notifications

    if call.data == 'unreg':
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await proccess_unregister(call.from_user.id)
        await bot.send_message(chat_id=call.message.chat.id, text='<b>Вы успешно сбросили регистрацию!</b>\n\n<i>/start</i> - для начала работы бота', parse_mode='html', reply_markup=types.ReplyKeyboardRemove())

    if call.data == 'register':
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await start_command(call.message)

    elif call.data == 'notify':
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        if call.from_user.id == call.message.chat.id:
            await bot.send_message(chat_id=call.message.chat.id,
                                   text='Чтобы включить или выключить оповещения от бота, нажмите на кнопки ниже.',
                                   reply_markup=kb.notify_keyboard)
        else:
            await bot.send_message(chat_id=call.message.chat.id, text="Данная функция работает только в личных сообщениях!")
    elif call.data == 'on_notifications':
        await notify_db(call.from_user.id, 1)
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await start_scheduler()
        await on_notify(call.message)
    elif call.data == 'off_notifications':
        await notify_db(call.from_user.id, 0)
        await bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        await off_notify(call.message)

keep_alive()
if __name__ == '__main__':
    logger.info("Запуск бота...")
    executor.start_polling(dp, skip_updates=False, on_startup=on_startup)
