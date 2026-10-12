import aiosqlite

class Database:
    def __init__(self, path):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS tracked_flights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    flight_id TEXT NOT NULL,
                    flight_number TEXT NOT NULL,
                    baggage_status TEXT NOT NULL DEFAULT 'pending',
                    baggage_user_id INTEGER,
                    baggage_username TEXT,
                    baggage_at TEXT,
                    note TEXT,
                    UNIQUE(chat_id, message_id)
                )
            ''')
            # Миграция для уже существующей базы (столбца note там ещё нет).
            cursor = await db.execute("PRAGMA table_info(tracked_flights)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "note" not in columns:
                await db.execute("ALTER TABLE tracked_flights ADD COLUMN note TEXT")

            # Отдельный, НЕ подчищаемый вместе с окном для исправлений лог
            # подтверждений — на нём считается статистика по сменам. Один
            # рейс = одна запись (UNIQUE), поправка статуса через /admin
            # просто перезаписывает её, а не плодит дубли.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS baggage_log (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    flight_number TEXT NOT NULL,
                    baggage_status TEXT NOT NULL,
                    user_id INTEGER,
                    username TEXT,
                    confirmed_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                )
            ''')

            # Админы, добавленные через бота (/addadmin), а не только через
            # ADMIN_IDS в переменных окружения. ADMIN_IDS остаются "сид"-
            # админами — их нельзя снять через бота, только правкой env.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    added_by INTEGER,
                    added_at TEXT NOT NULL
                )
            ''')

            # ID последнего автоматически присланного отчёта статистики по
            # чату — чтобы при следующей отправке знать, что удалить, даже
            # если бот между этим успел перезапуститься.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS last_stats_message (
                    chat_id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL
                )
            ''')

            # Шутка, которую получил каждый человек в последнем отчёте —
            # чтобы на следующий день не повторять её же.
            await db.execute('''
                CREATE TABLE IF NOT EXISTS joke_history (
                    chat_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    last_joke TEXT NOT NULL,
                    PRIMARY KEY (chat_id, username)
                )
            ''')
            await db.commit()

    async def add(self, chat_id, message_id, flight_id, flight_number, note=None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO tracked_flights
                (chat_id, message_id, flight_id, flight_number, note)
                VALUES (?, ?, ?, ?, ?)
            ''', (chat_id, message_id, flight_id, flight_number, note))
            await db.commit()

    async def set_baggage_status(self, chat_id, message_id, status, user_id, username, at):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('''
                UPDATE tracked_flights
                SET baggage_status=?, baggage_user_id=?, baggage_username=?, baggage_at=?
                WHERE chat_id=? AND message_id=?
            ''', (status, user_id, username, at, chat_id, message_id))
            await db.commit()

    async def get_all(self):
        """Все строки без разбора статуса — используется редко, только
        когда реально нужны и подтверждённые тоже."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tracked_flights")
            return await cursor.fetchall()

    async def get_pending(self):
        """Только активные (ещё не подтверждённые) — то, что реально
        отслеживается и показывается с кнопками статуса."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tracked_flights WHERE baggage_status='pending'")
            return await cursor.fetchall()

    async def get_by_chat(self, chat_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tracked_flights WHERE chat_id=? AND baggage_status='pending' ORDER BY id",
                (chat_id,),
            )
            return await cursor.fetchall()

    async def get_by_chat_and_flight(self, chat_id, flight_id):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tracked_flights WHERE chat_id=? AND flight_id=? AND baggage_status='pending'",
                (chat_id, flight_id),
            )
            return await cursor.fetchone()

    async def get_by_chat_and_number(self, chat_id, flight_number):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tracked_flights WHERE chat_id=? AND flight_number=? COLLATE NOCASE "
                "AND baggage_status='pending'",
                (chat_id, flight_number),
            )
            return await cursor.fetchone()

    async def get_by_chat_and_message(self, chat_id, message_id):
        """Строка по chat_id+message_id независимо от статуса — для
        админки, которой нужно достучаться и до уже подтверждённых."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tracked_flights WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            return await cursor.fetchone()

    async def get_recent_confirmed(self, cutoff_iso):
        """Рейсы, которым подтвердили статус багажа не раньше cutoff_iso —
        то, что ещё можно поправить через /admin."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tracked_flights "
                "WHERE baggage_status != 'pending' AND baggage_at >= ? "
                "ORDER BY baggage_at DESC",
                (cutoff_iso,),
            )
            return await cursor.fetchall()

    async def restart_tracking(self, chat_id, message_id):
        """Возвращает рейс в активное отслеживание — сбрасывает статус
        багажа и данные о том, кто/когда его подтвердил."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tracked_flights SET baggage_status='pending', "
                "baggage_user_id=NULL, baggage_username=NULL, baggage_at=NULL "
                "WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            await db.commit()

    async def log_confirmation(self, chat_id, message_id, flight_number, status, user_id, username, confirmed_at):
        """Пишет (или перезаписывает, если уже было) запись в вечный лог
        подтверждений — на нём считается статистика по сменам."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO baggage_log
                (chat_id, message_id, flight_number, baggage_status, user_id, username, confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (chat_id, message_id, flight_number, status, user_id, username, confirmed_at))
            await db.commit()

    async def delete_log_entry(self, chat_id, message_id):
        """Убирает запись из лога — например, если статус был присвоен по
        ошибке и его откатили через «Начать заново» в /admin."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM baggage_log WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            await db.commit()

    async def get_shift_log(self, chat_id, start_iso, end_iso):
        """Все подтверждения в этом чате в границах смены [start, end)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM baggage_log WHERE chat_id=? AND confirmed_at >= ? AND confirmed_at < ? "
                "ORDER BY confirmed_at",
                (chat_id, start_iso, end_iso),
            )
            return await cursor.fetchall()

    async def add_admin(self, user_id, username, added_by, added_at):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO admins (user_id, username, added_by, added_at)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, added_by, added_at))
            await db.commit()

    async def remove_admin(self, user_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
            await db.commit()

    async def is_admin_in_db(self, user_id) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
            return await cursor.fetchone() is not None

    async def list_admins(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admins ORDER BY added_at")
            return await cursor.fetchall()

    async def get_last_stats_message(self, chat_id):
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT message_id FROM last_stats_message WHERE chat_id=?", (chat_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_last_stats_message(self, chat_id, message_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO last_stats_message (chat_id, message_id)
                VALUES (?, ?)
            ''', (chat_id, message_id))
            await db.commit()

    async def get_most_recent_chat_id(self):
        """Chat ID чата, где бот реально используется — чтобы не просить
        задавать его вручную отдельной переменной, если он и так один.
        Сначала смотрим в лог подтверждений (там есть реальная активность),
        если там пусто — берём любой активно отслеживаемый рейс."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT chat_id FROM baggage_log ORDER BY confirmed_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return row[0]

            cursor = await db.execute(
                "SELECT chat_id FROM tracked_flights ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_last_jokes(self, chat_id) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT username, last_joke FROM joke_history WHERE chat_id=?", (chat_id,)
            )
            rows = await cursor.fetchall()
            return {username: last_joke for username, last_joke in rows}

    async def set_last_jokes(self, chat_id, jokes: dict):
        """jokes — {username: сегодняшняя_шутка}. Перезаписывает только тех,
        кто сегодня вообще попал в отчёт — остальные просто не трогаются."""
        if not jokes:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO joke_history (chat_id, username, last_joke) VALUES (?, ?, ?)",
                [(chat_id, username, joke) for username, joke in jokes.items()],
            )
            await db.commit()

    async def purge_old_confirmed(self, cutoff_iso):
        """Чистит записи, которые подтвердили раньше cutoff_iso — чтобы
        база не росла бесконечно. Активные (pending) не трогает никогда."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM tracked_flights WHERE baggage_status != 'pending' AND baggage_at < ?",
                (cutoff_iso,),
            )
            await db.commit()

    async def set_note(self, chat_id, message_id, note):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tracked_flights SET note=? WHERE chat_id=? AND message_id=?",
                (note, chat_id, message_id),
            )
            await db.commit()

    async def delete(self, chat_id, message_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM tracked_flights WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
            await db.commit()
