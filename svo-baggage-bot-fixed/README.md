# SVO Baggage Bot

Telegram-бот для отслеживания прилёта рейса и выдачи багажа в Шереметьево.

## Источник реальных данных

Бот обращается напрямую к публичному timetable endpoint официального сайта Шереметьево:

- `https://www.svo.aero/bitrix/timetable/` — расписание;
- `https://www.svo.aero/bitrix/timetable/{flight_id}/` — данные конкретного рейса.

Это тот же endpoint, который используется неофициальной библиотекой `aero-svo-api`. Данные включают плановое/расчётное/фактическое время, терминал, выход, ленту и начало выдачи багажа, когда эти поля опубликованы аэропортом.

## Запуск

Нужен Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

В `.env`:

```env
BOT_TOKEN=123456:YOUR_TOKEN
MOCK_MODE=false
POLL_INTERVAL=60
DATABASE_PATH=./svo_bot.sqlite3
```

Запуск:

```bash
python bot.py
```

В Telegram:

```text
/start
/track SU1234
```

Можно также искать просто по номеру:

```text
/track 1234
```

## Тестовый режим

Если SVO временно недоступен, для проверки Telegram-части можно поставить:

```env
MOCK_MODE=true
```

По умолчанию теперь используется **реальный режим** (`MOCK_MODE=false`).

## Важно

Официальный сайт не предоставляет этот endpoint как стабильный публичный API. Поэтому интеграция изолирована в `svo_api.py`. Если Шереметьево изменит внутренний endpoint или формат ответа, менять Telegram-логику не потребуется.
