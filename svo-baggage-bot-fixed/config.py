import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    bot_token: str
    mock_mode: bool
    poll_interval: int
    database_path: str
    baggage_topic_id: int | None
    auto_reorder_on_add: bool
    admin_ids: frozenset[int]
    correction_window_hours: float
    report_chat_id: int | None

def get_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    # Если задан — бот работает с рейсами (/track, /list, кнопки,
    # автораспознавание) только в этой теме супергруппы. Если не задан —
    # работает везде.
    topic_id_raw = os.getenv("BAGGAGE_TOPIC_ID", "").strip()
    baggage_topic_id = int(topic_id_raw) if topic_id_raw else None

    # Telegram user ID тех, кому в личке с ботом доступна /admin —
    # исправление последних подтверждённых статусов багажа. Через запятую:
    # ADMIN_IDS=123456789,987654321. Узнать свой ID можно у @userinfobot.
    admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
    admin_ids = frozenset(
        int(part) for part in admin_ids_raw.split(",") if part.strip()
    ) if admin_ids_raw else frozenset()

    return Config(
        bot_token=token,
        mock_mode=os.getenv("MOCK_MODE", "false").lower() in {"1", "true", "yes"},
        poll_interval=max(15, int(os.getenv("POLL_INTERVAL", "60"))),
        database_path=os.getenv("DATABASE_PATH", "./svo_bot.sqlite3"),
        baggage_topic_id=baggage_topic_id,
        # При добавлении нового рейса сразу пересылать все карточки в чате
        # по времени выдачи багажа (как /update, но само). Каждый такой
        # запуск — это заново все карточки чата новыми сообщениями, то есть
        # уведомление у всех на каждое добавление рейса. Если шумно —
        # выключить через AUTO_REORDER_ON_ADD=false, без правки кода.
        auto_reorder_on_add=os.getenv("AUTO_REORDER_ON_ADD", "true").lower() in {"1", "true", "yes"},
        admin_ids=admin_ids,
        # Сколько часов после подтверждения статуса багажа его ещё можно
        # исправить через /admin в личке. По истечении — запись тихо
        # удаляется фоновым циклом, исправить будет уже нельзя.
        correction_window_hours=float(os.getenv("CORRECTION_WINDOW_HOURS", "4")),
        # Chat ID супергруппы, куда в 08:00 по Мск сам улетает отчёт по
        # статистике за прошедшую смену. Если не задан — автоотчёт просто
        # не работает (без ошибок, тихо выключен). Это ID чата, а не темы —
        # тема берётся отдельно из BAGGAGE_TOPIC_ID.
        report_chat_id=(
            int(os.getenv("REPORT_CHAT_ID", "").strip())
            if os.getenv("REPORT_CHAT_ID", "").strip() else None
        ),
    )
