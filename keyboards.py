from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)

main = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
main.row(KeyboardButton('На завтра', callback_data='tommorow'))
main.row(
    KeyboardButton('На сегодня', callback_data='today'),
    KeyboardButton('По дням', callback_data='days'),
    KeyboardButton('Полностью', callback_data='full')
)
main.row(
    KeyboardButton('Учителя', callback_data='uchitelya'),
    KeyboardButton('Профиль', callback_data='profile'),
    KeyboardButton('Оповещения', callback_data='notify')
)
main.row(
    KeyboardButton('Обратная связь', callback_data='comm'),
    KeyboardButton('Донат', callback_data='donate')
)

days = InlineKeyboardMarkup().add(
    InlineKeyboardButton('Понедельник', callback_data='monday'),
    InlineKeyboardButton('Вторник', callback_data='tuesday'),
    InlineKeyboardButton('Среда', callback_data='wednesday'),
    InlineKeyboardButton('Четверг', callback_data='thursday'),
    InlineKeyboardButton('Пятница', callback_data='friday'),
    InlineKeyboardButton('Суббота', callback_data='saturday'),
)
donate = InlineKeyboardButton('Поддержать проект', callback_data='donate')
my_class = InlineKeyboardButton('Мой класс', callback_data='my_class')
changes_in_schedule = InlineKeyboardButton(
    'Изменения в расписании', callback_data='changes_in_schedule')
notify = InlineKeyboardButton('Оповещения', callback_data='notify')
unregister = InlineKeyboardButton(
    'Сменить класс', callback_data='unreg')

unregister_markup = InlineKeyboardMarkup(resize_keyboard=True).add(
    InlineKeyboardButton('Сменить класс', callback_data='unreg')
)

subscribe = InlineKeyboardMarkup().add(
    InlineKeyboardButton('Подписаться', url='https://t.me/gymn33_bot_news')
)

on = InlineKeyboardButton('🔔 Включить оповещения',
                          callback_data='on_notifications')
off = InlineKeyboardButton('🔕 Выключить оповещения',
                           callback_data='off_notifications')

notify_keyboard = InlineKeyboardMarkup(resize_keyboard=True, row_width=1)
notify_keyboard.add(on)
notify_keyboard.add(off)

weekdays = InlineKeyboardMarkup(resize_keyboard=True, row_width=2).add(
    InlineKeyboardButton('Понедельник', callback_data='edit_monday'),
    InlineKeyboardButton('Вторник', callback_data='edit_tuesday'),
    InlineKeyboardButton('Среда', callback_data='edit_wednesday'),
    InlineKeyboardButton('Четверг', callback_data='edit_thursday'),
    InlineKeyboardButton('Пятница', callback_data='edit_friday'),
    InlineKeyboardButton('Суббота', callback_data='edit_saturday'),
)
