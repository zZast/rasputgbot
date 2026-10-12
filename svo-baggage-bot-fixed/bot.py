from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat
from aiogram.exceptions import TelegramBadRequest

from config import get_config
from database import Database
from formatter import flight_text, flight_text_minimal
from svo_api import SVOClient
from stats import previous_shift_bounds, render_shift_stats

logging.basicConfig(level=logging.INFO)

cfg = get_config()
db = Database(cfg.database_path)
svo = SVOClient(mock=cfg.mock_mode)
dp = Dispatcher(storage=MemoryStorage())


class TrackFlight(StatesGroup):
    waiting_number = State()


def keyboard():
    """Клавиатура под карточкой рейса, пока статус багажа ещё не выбран.

    После подтверждения статуса карточка сворачивается до минимума и
    клавиатура убирается совсем — см. baggage_callback.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧳 Багаж получен", callback_data="bag:received"),
            InlineKeyboardButton(text="❌ Багажа нет", callback_data="bag:missing"),
        ],
    ])


def list_keyboard(rows):
    buttons = [
        [InlineKeyboardButton(text=f"❌ Удалить {r['flight_number']}", callback_data=f"untrack:{r['message_id']}")]
        for r in rows
    ]
    buttons.append([InlineKeyboardButton(text="🔄 Сортировать по багажу", callback_data="sortedlist")])
    buttons.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="closelist")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def list_text(rows):
    if not rows:
        return "Нет отслеживаемых рейсов в этом чате."
    lines = "\n".join(f"• {r['flight_number']}" for r in rows)
    return f"📋 <b>Отслеживаемые рейсы:</b>\n\n{lines}\n\nНажмите на кнопку, чтобы удалить рейс."


async def sorted_by_baggage_time(chat_id):
    """Тянет живые данные по всем отслеживаемым рейсам чата и сортирует их
    по времени выдачи багажа. Рейсы, по которым SVO сейчас не отвечает,
    уходят в конец списка. Возвращает список пар (запись_из_бд, Flight|None).
    """
    rows = await db.get_by_chat(chat_id)
    if not rows:
        return []
    flights = await asyncio.gather(*(svo.get_flight(r["flight_id"], r["flight_number"]) for r in rows))
    paired = list(zip(rows, flights))
    paired.sort(key=lambda item: (
        item[1] is None or item[1].baggage_time is None,
        item[1].baggage_time if item[1] and item[1].baggage_time else datetime.max,
    ))
    return paired


def sorted_list_text(paired):
    if not paired:
        return "Нет отслеживаемых рейсов в этом чате."
    lines = []
    for row, flight in paired:
        time_str = flight.display_baggage_time() if flight else "—"
        note = f" · 📝 {row['note']}" if row["note"] else ""
        lines.append(f"• {row['flight_number']} — {time_str}{note}")
    return (
        "📋 <b>Отслеживаемые рейсы (по времени выдачи багажа):</b>\n\n"
        + "\n".join(lines)
        + "\n\nНажмите на кнопку, чтобы удалить рейс."
    )


async def _safe_delete(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass


NOT_FOUND_MESSAGE_TTL = 60  # сек — через сколько само удалится сообщение об ошибке поиска

# Москва — фиксированный UTC+3, без перехода на летнее/зимнее время с 2014
# года, поэтому обычное фиксированное смещение надёжнее и проще, чем тянуть
# IANA tzdata (её может не быть на сервере). Сервер (Amvera) при этом может
# тикать в любом своём часовом поясе (чаще всего UTC) — отсюда и была путаница
# со временем в карточках/статистике/окне исправлений.
MSK = timezone(timedelta(hours=3))


def msk_now() -> datetime:
    """Текущее время как "наивный" (без tzinfo) datetime, но посчитанное
    по московскому времени — независимо от часового пояса самого сервера.
    Везде в боте, где раньше было datetime.now(), используем эту функцию.
    """
    return datetime.now(MSK).replace(tzinfo=None)


def _schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: float = NOT_FOUND_MESSAGE_TTL):
    """Удаляет сообщение через delay секунд, если пользователь его так и оставил висеть.

    Если сообщение уже удалено раньше (например, пользователь прислал новый
    номер рейса и мы стёрли старую попытку) — просто ничего не произойдёт,
    _safe_delete тихо проглотит ошибку "сообщение не найдено".
    """
    async def _later():
        await asyncio.sleep(delay)
        await _safe_delete(bot, chat_id, message_id)

    asyncio.create_task(_later())


async def safe_answer(callback: CallbackQuery, *args, **kwargs):
    """Отвечает на callback, но не падает, если запрос уже устарел.

    Telegram отклоняет ответ на callback_query, если прошло слишком много
    времени (например, бот был недоступен и разгребает накопившуюся
    очередь). Такие случаи не являются багом — их можно просто игнорировать.
    """
    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "query is too old" in err or "query id is invalid" in err:
            logging.info("Пропускаю устаревший callback: %s", e)
        else:
            raise


async def _append_note(bot: Bot, chat_id: int, message_id: int, flight_id: str, flight_number: str,
                        current_note: str | None, new_note_part: str) -> str:
    """Дописывает new_note_part к уже существующей заметке (через '; '), сохраняет
    и обновляет карточку. Если заметки ещё не было — просто ставит новую.
    Возвращает итоговый текст заметки.
    """
    combined = f"{current_note}; {new_note_part}" if current_note else new_note_part
    await db.set_note(chat_id, message_id, combined)
    flight = await svo.get_flight(flight_id, flight_number)
    if flight:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=flight_text(flight, combined),
                reply_markup=keyboard(),
            )
        except TelegramBadRequest:
            pass
    return combined


async def send_flight_card(
    bot: Bot,
    chat_id: int,
    flight_number: str,
    message_thread_id: int | None = None,
    state: FSMContext | None = None,
    notify_not_found: bool = True,
    target_date: date | None = None,
    note: str | None = None,
):
    """Ищет рейс и публикует компактную карточку. Возвращает True, если рейс найден.

    Если рейс не нашёлся (или SVO недоступен) и передан state — бот сам
    переводит диалог в режим ожидания номера рейса, чтобы пользователь мог
    просто прислать номер ещё раз, без повторного нажатия кнопки.

    notify_not_found=False — для фонового автораспознавания номера рейса по
    обычному тексту: если это оказался не рейс (или SVO недоступен), молча
    убираем плейсхолдер без сообщения об ошибке — иначе на любое похожее по
    формату слово в чате бот бы писал «рейс не найден», что выглядело бы
    как спам.
    """
    placeholder = await bot.send_message(chat_id, "🔍 Ищу рейс…", message_thread_id=message_thread_id)

    try:
        flight = await svo.find_flight(flight_number, target_date=target_date)
    except Exception:
        logging.exception("SVO search error")
        if not notify_not_found:
            await _safe_delete(bot, chat_id, placeholder.message_id)
            return False
        await placeholder.edit_text(
            "⚠️ Не удалось получить данные Шереметьево. Попробуйте ещё раз — пришлите номер рейса:"
        )
        _schedule_delete(bot, chat_id, placeholder.message_id)
        if state is not None:
            await state.set_state(TrackFlight.waiting_number)
            await state.update_data(prompt_message_id=placeholder.message_id)
        return False
    if not flight:
        if not notify_not_found:
            await _safe_delete(bot, chat_id, placeholder.message_id)
            return False
        await placeholder.edit_text("Рейс не найден. Проверьте номер и пришлите его ещё раз:")
        _schedule_delete(bot, chat_id, placeholder.message_id)
        if state is not None:
            await state.set_state(TrackFlight.waiting_number)
            await state.update_data(prompt_message_id=placeholder.message_id)
        return False

    existing = await db.get_by_chat_and_flight(chat_id, flight.flight_id)
    if existing:
        if note:
            combined = await _append_note(
                bot, chat_id, existing["message_id"], flight.flight_id, flight.flight_number,
                existing["note"], note,
            )
            await placeholder.edit_text(f"✈️ {flight.flight_number} уже отслеживается — заметка обновлена: {combined}")
        else:
            await placeholder.edit_text(f"✈️ {flight.flight_number} уже отслеживается в этом чате.")
        _schedule_delete(bot, chat_id, placeholder.message_id)
        if state is not None:
            await state.clear()
        return False

    await placeholder.edit_text(flight_text(flight, note), reply_markup=keyboard())
    await db.add(chat_id, placeholder.message_id, flight.flight_id, flight.flight_number, note)
    if state is not None:
        await state.clear()

    if cfg.auto_reorder_on_add:
        new_row = await db.get_by_chat_and_flight(chat_id, flight.flight_id)
        asyncio.create_task(_smart_reorder_on_add_safe(bot, chat_id, message_thread_id, new_row, flight))

    return True


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "✈️ <b>SVO Baggage Bot</b>\n\n"
        "Отследить рейс: <code>/track SU1234</code> или кнопкой «Добавить рейс» под карточкой.\n"
        "Список отслеживаемых рейсов: <code>/list</code>\n\n"
        "Работает и в группах — любой участник может добавить рейс, "
        "и он начнёт отслеживаться для всего чата.\n\n"
        f"Режим: {'MOCK' if cfg.mock_mode else 'SVO'}"
    )


@dp.message(Command("track"))
async def track(message: Message, state: FSMContext):
    if not _topic_allowed(message):
        await message.answer("Отслеживание рейсов работает только в теме для багажа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        hint = await message.answer(
            "Использование: <code>/track SU1234</code>, "
            "<code>/track SU1234 18.09</code> — если рейс завтра/другим днём, "
            "или <code>/track SU1234 Транзит в Сочи</code> — с заметкой."
        )
        _schedule_delete(message.bot, message.chat.id, hint.message_id)
        await _safe_delete(message.bot, message.chat.id, message.message_id)
        return

    parsed = parse_track_query(parts[1])
    if not parsed:
        hint = await message.answer(
            "Не понял номер рейса. Пример: <code>/track SU1234</code> "
            "или <code>/track SU1234 18.09 Транзит в Сочи</code>"
        )
        _schedule_delete(message.bot, message.chat.id, hint.message_id)
        await _safe_delete(message.bot, message.chat.id, message.message_id)
        return
    flight_number, target_date, note = parsed

    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await send_flight_card(
        message.bot, message.chat.id, flight_number, message.message_thread_id, state,
        target_date=target_date, note=note,
    )


@dp.message(Command("list"))
async def list_flights(message: Message):
    if not _topic_allowed(message):
        await message.answer("Отслеживание рейсов работает только в теме для багажа.")
        return

    await _safe_delete(message.bot, message.chat.id, message.message_id)
    rows = await db.get_by_chat(message.chat.id)
    if not rows:
        return
    await message.answer(list_text(rows), reply_markup=list_keyboard(rows))


async def _smart_reorder_on_add(
    bot: Bot, chat_id: int, message_thread_id: int | None,
    new_row: "sqlite3.Row", new_flight: "Flight | None",
):
    """"Умная" перестановка для автосортировки при добавлении рейса: в
    отличие от /update (который пересобирает вообще всё), трогает только
    те карточки, чей порядок реально должен измениться из-за нового рейса.

    Карточки уже отсортированы в чате (это поддерживается каждым вызовом
    этой функции и /update) — поэтому порядок их появления в чате совпадает
    с порядком message_id. Ищем в этом уже отсортированном списке точку, куда
    по времени выдачи багажа встаёт новый рейс: всё, что в чате оказалось бы
    физически ПОСЛЕ него, нужно сдвинуть (пересослать), а всё, что раньше —
    не трогаем вообще, там и так всё на своих местах.
    """
    rows = await db.get_by_chat(chat_id)
    existing_rows = [r for r in rows if r["message_id"] != new_row["message_id"]]
    if not existing_rows:
        return  # это первый отслеживаемый рейс в чате — переставлять нечего

    existing_rows.sort(key=lambda r: r["message_id"])  # текущий порядок в чате
    flights = await asyncio.gather(*(svo.get_flight(r["flight_id"], r["flight_number"]) for r in existing_rows))
    paired_existing = list(zip(existing_rows, flights))

    new_time = new_flight.baggage_time if new_flight and new_flight.baggage_time else None

    insert_index = len(paired_existing)  # по умолчанию — в самый конец (где он физически уже лежит)
    if new_time is not None:
        for i, (_, flight) in enumerate(paired_existing):
            existing_time = flight.baggage_time if flight and flight.baggage_time else None
            if existing_time is None or new_time < existing_time:
                insert_index = i
                break

    tail = paired_existing[insert_index:]
    if not tail:
        return  # новый рейс и так встаёт в конец — он там уже лежит, трогать нечего

    # Хвост, который нужно пересобрать: новый рейс + всё, что должно оказаться после него.
    to_resend = [(new_row, new_flight)] + tail

    for row, flight in to_resend:
        if flight is None:
            continue
        try:
            new_message = await bot.send_message(
                chat_id,
                flight_text(flight, row["note"]),
                reply_markup=keyboard(),
                message_thread_id=message_thread_id,
            )
        except TelegramBadRequest as e:
            logging.warning("Smart reorder: не удалось переслать карточку %s: %s", row["flight_number"], e)
            continue

        still_tracked = await db.get_by_chat_and_flight(chat_id, row["flight_id"])
        if not still_tracked or still_tracked["message_id"] != row["message_id"]:
            await _safe_delete(bot, chat_id, new_message.message_id)
            continue

        await _safe_delete(bot, chat_id, row["message_id"])
        await db.delete(chat_id, row["message_id"])
        await db.add(chat_id, new_message.message_id, row["flight_id"], row["flight_number"], row["note"])


async def _smart_reorder_on_add_safe(bot: Bot, chat_id: int, message_thread_id, new_row, new_flight):
    try:
        await _smart_reorder_on_add(bot, chat_id, message_thread_id, new_row, new_flight)
    except Exception:
        logging.exception("Умная автосортировка при добавлении рейса упала")


async def _reorder_cards(bot: Bot, chat_id: int, message_thread_id: int | None = None) -> tuple[int, int]:
    """Физически переставляет карточки рейсов в чате по времени выдачи
    багажа. Telegram не даёт двигать существующие сообщения — поэтому
    каждая карточка пересылается заново (в нужном порядке), а старая
    удаляется. Рейсы, по которым SVO сейчас не ответил, не трогаем —
    остаются на своём месте, чтобы не потерять карточку зря.

    Используется и командой /update, и автоматически при добавлении нового
    рейса (если это включено в конфиге) — логика общая, чтобы не разъезжалась.
    Возвращает (сколько переставлено, сколько пропущено).
    """
    paired = await sorted_by_baggage_time(chat_id)
    if not paired:
        return 0, 0

    moved = 0
    skipped = 0
    for row, flight in paired:
        if flight is None:
            skipped += 1
            continue
        try:
            new_message = await bot.send_message(
                chat_id,
                flight_text(flight, row["note"]),
                reply_markup=keyboard(),
                message_thread_id=message_thread_id,
            )
        except TelegramBadRequest as e:
            logging.warning("Reorder: не удалось переслать карточку %s: %s", row["flight_number"], e)
            skipped += 1
            continue

        # Пока мы пересылали карточку, статус багажа могли успеть подтвердить
        # (или рейс мог сняться с отслеживания иначе) — тогда старой записи
        # в базе уже нет, и заново резервировать рейс не нужно, иначе он
        # "воскреснет" отслеживаемым сразу после того, как его подтвердили.
        still_tracked = await db.get_by_chat_and_flight(chat_id, row["flight_id"])
        if not still_tracked or still_tracked["message_id"] != row["message_id"]:
            await _safe_delete(bot, chat_id, new_message.message_id)
            skipped += 1
            continue

        # Сначала убираем старую карточку из чата — и только потом
        # переключаем базу на новую. Если сделать в обратном порядке, между
        # шагами будет момент, когда старая карточка ещё видна в чате, но
        # бот по ней уже отвечает "рейс не найден" — это и чинит перестановку.
        await _safe_delete(bot, chat_id, row["message_id"])
        await db.delete(chat_id, row["message_id"])
        await db.add(chat_id, new_message.message_id, row["flight_id"], row["flight_number"], row["note"])
        moved += 1

    return moved, skipped


async def _reorder_cards_safe(bot: Bot, chat_id: int, message_thread_id: int | None = None):
    """Запускает _reorder_cards в фоне (через create_task), чтобы не
    заставлять человека, который только что добавил рейс, ждать, пока
    переставятся все карточки в чате — это может занять много секунд.
    Ошибки просто логируем, чтобы фон не падал молча.
    """
    try:
        await _reorder_cards(bot, chat_id, message_thread_id)
    except Exception:
        logging.exception("Автосортировка при добавлении рейса упала")


@dp.message(Command("update"))
async def update_sorted_list(message: Message):
    if not _topic_allowed(message):
        await message.answer("Отслеживание рейсов работает только в теме для багажа.")
        return

    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await _reorder_cards(message.bot, message.chat.id, message.message_thread_id)


@dp.callback_query(F.data == "sortedlist")
async def sortedlist_callback(callback: CallbackQuery):
    paired = await sorted_by_baggage_time(callback.message.chat.id)
    if not paired:
        await safe_answer(callback, "Нет отслеживаемых рейсов в этом чате.", show_alert=True)
        return
    sorted_rows = [row for row, _ in paired]
    try:
        await callback.message.edit_text(sorted_list_text(paired), reply_markup=list_keyboard(sorted_rows))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await safe_answer(callback)


@dp.callback_query(F.data == "listflights")
async def listflights_callback(callback: CallbackQuery):
    rows = await db.get_by_chat(callback.message.chat.id)
    if not rows:
        await safe_answer(callback, "Нет отслеживаемых рейсов в этом чате.", show_alert=True)
        return
    await callback.message.answer(list_text(rows), reply_markup=list_keyboard(rows))
    await safe_answer(callback)


@dp.callback_query(F.data == "closelist")
async def closelist_callback(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await safe_answer(callback)


@dp.callback_query(F.data == "addflight")
async def addflight_callback(callback: CallbackQuery, state: FSMContext):
    if not _topic_allowed(callback.message):
        await safe_answer(callback, "Отслеживание рейсов работает только в теме для багажа.", show_alert=True)
        return
    prompt = await callback.message.answer(
        "Введите номер рейса, например <code>SU1234</code>"
    )
    await state.set_state(TrackFlight.waiting_number)
    await state.update_data(prompt_message_id=prompt.message_id)
    await safe_answer(callback)


@dp.message(StateFilter(TrackFlight.waiting_number))
async def addflight_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")

    raw_text = (message.text or "").strip()

    # Убираем всё лишнее — остаётся только карточка рейса (или новая попытка).
    if prompt_message_id:
        await _safe_delete(message.bot, message.chat.id, prompt_message_id)
    await _safe_delete(message.bot, message.chat.id, message.message_id)

    if not raw_text:
        await state.clear()
        return

    # FSM-состояние не привязано к конкретной теме супергруппы — если
    # пользователь нажал «Добавить рейс» в теме для багажа, а номер рейса
    # прислал уже в другой теме, не публикуем карточку не там, где нужно.
    if not _topic_allowed(message):
        await state.clear()
        return

    parsed = parse_track_query(raw_text)
    if not parsed:
        await state.clear()
        return
    flight_number, target_date, note = parsed

    # send_flight_card сам решит: если рейс снова не найдётся — состояние
    # ожидания номера останется активным, и пользователь сможет просто
    # прислать номер ещё раз без повторного нажатия кнопки.
    await send_flight_card(
        message.bot, message.chat.id, flight_number, message.message_thread_id, state,
        target_date=target_date, note=note,
    )


# Похоже на номер рейса: 2 символа кода авиакомпании (буквы и/или цифры,
# например SU, U6, DP, 5N) + 1-4 цифры номера, без пробела или с одним.
FLIGHT_NUMBER_RE = re.compile(r"^[A-ZА-Я0-9]{2}\s?-?\d{1,4}$", re.IGNORECASE)

# То же самое, но с датой через пробел: "SU1701 18.09" или "SU1701 18.09.2025".
# Дата — прилёт нужного дня, а не ближайший по времени рейс с этим номером
# (актуально для регулярных рейсов, которые летают каждый день под одним
# и тем же номером).
FLIGHT_WITH_DATE_RE = re.compile(
    r"^([A-ZА-Я0-9]{2}\s?-?\d{1,4})[\s,]+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$",
    re.IGNORECASE,
)


def parse_flight_query(text: str) -> tuple[str, "date | None"] | None:
    """Разбирает "SU1701" или "SU1701 18.09[.2025]" в (номер, дата|None).

    Возвращает None, если строка вообще не похожа на запрос рейса.
    """
    text = text.strip()

    m = FLIGHT_WITH_DATE_RE.match(text)
    if m:
        flight_number, day_s, month_s, year_s = m.groups()
        try:
            day, month = int(day_s), int(month_s)
        except ValueError:
            return None
        now = msk_now()
        if year_s:
            year = int(year_s)
            if year < 100:
                year += 2000
        else:
            year = now.year
        try:
            target = date(year, month, day)
        except ValueError:
            return None
        # Год не указан явно и дата уже прилично в прошлом — скорее всего,
        # имелся в виду тот же день, но в следующем году (актуально в
        # декабре-январе: "05.01" в декабре обычно значит январь next year).
        if not year_s and target < now.date() - timedelta(days=2):
            try:
                target = date(year + 1, month, day)
            except ValueError:
                pass
        return flight_number, target

    if FLIGHT_NUMBER_RE.match(text):
        return text, None

    return None


# То же самое, что FLIGHT_WITH_DATE_RE / FLIGHT_NUMBER_RE, но с необязательной
# произвольной заметкой в хвосте: "SU1315 Транзит в Сочи" или
# "SU1315 21.09 Транзит в Сочи". Используется только там, где пользователь
# явно ввёл команду/номер сам (/track, кнопка «Добавить рейс») — в
# автораспознавании обычного текста (autodetect_flight) это не участвует,
# чтобы бот не принимал случайный текст рядом с похожим словом за заметку.
FLIGHT_DATE_NOTE_RE = re.compile(
    r"^([A-ZА-Я0-9]{2}\s?-?\d{1,4})[\s,]+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?(?:\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)
FLIGHT_NOTE_RE = re.compile(r"^([A-ZА-Я0-9]{2}\s?-?\d{1,4})(?:\s+(.+))?$", re.IGNORECASE | re.DOTALL)


def parse_track_query(text: str) -> tuple[str, "date | None", "str | None"] | None:
    """Разбирает ввод команды /track (или кнопки «Добавить рейс»):
    "SU1701", "SU1701 18.09", "SU1701 Транзит в Сочи" или
    "SU1701 18.09 Транзит в Сочи" — в (номер, дата|None, заметка|None).

    Возвращает None, если строка вообще не похожа на запрос рейса.
    """
    text = text.strip()

    m = FLIGHT_DATE_NOTE_RE.match(text)
    if m:
        flight_number, day_s, month_s, year_s, note = m.groups()
        try:
            day, month = int(day_s), int(month_s)
        except ValueError:
            return None
        now = msk_now()
        if year_s:
            year = int(year_s)
            if year < 100:
                year += 2000
        else:
            year = now.year
        try:
            target = date(year, month, day)
        except ValueError:
            return None
        if not year_s and target < now.date() - timedelta(days=2):
            try:
                target = date(year + 1, month, day)
            except ValueError:
                pass
        return flight_number, target, (note.strip() if note else None)

    m = FLIGHT_NOTE_RE.match(text)
    if m:
        flight_number, note = m.groups()
        return flight_number, None, (note.strip() if note else None)

    return None


def _topic_allowed(message: Message) -> bool:
    """Разрешено ли работать с рейсами в этом чате/теме.

    Если BAGGAGE_TOPIC_ID не задан — работает везде. Если задан — только в
    этой теме внутри супергруппы; в личных чатах и обычных группах (где тем
    не бывает) ограничение не действует, чтобы не ломать тесты в личке.
    Действует на всё: /track, /list, кнопку «Добавить рейс» и
    автораспознавание номера рейса по обычному тексту.
    """
    if cfg.baggage_topic_id is None:
        return True
    if message.chat.type not in ("supergroup", "group"):
        return True
    return message.message_thread_id == cfg.baggage_topic_id


# Триггер автораспознавания. В отличие от FLIGHT_NUMBER_RE/FLIGHT_WITH_DATE_RE,
# хвост-заметка тут разрешена: "SU1315 срочно забрать" тоже должно долетать до
# хендлера. Сам риск ложных срабатываний на случайный текст (не рейс) гасится
# внутри хендлера: заметку без /track можно приставить только к уже
# отслеживаемому рейсу, а не завести новое отслеживание из случайной фразы.
AUTODETECT_RE = re.compile(
    r"^[A-ZА-Я0-9]{2}\s?-?\d{1,4}(?:[\s,]+\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)?(?:\s+.+)?$",
    re.IGNORECASE | re.DOTALL,
)


@dp.message(F.text.regexp(AUTODETECT_RE))
async def autodetect_flight(message: Message):
    """Ловит номер рейса, присланный обычным текстом, без /track и без кнопки.
    Понимает "SU1701", "SU1701 18.09" и "SU1701 18.09 срочно забрать".

    Если в конце есть произвольный текст (заметка) — он применяется, только
    если этот рейс уже отслеживается в этом чате. Иначе, чтобы не заводить
    новое отслеживание из случайной фразы вроде "PS5 наконец обновили
    прошивку", бот такое сообщение просто игнорирует.

    Молчит, если это не оказалось рейсом (см. notify_not_found=False) —
    иначе на любое случайно похожее слово в чате бот бы отвечал «не найден».
    """
    if not _topic_allowed(message):
        return
    parsed = parse_track_query(message.text)
    if not parsed:
        return
    flight_number, target_date, note = parsed

    if note:
        existing = await db.get_by_chat_and_number(message.chat.id, flight_number)
        if not existing:
            return  # не отслеживается — молчим, не заводим новый трек из случайной фразы
        await _append_note(
            message.bot, message.chat.id, existing["message_id"],
            existing["flight_id"], existing["flight_number"], existing["note"], note,
        )
        await _safe_delete(message.bot, message.chat.id, message.message_id)
        return

    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await send_flight_card(
        message.bot,
        message.chat.id,
        flight_number,
        message.message_thread_id,
        state=None,
        notify_not_found=False,
        target_date=target_date,
    )


@dp.callback_query(F.data.startswith("untrack:"))
async def untrack_callback(callback: CallbackQuery):
    message_id = int(callback.data.split(":", 1)[1])
    chat_id = callback.message.chat.id

    await db.delete(chat_id, message_id)
    await safe_answer(callback, "Рейс удалён из отслеживания")

    # Удаляем исходное сообщение с рейсом, раз рейс сняли с отслеживания.
    await _safe_delete(callback.bot, chat_id, message_id)

    rows = await db.get_by_chat(chat_id)
    if not rows:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        return
    try:
        await callback.message.edit_text(list_text(rows), reply_markup=list_keyboard(rows))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


def _format_baggage_at(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return datetime.fromisoformat(raw).strftime("%d.%m %H:%M")
    except ValueError:
        return raw


async def is_admin(user_id: int) -> bool:
    """Админ — либо из ADMIN_IDS (сид-список в переменных окружения, его
    нельзя снять через бота), либо добавлен через /addadmin (хранится в БД)."""
    if user_id in cfg.admin_ids:
        return True
    return await db.is_admin_in_db(user_id)


def admin_entry_keyboard(chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Начать заново", callback_data=f"admin_restart:{chat_id}:{message_id}"),
        InlineKeyboardButton(text="✅ Получен", callback_data=f"admin_set:received:{chat_id}:{message_id}"),
        InlineKeyboardButton(text="❌ Нет багажа", callback_data=f"admin_set:missing:{chat_id}:{message_id}"),
    ]])


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    """Админка для исправления статуса багажа — работает только в личке
    с ботом и только для ID из ADMIN_IDS. Показывает рейсы, которым
    подтвердили статус за последние CORRECTION_WINDOW_HOURS часов, с
    кнопками, чтобы откатить/поправить прямо из личных сообщений.
    """
    if message.chat.type != "private":
        return  # в группах админку не показываем вообще, тихо игнорируем
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    cutoff = (msk_now() - timedelta(hours=cfg.correction_window_hours)).isoformat()
    rows = await db.get_recent_confirmed(cutoff)
    if not rows:
        await message.answer(
            f"За последние {cfg.correction_window_hours:g} ч. подтверждений багажа не было."
        )
        return

    for row in rows:
        status_label = "✅ Получен" if row["baggage_status"] == "received" else "❌ Нет багажа"
        text = (
            f"✈️ {row['flight_number']}\n"
            f"Статус: {status_label}\n"
            f"Подтвердил: {row['baggage_username'] or '—'}\n"
            f"Когда: {_format_baggage_at(row['baggage_at'])}"
        )
        await message.answer(text, reply_markup=admin_entry_keyboard(row["chat_id"], row["message_id"]))


async def _register_admin_command_scope(bot: Bot, user_id: int):
    """Показывает полный набор команд бота (а не только /admin) в личке
    конкретного админа. Назначение чат-специфичного списка команд полностью
    ПЕРЕКРЫВАЕТ общий список для этого чата — поэтому сюда включается всё
    сразу, а не только админские, иначе /track и другие пропадут из
    подсказок в личке у этого человека."""
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="track", description="Отследить рейс, например /track SU1234"),
                BotCommand(command="list", description="Список отслеживаемых рейсов"),
                BotCommand(command="update", description="Список рейсов по времени выдачи багажа"),
                BotCommand(command="admin", description="Исправить последние статусы багажа"),
                BotCommand(command="addadmin", description="Добавить админа по его Telegram ID"),
                BotCommand(command="removeadmin", description="Убрать админа по его Telegram ID"),
                BotCommand(command="admins", description="Список админов"),
                BotCommand(command="testdailystats", description="Прислать отчёт по смене прямо сейчас (тест)"),
            ],
            scope=BotCommandScopeChat(chat_id=user_id),
        )
    except Exception:
        logging.exception("Не удалось зарегистрировать команды в личке для %s", user_id)


@dp.message(Command("addadmin"))
async def addadmin_cmd(message: Message):
    if message.chat.type != "private":
        return
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.answer(
            "Использование: <code>/addadmin 123456789</code> — Telegram ID нового админа "
            "(узнать свой/чужой ID можно у @userinfobot)."
        )
        return

    new_admin_id = int(parts[1].strip())
    if new_admin_id in cfg.admin_ids:
        await message.answer("Этот человек и так в сид-списке ADMIN_IDS, добавлять не нужно.")
        return

    await db.add_admin(new_admin_id, None, message.from_user.id, msk_now().isoformat())
    await message.answer(f"Готово, {new_admin_id} теперь админ.")
    # Чтобы у нового админа сразу были все команды в подсказках, а не только
    # после следующего перезапуска бота (там это тоже произойдёт само).
    await _register_admin_command_scope(message.bot, new_admin_id)


@dp.message(Command("removeadmin"))
async def removeadmin_cmd(message: Message):
    if message.chat.type != "private":
        return
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.answer("Использование: <code>/removeadmin 123456789</code>")
        return

    target_id = int(parts[1].strip())
    if target_id in cfg.admin_ids:
        await message.answer(
            "Этот ID задан через ADMIN_IDS в переменных окружения Amvera — "
            "через бота его снять нельзя, нужно поправить переменную и пересобрать проект."
        )
        return

    await db.remove_admin(target_id)
    await message.answer(f"Готово, {target_id} больше не админ.")


@dp.message(Command("admins"))
async def list_admins_cmd(message: Message):
    if message.chat.type != "private":
        return
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    lines = [f"• {admin_id} (ADMIN_IDS, задан в переменных окружения)" for admin_id in sorted(cfg.admin_ids)]
    db_admins = await db.list_admins()
    for row in db_admins:
        label = row["username"] or str(row["user_id"])
        lines.append(f"• {label} (добавлен через /addadmin)")

    if not lines:
        await message.answer("Админов пока нет вообще — это странно, раз вы смогли сюда написать.")
        return
    await message.answer("👤 <b>Админы:</b>\n\n" + "\n".join(lines))


@dp.message(Command("testdailystats"))
async def test_daily_stats_cmd(message: Message):
    """Для проверки, что автоотчёт по статистике смены реально работает —
    запускает ровно ту же отправку, что происходит сама в 08:00, прямо
    сейчас, не дожидаясь расписания. Только в личке, только для админов.
    """
    if message.chat.type != "private":
        return
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    await message.answer("Отправляю тестовый отчёт…")
    result = await send_daily_stats_report(message.bot)
    await message.answer(result)


@dp.callback_query(F.data.startswith("admin_restart:"))
async def admin_restart_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", show_alert=True)
        return

    _, chat_id_s, message_id_s = callback.data.split(":")
    chat_id, message_id = int(chat_id_s), int(message_id_s)

    row = await db.get_by_chat_and_message(chat_id, message_id)
    if not row:
        await safe_answer(callback, "Запись уже не найдена — возможно, истекло время исправления.", show_alert=True)
        return

    await db.restart_tracking(chat_id, message_id)
    # Подтверждение было ошибочным — убираем его и из статистики смены тоже.
    await db.delete_log_entry(chat_id, message_id)

    flight = await svo.get_flight(row["flight_id"], row["flight_number"])
    if flight:
        try:
            await callback.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=flight_text(flight, row["note"]),
                reply_markup=keyboard(),
            )
        except TelegramBadRequest as e:
            logging.warning("Admin restart: не удалось обновить карточку %s: %s", row["flight_number"], e)

    await safe_answer(callback, "Рейс возвращён в отслеживание")
    try:
        await callback.message.edit_text(callback.message.text + "\n\n↩️ Возвращено в отслеживание.", reply_markup=None)
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("admin_set:"))
async def admin_set_callback(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await safe_answer(callback, "Нет доступа.", show_alert=True)
        return

    _, status, chat_id_s, message_id_s = callback.data.split(":")
    chat_id, message_id = int(chat_id_s), int(message_id_s)

    row = await db.get_by_chat_and_message(chat_id, message_id)
    if not row:
        await safe_answer(callback, "Запись уже не найдена — возможно, истекло время исправления.", show_alert=True)
        return

    flight = await svo.get_flight(row["flight_id"], row["flight_number"])
    if not flight:
        await safe_answer(callback, "Не удалось получить данные рейса от SVO, попробуйте позже.", show_alert=True)
        return

    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    confirmed_at = msk_now().isoformat()
    await db.set_baggage_status(chat_id, message_id, status, callback.from_user.id, admin_name, confirmed_at)
    # Поправка через админку перезаписывает запись в логе статистики смены
    # (та же пара chat_id+message_id) — не задваивает рейс в счётчике.
    await db.log_confirmation(chat_id, message_id, row["flight_number"], status, callback.from_user.id, admin_name, confirmed_at)

    try:
        await callback.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=flight_text_minimal(flight, status, admin_name),
            reply_markup=None,
        )
    except TelegramBadRequest as e:
        logging.warning("Admin set: не удалось обновить карточку %s: %s", row["flight_number"], e)

    await safe_answer(callback, "Статус исправлен")
    status_label = "✅ Получен" if status == "received" else "❌ Нет багажа"
    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n✏️ Исправлено на: {status_label} (вы)", reply_markup=None,
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.in_({"bag:received", "bag:missing"}))
async def baggage_callback(callback: CallbackQuery):
    status = "received" if callback.data == "bag:received" else "missing"
    username = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name

    rows = await db.get_pending()
    target = next(
        (r for r in rows
         if r["chat_id"] == callback.message.chat.id
         and r["message_id"] == callback.message.message_id),
        None
    )

    if not target:
        await safe_answer(callback, "Рейс не найден.", show_alert=True)
        return

    flight = await svo.get_flight(target["flight_id"], target["flight_number"])
    if not flight:
        await safe_answer(callback, "Рейс не найден.", show_alert=True)
        return

    # Статус подтверждён — сворачиваем карточку до минимума (рейс,
    # авиакомпания, город отправления, кто подтвердил) и убираем кнопки:
    # дальше с этой карточкой уже нечего делать.
    await callback.message.edit_text(
        flight_text_minimal(flight, status, username),
        reply_markup=None,
    )
    await safe_answer(callback, "Статус обновлён")

    # Рейс больше не опрашивается SVO и не участвует в /list или сортировке,
    # но запись НЕ удаляется сразу — несколько часов (CORRECTION_WINDOW_HOURS)
    # её ещё можно поправить через /admin в личке с ботом, если статус
    # присвоили случайно/ошибочно. Чистит такие записи фоновый updater().
    confirmed_at = msk_now().isoformat()
    await db.set_baggage_status(
        callback.message.chat.id, callback.message.message_id,
        status, callback.from_user.id, username, confirmed_at,
    )
    # Отдельный вечный лог для статистики по сменам — не трогается чисткой
    # окна исправлений.
    await db.log_confirmation(
        callback.message.chat.id, callback.message.message_id,
        target["flight_number"], status, callback.from_user.id, username, confirmed_at,
    )


async def updater(bot):
    # Ошибки, при которых карточку уже никогда не получится обновить —
    # чат/сообщение исчезли не по нашей вине. В этих случаях просто
    # вычищаем запись из БД, чтобы не пытаться снова каждый цикл.
    DEAD_ROW_MARKERS = (
        "chat not found",
        "message to edit not found",
        "bot was kicked",
        "bot was blocked",
        "have no rights",
        "user is deactivated",
    )
    while True:
        try:
            cutoff = (msk_now() - timedelta(hours=cfg.correction_window_hours)).isoformat()
            await db.purge_old_confirmed(cutoff)

            rows = await db.get_pending()
            logging.info("Updater: проверяю %d рейсов, интервал %d сек", len(rows), cfg.poll_interval)
            for row in rows:
                flight = await svo.get_flight(row["flight_id"], row["flight_number"])
                if not flight:
                    continue
                try:
                    await bot.edit_message_text(
                        chat_id=row["chat_id"],
                        message_id=row["message_id"],
                        text=flight_text(flight, row["note"]),
                        reply_markup=keyboard(),
                    )
                except TelegramBadRequest as e:
                    err = str(e).lower()
                    if "message is not modified" in err:
                        pass
                    elif any(marker in err for marker in DEAD_ROW_MARKERS):
                        logging.info("Удаляю недоступную запись из БД: chat=%s message=%s (%s)",
                                     row["chat_id"], row["message_id"], e)
                        await db.delete(row["chat_id"], row["message_id"])
                    else:
                        logging.warning("Telegram edit error: %s", e)
        except Exception:
            logging.exception("Updater error")
        await asyncio.sleep(cfg.poll_interval)


async def daily_stats_job(bot: Bot):
    """Раз в сутки, ровно в 08:00 по Мск (момент окончания смены), сам
    публикует статистику за только что закончившуюся смену — в тему
    BAGGAGE_TOPIC_ID (если задана). Перед отправкой нового отчёта удаляет
    предыдущий — остаётся только самый свежий.

    Чат, куда слать, не нужно задавать отдельной переменной: берётся
    автоматически из собственных данных бота (где реально есть движение).
    REPORT_CHAT_ID в переменных окружения остаётся как необязательный
    ручной override — пригодится, если бот когда-нибудь будет работать не
    в одной группе, а в нескольких, и автоопределение станет неоднозначным.
    """
    while True:
        now = msk_now()
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        await send_daily_stats_report(bot)


async def send_daily_stats_report(bot: Bot) -> str:
    """Собственно отправка отчёта — вынесено отдельно от цикла ожидания,
    чтобы этим же кодом можно было воспользоваться и из тестовой команды
    /testdailystats, не дожидаясь реальных 08:00. Возвращает короткое
    текстовое описание результата (для ответа в тестовой команде).
    """
    try:
        chat_id = cfg.report_chat_id or await db.get_most_recent_chat_id()
        if chat_id is None:
            msg = "Автоотчёт по статистике: ещё ни одного рейса в базе, слать некуда — пропускаю."
            logging.info(msg)
            return msg

        fire_time = msk_now()
        shift_start, shift_end = previous_shift_bounds(fire_time)
        rows = await db.get_shift_log(chat_id, shift_start.isoformat(), shift_end.isoformat())
        previous_jokes = await db.get_last_jokes(chat_id)
        text, today_jokes = render_shift_stats(rows, shift_start, shift_end, is_current=False, previous_jokes=previous_jokes)

        old_message_id = await db.get_last_stats_message(chat_id)
        if old_message_id:
            await _safe_delete(bot, chat_id, old_message_id)

        new_message = await bot.send_message(
            chat_id, text, message_thread_id=cfg.baggage_topic_id,
        )
        await db.set_last_stats_message(chat_id, new_message.message_id)
        await db.set_last_jokes(chat_id, today_jokes)
        return f"Отчёт отправлен в чат {chat_id} (сообщение {new_message.message_id})."
    except Exception as e:
        logging.exception("Не удалось отправить автоотчёт по статистике смены")
        return f"Не получилось: {e}"


async def main():
    await db.init()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await bot.set_my_commands([
            BotCommand(command="track", description="Отследить рейс, например /track SU1234"),
            BotCommand(command="list", description="Список отслеживаемых рейсов"),
            BotCommand(command="update", description="Список рейсов по времени выдачи багажа"),
        ])
        logging.info("Команды бота (/track, /list, /update) зарегистрированы в Telegram")
    except Exception:
        logging.exception("Не удалось зарегистрировать список команд в Telegram")

    # Админские команды регистрируем ТОЛЬКО в личных чатах самих админов
    # (scope), а не глобально — иначе их увидели бы в подсказках все
    # пользователи бота. Важно: chat-специфичный список ПОЛНОСТЬЮ заменяет
    # общий для этого чата, а не дополняет его — поэтому сюда идёт весь
    # набор команд сразу (см. _register_admin_command_scope), а не только
    # админские, иначе /track и другие пропадут из подсказок в личке.
    db_admin_rows = await db.list_admins()
    all_admin_ids = set(cfg.admin_ids) | {row["user_id"] for row in db_admin_rows}
    for admin_id in all_admin_ids:
        await _register_admin_command_scope(bot, admin_id)
    asyncio.create_task(updater(bot))
    asyncio.create_task(daily_stats_job(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await svo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
