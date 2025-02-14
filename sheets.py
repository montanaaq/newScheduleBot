import hashlib
import time
from pprint import pprint

import apiclient.discovery  # type: ignore
import httplib2
from oauth2client.service_account import ServiceAccountCredentials

# Файл, полученный в Google Developer Console
CREDENTIALS_FILE = 'creds.json'
# ID Google Sheets документа (можно взять из его URL)
spreadsheet_id = '1tL5zZM2ytn7u7WgEqQHNWaOkRU_jQb7er14d4v_sy_Q'

# Авторизуемся и получаем service — экземпляр доступа к API
credentials = ServiceAccountCredentials.from_json_keyfile_name(
    CREDENTIALS_FILE,
    ['https://www.googleapis.com/auth/spreadsheets',
     'https://www.googleapis.com/auth/drive'])
httpAuth = credentials.authorize(httplib2.Http())
service = apiclient.discovery.build('sheets', 'v4', http=httpAuth)


async def read_from_sheets(range_of_read: str, type_of_dimension: str):
    values = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_of_read,
        majorDimension=type_of_dimension
    ).execute()
    return values


async def parse_schedule(data, classes):
    schedule = {}
    current_day = None
    lesson_count = 0  # Счетчик уроков для текущего дня

    for row in data.get('values', []):
        if not row:  # Пропускаем пустые строки
            continue

        # Если строка начинается с дня недели, это новый день
        if row[0].strip() in ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]:
            current_day = row[0].strip()
            schedule[current_day] = {class_name: [] for class_name in classes}
            lesson_count = 0  # Сбрасываем счетчик уроков для нового дня
            continue

        # Если это строка с уроками
        if current_day:
            for i, class_name in enumerate(classes):
                if i + 1 >= len(row):  # Если данных для класса нет
                    schedule[current_day][class_name].append(
                        {'предмет': None, 'кабинет': None})
                else:
                    lesson_str = row[i + 1].strip()
                    if not lesson_str:  # Если урок отсутствует
                        schedule[current_day][class_name].append(
                            {'предмет': None, 'кабинет': None})
                    else:
                        # Разделяем предмет и кабинет
                        if ' (' in lesson_str and lesson_str.endswith(')'):
                            subject_part, room_part = lesson_str.rsplit(
                                ' (', 1)
                            subject = subject_part.strip()
                            # Удаляем закрывающую скобку
                            room = room_part[:-1].strip()
                        else:
                            subject = lesson_str
                            room = None
                        schedule[current_day][class_name].append(
                            {'предмет': subject, 'кабинет': room})
            lesson_count += 1

    # Убедимся, что у каждого класса ровно 7 уроков
    for day in schedule:
        for class_name in schedule[day]:
            while len(schedule[day][class_name]) < 7:
                schedule[day][class_name].append(
                    {'предмет': None, 'кабинет': None})

    return schedule


async def get_data_hash(data):
    """Функция для вычисления хеш-суммы данных (для проверки изменений)."""
    data_str = str(data)
    return hashlib.md5(data_str.encode()).hexdigest()


async def get_schedule_start():
    global classes
    # Читаем заголовки классов из первой строки (B1:E1)
    class_data = await read_from_sheets('Расписание!B1:E1', 'ROWS')
    classes = [class_name.strip() for class_name in class_data['values'][0]]

    # Читаем расписание
    data = await read_from_sheets(range_of_read="Расписание!A2:E", type_of_dimension="ROWS")
    schedule = await parse_schedule(data, classes)
    return schedule


async def get_teachers_from_sheets():
    teacher_data = await read_from_sheets('Учителя!A:B', 'ROWS')
    return teacher_data


async def format_teachers_list():
    """Читает список учителей из Google Sheets."""
    teacher_data = await get_teachers_from_sheets()
    if 'values' not in teacher_data:
        return "❌ Список учителей не найден."

    teachers_list = '👨‍🏫 <b>Список учителей:</b>\n'
    teachers_list += '—' * 20 + '\n'

    for row in teacher_data['values']:
        if len(row) < 2:
            continue
        subject, teacher_name = row
        teachers_list += f"👨‍🏫 <b>{subject}</b>\n<code>{teacher_name}</code>\n"
        teachers_list += '—' * 20 + '\n'

    return teachers_list

# Пример использования
if __name__ == "__main__":
    import asyncio

    # Инициализируем и начинаем проверку изменений
    asyncio.run(get_schedule_start())  # Сначала получаем начальное расписание
