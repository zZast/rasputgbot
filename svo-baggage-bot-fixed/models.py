from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class Flight:
    flight_id: str
    flight_number: str
    origin: str = "—"
    destination: str = "SVO"
    scheduled_arrival: Optional[datetime] = None
    estimated_arrival: Optional[datetime] = None
    actual_arrival: Optional[datetime] = None
    baggage_time: Optional[datetime] = None
    baggage_belt: Optional[str] = None
    status: str = "unknown"
    status_code: Optional[str] = None
    terminal: Optional[str] = None
    gate: Optional[str] = None
    airline: Optional[str] = None

    def display_arrival(self):
        value = self.actual_arrival or self.estimated_arrival or self.scheduled_arrival
        return value.strftime("%H:%M") if value else "—"

    def display_scheduled(self):
        return self.scheduled_arrival.strftime("%H:%M") if self.scheduled_arrival else "—"

    def display_baggage_time(self):
        return self.baggage_time.strftime("%H:%M") if self.baggage_time else "—"
