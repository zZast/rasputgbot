"""Real Sheremetyevo (SVO) data adapter.

The bot talks to the same public timetable endpoints used by the official
Sheremetyevo website. The endpoint itself is not a public documented API;
keep all integration code in this module so it can be replaced if SVO
changes it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from models import Flight

log = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

TIMETABLE_URL = "https://www.svo.aero/bitrix/timetable/"
FLIGHT_URL = "https://www.svo.aero/bitrix/timetable/{flight_id}/"


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9А-ЯЁ]", "", value.upper())


def _city(raw: dict | None) -> str:
    if not raw:
        return "—"
    return str(raw.get("city") or raw.get("airport_rus") or raw.get("airport") or "—").strip()


def _flight_from_raw(raw: dict) -> Flight:
    company = raw.get("co") or {}
    company_code = str(company.get("code") or "").strip().upper()
    number = str(raw.get("flt") or "").strip().upper()
    display_number = f"{company_code}{number}" if company_code and not number.startswith(company_code) else number

    scheduled = _parse_dt(raw.get("t_st"))
    actual = _parse_dt(raw.get("t_at"))
    estimated = _parse_dt(raw.get("t_et"))
    baggage = _parse_dt(raw.get("bbel_start")) or _parse_dt(raw.get("estimated_bag_start"))

    # For an arrival, mar1 is the origin and mar2 is normally SVO.
    origin = _city(raw.get("mar1"))
    destination = _city(raw.get("mar2"))

    if actual:
        status = "Приземлился"
    elif estimated and scheduled and estimated != scheduled:
        status = "Ожидается (уточнённое время)"
    elif scheduled:
        status = "Ожидается"
    else:
        status = "Статус неизвестен"

    return Flight(
        flight_id=str(raw.get("i_id")),
        flight_number=display_number,
        origin=origin,
        destination=destination,
        scheduled_arrival=scheduled,
        estimated_arrival=estimated,
        actual_arrival=actual,
        baggage_time=baggage,
        baggage_belt=str(raw.get("bbel_id") or "").strip() or None,
        status=status,
        status_code=str(raw.get("status_code") or "").strip() or None,
        terminal=str(raw.get("term") or "").strip() or None,
        gate=str(raw.get("gate_id") or "").strip() or None,
        airline=company.get("name"),
    )


class SVOClient:
    # Таймаут и число попыток нарочно небольшие: это интерактивный сценарий
    # (пользователь ждёт ответ в чате), а не фоновая задача — лучше быстро
    # сказать "не получилось", чем держать человека в ожидании почти минуту.
    def __init__(self, mock: bool = False, timeout: int = 6, retries: int = 2):
        self.mock = mock
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.retries = max(1, retries)
        self._session: aiohttp.ClientSession | None = None

    async def _get_json(self, url: str, params: dict) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Referer": "https://www.svo.aero/ru/timetable/arrival",
                    "Origin": "https://www.svo.aero",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with self._session.get(url, params=params) as response:
                    body_preview = None
                    if response.status >= 400:
                        body_preview = (await response.text())[:500]
                        log.warning(
                            "SVO вернул статус %s для %s (попытка %s/%s). Тело ответа: %r",
                            response.status, response.url, attempt + 1, self.retries, body_preview,
                        )
                    response.raise_for_status()
                    try:
                        return await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as parse_exc:
                        raw_body = await response.text()
                        log.warning(
                            "SVO вернул не-JSON ответ (похоже, это HTML-страница блокировки/капчи "
                            "или antibot-редирект). Первые 500 символов: %r",
                            raw_body[:500],
                        )
                        raise ValueError(f"Некорректный JSON от SVO: {parse_exc}") from parse_exc
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    await asyncio.sleep(1)

        raise RuntimeError(f"SVO API недоступен: {last_error}")

    async def find_flight(self, flight_number: str, target_date: date | None = None) -> Flight | None:
        """Ищет рейс по номеру. Если target_date не задан — берёт ближайший
        к текущему моменту (как раньше). Если задан — среди найденных
        рейсов с таким номером выбирает тот, что летит именно в этот день
        (актуально для регулярных рейсов с одним номером каждый день:
        SU1701 сегодня и SU1701 завтра — это два разных физических рейса).
        """
        if self.mock:
            now = datetime.now(MSK).replace(second=0, microsecond=0)
            base = datetime.combine(target_date, time(12, 0), tzinfo=MSK) if target_date else now
            return Flight(
                flight_id=f"mock:{flight_number}:{target_date or 'today'}",
                flight_number=flight_number.upper().replace(" ", ""),
                origin="Москва",
                destination="SVO",
                scheduled_arrival=base + timedelta(minutes=25),
                status="ТЕСТОВЫЙ РЕЖИМ",
            )

        query = _norm(flight_number)
        now = datetime.now(MSK)
        anchor = datetime.combine(target_date, time(0, 0), tzinfo=MSK) if target_date else now

        # The official timetable endpoint exposes current/past/next-day data.
        # A 48-hour window (anchored either on "now" or on the requested date)
        # is enough to find the arrival by number.
        payload = await self._get_json(
            TIMETABLE_URL,
            {
                "direction": "arrival",
                "dateStart": (anchor - timedelta(hours=12)).isoformat(timespec="seconds"),
                "dateEnd": (anchor + timedelta(hours=36)).isoformat(timespec="seconds"),
                "perPage": 99999,
                "page": 0,
                "locale": "ru",
            },
        )

        items = payload.get("items", []) if isinstance(payload, dict) else []
        candidates: list[Flight] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            company = str((raw.get("co") or {}).get("code") or "").upper()
            number = str(raw.get("flt") or "").upper()
            full = _norm(f"{company}{number}")
            short = _norm(number)
            if query == full or query == short or full.endswith(query):
                try:
                    candidates.append(_flight_from_raw(raw))
                except Exception:
                    log.exception("Failed to parse SVO flight: %r", raw)

        if not candidates:
            return None

        if target_date is not None:
            # Из всех рейсов с этим номером в окне оставляем только тот
            # (те), что летит именно в запрошенный день.
            exact = [
                f for f in candidates
                if (f.scheduled_arrival or f.actual_arrival or f.estimated_arrival)
                and (f.scheduled_arrival or f.actual_arrival or f.estimated_arrival).astimezone(MSK).date() == target_date
            ]
            if exact:
                candidates = exact

        # Prefer the flight closest to the anchor point; if already landed, keep the most recent one.
        def distance(f: Flight) -> float:
            dt = f.actual_arrival or f.estimated_arrival or f.scheduled_arrival
            if not dt:
                return 10**18
            return abs((dt - anchor).total_seconds())

        return min(candidates, key=distance)

    async def get_flight(self, flight_id: str, flight_number: str) -> Flight | None:
        if self.mock:
            return await self.find_flight(flight_number)

        try:
            payload = await self._get_json(FLIGHT_URL.format(flight_id=int(flight_id)), {"locale": "ru"})
            # The endpoint normally returns the flight object directly.
            raw = payload.get("item", payload) if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                return None
            return _flight_from_raw(raw)
        except (ValueError, TypeError, RuntimeError, aiohttp.ClientError) as exc:
            log.warning("SVO get_flight failed for %s: %s", flight_id, exc)
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
