"""Reservation models rendered as schema.org JSON-LD for Gmail markup

Field names follow Google's reference docs:
https://developers.google.com/workspace/gmail/markup/reference
"""

import hashlib
import html
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Optional

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

CONFIRMED = 'http://schema.org/ReservationConfirmed'

# Required text must have content: '' or '  ' would render as blank markup Gmail ignores
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Names = Annotated[list[Text], Field(min_length=1, description='Full name of each traveller')]
LocalTime = Annotated[
    AwareDatetime,
    Field(description='Local time with its UTC offset, e.g. 2026-11-01T09:05:00+01:00'),
]


class Address(BaseModel):
    street_address: Text
    locality: Text = Field(description='City or town')
    region: Optional[Text] = None
    postal_code: Text
    country: Text


class Leg(BaseModel):
    departure_time: LocalTime
    arrival_time: LocalTime

    @model_validator(mode='after')
    def _arrives_after_departure(self):
        if self.arrival_time <= self.departure_time:
            raise ValueError('`arrival_time` must be after `departure_time`')
        return self


class FlightLeg(Leg):
    airline_name: Text
    airline_code: str = Field(
        pattern=r'^[A-Z0-9]{2,3}$',
        description=(
            'Airline code as printed before the flight number in the booking, e.g. EJU for '
            'EJU7024. Either the 2-character IATA code (EC) or the 3-letter ICAO code (EJU) works'
        ),
    )
    flight_number: str = Field(
        pattern=r'^[0-9]{1,4}[A-Z]?$', description='Without the airline code, e.g. 7024'
    )
    departure_airport_name: Text
    departure_airport_iata: str = Field(pattern=r'^[A-Z]{3}$')
    departure_terminal: Optional[Text] = None
    arrival_airport_name: Text
    arrival_airport_iata: str = Field(pattern=r'^[A-Z]{3}$')
    arrival_terminal: Optional[Text] = None


class TrainLeg(Leg):
    train_company: Optional[Text] = None
    train_number: Optional[Text] = None
    departure_station: Text
    departure_platform: Optional[Text] = None
    arrival_station: Text
    arrival_platform: Optional[Text] = None


class BusLeg(Leg):
    bus_company: Text
    bus_number: Optional[Text] = None
    departure_stop: Text
    arrival_stop: Text


