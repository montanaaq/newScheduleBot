import asyncio
import json
import logging
import os
import time
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

isLocal = False

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
    class_data = await sheets.read_from_sheets('B1:AF1', 'ROWS')
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


@dp.message_handler(commands=['clear_classes'])
async def clear_classes(message: types.Message):
    if ADMIN_ID is not None:
        if message.chat.id == int(ADMIN_ID):
            """Remove all class IDs for users and hide reply keyboards for each user."""
            conn = get_users_db_connection()
            cursor = conn.cursor()

            cursor.execute("UPDATE users SET class_id = ''")
            conn.commit()

            cursor.execute("SELECT tg_id FROM users")
            users = cursor.fetchall()
            conn.close()

            for user in users:
                tg_id = user[0]
                try:
                    await bot.send_message(tg_id, "Ваш класс был сброшен! Чтобы указать класс, используйте /start", reply_markup=types.ReplyKeyboardRemove())
                except Exception as e:
                    print(f"Failed to send message to {tg_id}: {e}")

            # Confirm execution to the admin
            await message.answer("Все классы для пользователей были сброшены и клавиатуры были убраны")


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

async def sub_verification(chat_member: types.ChatMember) -> bool:
    member = await chat_member
    if member.status != 'left':
        return True
    else:
        return False


@dp.message_handler(commands=["start"])
async def start_command(message: types.Message, state: FSMContext):
    logger.info(f'Команда /start от пользователя {message.chat.id}')
    user = users_cursor.execute('SELECT tg_id, class_id FROM users WHERE tg_id = ?',
                                (message.chat.id,)).fetchone()
    if await sub_verification(bot.get_chat_member(chat_id="@gymn33_bot_news", user_id=message.from_user.id)):
        if user is None:
            await add_user_to_db(message.chat.id, f'@{message.from_user.username}')
            await select_class(message, state)

        elif user[1] == "":
            await select_class(message, state)
        else:
            await message.answer('Ты уже зарегистрирован! Для смены класса регистрации используй <b>/change</b>', parse_mode='html')
    else:
        await message.answer('Чтобы пользоваться ботом, подпишись на наш новостной канал @gymn33_bot_news!')

async def select_class(message: types.Message, state: FSMContext):
    # Отправляем сообщение и сохраняем его ID в состоянии
    sent_message = await message.answer(
        f"Привет <b>{message.from_user.first_name}</b>, это бот для удобного просмотра расписаний занятий в Гимназии №33 г.Казань! \n\n"
        "Напиши класс в формате <b>11Т</b> пиши без пробелов. Список доступных классов можешь посмотреть в закрепленном сообщении: @gymn33_bot_news\n"
        "Теперь напиши свой класс: ",
        parse_mode='html'
    )

    # Сохраняем ID сообщения в состоянии
    await state.update_data(class_message_id=sent_message.message_id)
    await Class_id.wait_for_class.set()
    logger.info(f'Ожидание ввода класса от пользователя {message.chat.id}')


class Class_id(StatesGroup):
    wait_for_class = State()


@dp.message_handler(state=Class_id.wait_for_class)
async def proccess_select_class(message: types.Message, state: FSMContext):
    user_class = message.text.upper()
    user_data = await state.get_data()

    user_schedule = await get_class_schedule(schedule, user_class)

    if user_schedule:
        await save_user_schedule(message.chat.id, user_class, schedule)
        await complete_class(message, state)  # Передаем state
        logger.info(
            f'Пользователь {message.chat.id} ввел класс {user_class} и расписание сохранено.')
    else:
        await message.answer("Расписание для указанного класса не найдено. Пожалуйста, проверьте правильность ввода и повторите /start.")
        logger.warning(
            f'Расписание не найдено для класса {user_class} от пользователя {message.chat.id}.')

    await state.finish()


async def complete_class(message: types.Message, state: FSMContext):
    user_data = await state.get_data()

    try:
        # Пытаемся отредактировать оригинальное сообщение
        message_id_to_edit = user_data.get('class_message_id')
        if message_id_to_edit:
            await set_class(message.chat.id, message.text.upper())
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message_id_to_edit,
                text=f'✅ Успешно! Ваш класс: <b>{message.text.upper()}</b>',
                parse_mode='html'
            )
    except Exception as e:
        logger.error(f"Ошибка редактирования сообщения: {e}")
        await set_class(message.chat.id, message.text.upper())
        # Если не удалось отредактировать, отправляем новое сообщение
        await message.answer(f'✅ Успешно! Ваш класс: <b>{message.text.upper()}</b>', parse_mode='html')

    await asyncio.sleep(0.3)
    await start_schedule(message)


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

