async def return_schedule(schedule_data: dict, requested_day: str) -> str:
    """Возвращает отформатированное расписание, оптимизированное для Telegram."""

    if not schedule_data:
        return "❌ Ваше расписание не найдено. Возможно, вам нужно заново выбрать класс с помощью /start."

    days = {
        "monday": "Понедельник",
        "tuesday": "Вторник",
        "wednesday": "Среда",
        "thursday": "Четверг",
        "friday": "Пятница",
        "saturday": "Суббота",
        "sunday": "Воскресенье"
    }

    def format_day(day_name: str, lessons: list) -> str:
        """Форматирует расписание на один день с заголовками и разделителями."""
        if not lessons:
            return f"📅 <b>{day_name}</b>\n<i>Расписание отсутствует.</i>"

        header = f"📅 <b>{day_name}</b>\n"
        table_header = "<b>№ | Предмет</b>\n—————————————\n"
        schedule_body = ""

        for i, lesson in enumerate(lessons):
            lesson_number = str(i + 1).rjust(2)
            subject = lesson[1]  # Содержит и предмет, и кабинет

            schedule_body += f"<b>{lesson_number}</b> | {subject}\n"

        return f"{header}<pre>{table_header}{schedule_body}</pre>"

    # Если запрашивается полное расписание
    if requested_day == 'full':
        return "\n\n".join(format_day(day, lessons) for day, lessons in schedule_data.items())

    # Преобразуем день (число) в название дня недели
    day_name = days.get(requested_day)

    if day_name and day_name in schedule_data:
        return format_day(day_name, schedule_data[day_name])

    return "❌ Неверный день недели."