@dataclass(frozen=True)
class Email:
    subject: str
    lines: list[str]
    items: list[dict[str, Any]]

    def html(self) -> str:
        data = json.dumps(self.items, ensure_ascii=False, indent=2).replace('</', '<\\/')
        body = ''.join(f'<p>{html.escape(line)}</p>' for line in self.lines)
        return (
            f'<html><body>\n<script type="application/ld+json">\n{data}\n</script>\n'
            f'{body}\n</body></html>'
        )


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _reservation(
    type_: str, reservation_number: str, name: str, reservation_for: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {
        '@context': 'http://schema.org',
        '@type': type_,
        'reservationNumber': reservation_number,
        'reservationStatus': CONFIRMED,
        'underName': {'@type': 'Person', 'name': name},
        'reservationFor': reservation_for,
        **extra,
    }


def _address(address: Address) -> dict[str, Any]:
    return _drop_none({
        '@type': 'PostalAddress',
        'streetAddress': address.street_address,
        'addressLocality': address.locality,
        'addressRegion': address.region,
        'postalCode': address.postal_code,
        'addressCountry': address.country,
    })


def _when(departure: datetime, arrival: datetime) -> str:
    return f'{departure:%Y-%m-%d %H:%M} -> {arrival:%Y-%m-%d %H:%M}'


def _reference(reservation_number: Optional[str], key: str) -> tuple[str, list[str]]:
    """Return the reservation number to use, plus a note for the email if it was generated

    For a booking without a reference, the stand-in is derived from `key`, so the same booking
    always gets the same one: Gmail treats a new reservation number as a new booking, so a random
    one would duplicate the event on every re-run.
    """
    if reservation_number is not None:
        return reservation_number, []
    generated = 'NOREF-' + hashlib.sha256(key.encode()).hexdigest()[:8].upper()
    return generated, [f'No booking reference in the booking; generated {generated}']


def _normalise(text: str) -> str:
    """Letters, marks and digits of any script, casefolded, so wording noise maps to the same key

    Case, punctuation and spacing are dropped, and full-width forms fold to ASCII: 'Geneve, Gare
    routiere' and 'GENEVE gare-routiere' both give 'genevegareroutiere'. Accents are dropped from
    Latin letters only, since an LLM may write the unaccented spelling; marks in other scripts
    change the letter (e.g. Japanese voicing marks), so Chinese, Japanese and Korean names keep
    every character.
    """
    kept = []
    after_latin = False
    for c in unicodedata.normalize('NFKD', text.casefold()):
        if unicodedata.combining(c):
            if after_latin:
                continue  # an accent on a Latin letter
        else:
            after_latin = c.isascii()
        kept.append(c)
    recomposed = unicodedata.normalize('NFC', ''.join(kept))
    return ''.join(c for c in recomposed if unicodedata.category(c)[0] in 'LMN')


def _legs_key(legs: list[Leg]) -> str:
    """Times only: the LLM may word stop, station and company names differently between runs

    Times alone are enough here: two different trips with identical departure and arrival times
    to the minute cannot both be taken, so a match means the same trip.
    """
    return '|'.join(
        f'{leg.departure_time.astimezone(UTC).isoformat()}>'
        f'{leg.arrival_time.astimezone(UTC).isoformat()}'
        for leg in legs
    )


def flight_email(reservation_number: str, names: list[str], legs: list[FlightLeg]) -> Email:
    items = [
        _reservation('FlightReservation', reservation_number, name, _drop_none({
            '@type': 'Flight',
            'flightNumber': leg.flight_number,
            'airline': {
                '@type': 'Airline',
                'name': leg.airline_name,
                'iataCode': leg.airline_code
            },
            'departureAirport': {
                '@type': 'Airport',
                'name': leg.departure_airport_name,
                'iataCode': leg.departure_airport_iata,
            },
            'departureTerminal': leg.departure_terminal,
            'departureTime': leg.departure_time.isoformat(),
            'arrivalAirport': {
                '@type': 'Airport',
                'name': leg.arrival_airport_name,
                'iataCode': leg.arrival_airport_iata,
            },
            'arrivalTerminal': leg.arrival_terminal,
            'arrivalTime': leg.arrival_time.isoformat(),
        }))
        for leg in legs
        for name in names
    ]
    lines = [
        f'{leg.airline_code}{leg.flight_number} {leg.departure_airport_iata} -> '
        f'{leg.arrival_airport_iata}, {_when(leg.departure_time, leg.arrival_time)}'
        for leg in legs
    ]
    return Email(f'Flight reservation {reservation_number}', [*lines, ', '.join(names)], items)


def train_email(
    reservation_number: Optional[str], names: list[str], legs: list[TrainLeg]
) -> Email:
    reservation_number, notes = _reference(reservation_number, _legs_key(legs))
    items = [
        _reservation('TrainReservation', reservation_number, name, _drop_none({
            '@type': 'TrainTrip',
            'trainCompany': (
                {'@type': 'Organization', 'name': leg.train_company} if leg.train_company else None
            ),
            'trainNumber': leg.train_number,
            'departureStation': {'@type': 'TrainStation', 'name': leg.departure_station},
            'departurePlatform': leg.departure_platform,
            'departureTime': leg.departure_time.isoformat(),
            'arrivalStation': {'@type': 'TrainStation', 'name': leg.arrival_station},
            'arrivalPlatform': leg.arrival_platform,
            'arrivalTime': leg.arrival_time.isoformat(),
        }))
        for leg in legs
        for name in names
    ]
    lines = [
        f'{leg.departure_station} -> {leg.arrival_station}, '
        f'{_when(leg.departure_time, leg.arrival_time)}'
        for leg in legs
    ]
    return Email(
        f'Train reservation {reservation_number}', [*lines, ', '.join(names), *notes], items
    )


def bus_email(reservation_number: Optional[str], names: list[str], legs: list[BusLeg]) -> Email:
    reservation_number, notes = _reference(reservation_number, _legs_key(legs))
    items = [
        _reservation('BusReservation', reservation_number, name, _drop_none({
            '@type': 'BusTrip',
            'busCompany': {'@type': 'Organization', 'name': leg.bus_company},
            'busNumber': leg.bus_number,
            'departureBusStop': {'@type': 'BusStop', 'name': leg.departure_stop},
            'departureTime': leg.departure_time.isoformat(),
            'arrivalBusStop': {'@type': 'BusStop', 'name': leg.arrival_stop},
            'arrivalTime': leg.arrival_time.isoformat(),
        }))
        for leg in legs
        for name in names
    ]
    lines = [
        f'{leg.departure_stop} -> {leg.arrival_stop}, '
        f'{_when(leg.departure_time, leg.arrival_time)}'
        for leg in legs
    ]
    return Email(
        f'Bus reservation {reservation_number}', [*lines, ', '.join(names), *notes], items
    )


def hotel_email(
    reservation_number: Optional[str],
    guest_name: str,
    hotel_name: str,
    address: Address,
    checkin: datetime,
    checkout: datetime,
    telephone: Optional[str] = None,
) -> Email:
    if checkout <= checkin:
        raise ValueError('checkout must be after checkin')
    # Dates, not times: check-in and check-out times are often missing and guessed. Two stays can
    # share dates, so the postcode and name tell them apart; a reworded name then duplicates the
    # event, which is visible, rather than overwriting another booking, which is not
    reservation_number, notes = _reference(
        reservation_number,
        f'{checkin.date().isoformat()}>{checkout.date().isoformat()}'
        f'|{_normalise(address.postal_code)}|{_normalise(hotel_name)}',
    )
    item = _reservation(
        'LodgingReservation',
        reservation_number,
        guest_name,
        _drop_none({
            '@type': 'LodgingBusiness',
            'name': hotel_name,
            'address': _address(address),
            'telephone': telephone,
        }),
        checkinDate=checkin.isoformat(),
        checkoutDate=checkout.isoformat(),
    )
    lines = [f'{hotel_name}, {address.locality}', _when(checkin, checkout), guest_name, *notes]
    return Email(f'Hotel reservation {reservation_number}', lines, [item])


def event_email(
    reservation_number: Optional[str],
    attendee_name: str,
    event_name: str,
    start: datetime,
    venue_name: str,
    venue_address: Address,
    end: Optional[datetime] = None,
) -> Email:
    if end is not None and end <= start:
        raise ValueError('end must be after start')
    # Start, not end: tickets often have no end time, so it may be given on one run and not the
    # next. Two events can start together (e.g. screenings at a multiplex), so add the name
    reservation_number, notes = _reference(
        reservation_number, f'{start.astimezone(UTC).isoformat()}|{_normalise(event_name)}'
    )
    item = _reservation(
        'EventReservation',
        reservation_number,
        attendee_name,
        _drop_none({
            '@type': 'Event',
            'name': event_name,
            'startDate': start.isoformat(),
            'endDate': end.isoformat() if end else None,
            'location': {
                '@type': 'Place',
                'name': venue_name,
                'address': _address(venue_address),
            },
        }),
    )
    when = f'{start:%Y-%m-%d %H:%M}' + (f' -> {end:%Y-%m-%d %H:%M}' if end else '')
    lines = [event_name, f'{venue_name}, {venue_address.locality}', when, attendee_name, *notes]
    return Email(f'Event reservation {reservation_number}', lines, [item])