8. <b>/change</b> - Поменять класс.

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


# @dp.message_handler(commands=['send_to_channel'])
# async def send_to_channel(message: types.Message):
#     keyboard = types.InlineKeyboardMarkup(row_width=1).add(types.InlineKeyboardButton(text='Перейти в бота', url='https://telegram.me/gymn33_bot'))
#     await bot.send_message(chat_id=-1002406956516, text='''
# 🤖 <b>Всем привет!</b> Наконец-то мы смогли возобновить работу над ботом и исправить все недочёты предыдущей версии. Сейчас данный бот работает в пределах <b>Гимназии №33 г.Казань</b>. Сейчас бот успешно работает с расписанием для 7-11 классов включительно. 💫

# 🆘 Если нашли несовпадение в расписании или бот работает неисправно нажмите на кнопку <b>"Обратная связь"</b> и опишите проблему с которой вы столкнулись.

# Список доступных классов на данный момент:
# <b>7А, 7Б, 7В, 7И</b>
# <b>8А, 8Б, 8В, 8Г, 8И</b>
# <b>9А, 9Б, 9В, 9И</b>
# <b>10Б, 10Т</b>
# <b>11Б, 11Т</b>

# ''', reply_markup=keyboard, parse_mode='html')

@dp.message_handler(commands=['change'])
async def unregister(message: types.Message):
    await bot.send_message(chat_id=message.chat.id, text='Чтобы сменить класс, нажми на кнопку ниже', reply_markup=kb.unregister_markup)


# сделать изменения в расписании
async def changes_in_schedule(message: types.Message):
    await bot.send_message(chat_id=message.chat.id, text='Пока изменений в расписании не обнаружено.',
                           parse_mode='html')


async def report_bug(message: types.Message):
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
        "Сообщить об ошибке", callback_data='report_about_bug'))
    await bot.send_message(chat_id=message.from_user.id,
                           text='Если вы нашли несовпадение в расписании или списке учителей, нажмите на кнопку ниже, для связи с разработчиком.', reply_markup=markup)


last_report_time = {}


class ReportMessage(StatesGroup):
    report_message = State()


@dp.callback_query_handler(lambda c: c.data == 'report_about_bug')
async def handle_report_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    current_time = time.time()

    if user_id in last_report_time and current_time - last_report_time[user_id] < 3600:
        remaining_time = int(3600 - (current_time - last_report_time[user_id]))
        minutes = remaining_time // 60
        seconds = remaining_time % 60
        await callback_query.answer(f'⏳ Вы уже отправили сообщение. Попробуйте снова через {minutes} мин. {seconds} сек.', show_alert=True)
        return

    await callback_query.answer()
    kb = types.ReplyKeyboardMarkup().add(types.KeyboardButton('Выход'))
    await bot.send_message(callback_query.from_user.id, 'Какое сообщение вы хотите отправить?\n\nНапишите его здесь:', reply_markup=kb)
    await ReportMessage.report_message.set()


async def send_to_admin(message_text: str, username: str):
    username = username if username else 'Без имени пользователя'
    await bot.send_message(chat_id=int(ADMIN_ID), text=f'🚨 Новый репорт от @{username} \n\nСообщение: {message_text}')

@dp.message_handler(lambda message: message.text.lower() == 'выход', state=ReportMessage.report_message)
async def process_exit(message: types.Message, state: FSMContext):
    """Обрабатывает кнопку 'Выход' только в состоянии отправки репорта."""
    await message.answer("Отмена отправки сообщения.", reply_markup=kb.main)
    await state.finish()

@dp.message_handler(state=ReportMessage.report_message)
async def process_report_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username if message.from_user.username else f'ID: {user_id}'
    message_text = message.text.strip()

    if not message_text:
        await message.answer('Сообщение не может быть пустым. Попробуйте снова.')
        return

    await send_to_admin(message_text, username)
    last_report_time[user_id] = time.time()
    await message.answer('✅ Сообщение успешно отправлено разработчику.', reply_markup=kb.main)
    await state.finish()


