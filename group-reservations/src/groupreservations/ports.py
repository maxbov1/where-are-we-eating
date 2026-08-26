"""Provider interfaces copied from HungryRadar and scoped for group use."""

from datetime import date, time
from typing import Protocol

from .models import AvailabilityEvidence, Place


class PlaceProvider(Protocol):
    def get_place(self, place_id: str) -> Place: ...


class ReservationProvider(Protocol):
    def check(
        self, place: Place, party_size: int, date_: date, time_: time
    ) -> AvailabilityEvidence: ...
