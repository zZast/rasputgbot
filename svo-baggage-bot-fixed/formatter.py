from models import Flight

def flight_text(flight: Flight, note: str | None = None) -> str:
    """Карточка рейса, пока статус багажа ещё не выбран.

    Одна строка: номер рейса, город отправления, время прилёта, время
    выдачи багажа (оба без подписи, через слэш) и номер ленты в скобках.
    Если задана заметка — второй строкой под основной.
    """
    belt = flight.baggage_belt or "—"
    text = (
        f"✈️ {flight.flight_number} {flight.origin} "
        f"{flight.display_arrival()}/{flight.display_baggage_time()} ({belt})"
    )
    if note:
        text += f"\n📝 {note}"
    return text


def flight_text_minimal(flight: Flight, baggage_status: str, username: str) -> str:
    """Карточка после подтверждения статуса багажа.

    Первая строка — номер рейса и город (чтобы было понятно, о каком рейсе
    речь), вторая — значок статуса и ник того, кто подтвердил.
    """
    icon = "✅" if baggage_status == "received" else "❌"
    return f"✈️ {flight.flight_number} {flight.origin}\n{icon} {username}"