@dp.message_handler(content_types=['text'])
async def func(message: types.Message):
    chat_id = "@gymn33_bot_news"
    user_id = message.from_user.id

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status == "left":
            print(12)
            await message.answer(
                'Чтобы пользоваться ботом, подпишись на наш новостной канал @gymn33_bot_news!'
            )
            return
    except Exception as e:
        await message.answer('Произошла ошибка при проверке подписки. Попробуйте снова позже.')
        return

    user_data = users_cursor.execute(
        'SELECT class_id FROM users WHERE tg_id = ?', (message.chat.id,)
    ).fetchone()

    class_id = user_data[0] if user_data else None
    print(class_id)
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
        '/start',
        'Выход'
    ]

    if class_id is None or class_id == '':
        await bot.send_message(
            chat_id=message.chat.id,
            text='Мы не нашли вас в базе данных. Попробуйте <b>/start</b> и повторите попытку! '
                    'Пожалуйста, укажите класс в формате: <b>11Т</b>',
            parse_mode='html'
        )
        return

    async def get_schedule_for_day(user_id: int, day: str, msg: str, context: str = 'today'):
        global user_schedule, teachers
        if day == 'full':
            response = await msg.return_schedule(get_user_schedule(message.chat.id), 'full')
            await bot.send_message(chat_id=user_id, text=response, parse_mode='html')
            return

        day_mapping = {
            "0": 'monday',
            "1": 'tuesday',
            "2": 'wednesday',
            "3": 'thursday',
            "4": 'friday',
            "5": 'saturday',
            "6": 'sunday',
        }

        if day in day_mapping:
            requested_day = day_mapping[day]
            response = await msg.return_schedule(get_user_schedule(message.chat.id), requested_day, context)
            await bot.send_message(chat_id=user_id, text=response, parse_mode='html')
        else:
            await bot.send_message(chat_id=user_id, text='❌ Неверный день недели!')

    # Обработка команд
    if message.text == 'По дням':
        await bot.send_message(chat_id=message.chat.id, text='Выберите день недели:', reply_markup=kb.days)

    elif message.text == 'Полностью':
        await bot.send_message(
            chat_id=message.chat.id,
            text=(await msg.return_schedule(get_user_schedule(message.chat.id), 'full')),
            parse_mode='html'
        )

    elif message.text == 'На завтра':
        tomorrow = (datetime.now().weekday() + 1) % 7
        await get_schedule_for_day(message.chat.id, str(tomorrow), msg, 'tommorow')

    elif message.text == 'На сегодня':
        today = datetime.now().weekday()
        await get_schedule_for_day(message.chat.id, str(today), msg, 'today')

    elif message.text == 'Донат':
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton(
                'Отправить донат', url='https://www.tinkoff.ru/rm/r_vpHWsJeqjz.TmlPkWbvLU/5Dvvy70865')
        )
        await bot.send_message(
            chat_id=message.from_user.id,
            text='💰 Если вам нравится работа бота и вы хотите поддержать разработчика, нажмите на кнопку ниже:',
            reply_markup=markup
        )

    elif message.text == 'Обратная связь':
        await report_bug(message)

    elif message.text == 'Профиль':
        markup = types.InlineKeyboardMarkup(resize_keyboard=True)
        markup.row(kb.donate)
        markup.row(kb.notify)
        markup.row(kb.changes_in_schedule)
        markup.row(kb.unregister)
        await bot.send_message(chat_id=message.from_user.id, text='📄 Ваш профиль:', reply_markup=markup)

    elif message.text == 'Оповещения':
        if message.chat.type == 'private':
            await bot.send_message(
                chat_id=message.chat.id,
                text='🔔 Чтобы включить или выключить уведомления, используйте кнопки ниже.',
                reply_markup=kb.notify_keyboard
            )
        else:
            await bot.send_message(chat_id=message.chat.id, text="❌ Эта функция работает только в личных сообщениях!")

    elif message.text == 'Учителя':
        message_text, keyboard = await create_page()
        await message.answer(message_text, parse_mode='HTML', reply_markup=keyboard)

    elif message.text not in formatted_messages:
        await bot.send_message(chat_id=message.chat.id, text='🤖 Я не понимаю эту команду. Используйте /help.')

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
    if isLocal:
        from aiogram.utils import executor
        executor.start_polling(
            dispatcher=dp, on_startup=on_startup, skip_updates=False)
    else:
        import uvicorn
        uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
