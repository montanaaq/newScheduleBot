import sqlite3 as sql

users_db = sql.connect('databases/users.db')
schedule_db = sql.connect("databases/schedule.db")


def get_users_db_connection():
    return sql.connect('databases/users.db')


def get_schedule_db_connection():
    return sql.connect("databases/schedule.db")


def create_tables():
    """Create necessary tables if they don't exist."""
    users_cursor = users_db.cursor()
    users_cursor.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                tg_id INTEGER,
                class_id TEXT DEFAULT '',
                isNotified INTEGER DEFAULT 0
                )""")
    users_db.commit()
    users_db.close()

    schedule_cursor = schedule_db.cursor()
    schedule_cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_schedules (
            user_id INTEGER PRIMARY KEY,
            schedule TEXT
        )
    ''')
    schedule_db.commit()
    schedule_db.close()
