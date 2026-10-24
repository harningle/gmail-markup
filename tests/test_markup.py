import functools
import json
import re
from datetime import datetime

import httpx
import pydantic
import pytest
from mcp import Client
from pydantic import ValidationError

from gmail_markup import markup, server

# A fake trip, 1-5 Nov 2026
OUTBOUND = {
    'airline_name': 'easyJet',
    'airline_code': 'EC',
    'flight_number': '1234',
    'departure_airport_name': 'Geneva',
    'departure_airport_iata': 'GVA',
    'departure_terminal': '1',
    'departure_time': '2026-11-01T09:05:00+01:00',
    'arrival_airport_name': 'Malaga',
    'arrival_airport_iata': 'AGP',
    'arrival_time': '2026-11-01T11:35:00+01:00',
}
RETURN = {
    'airline_name': 'easyJet',
    'airline_code': 'EJU',
    'flight_number': '1235',
    'departure_airport_name': 'Malaga',
    'departure_airport_iata': 'AGP',
    'departure_time': '2026-11-05T12:10:00+01:00',
    'arrival_airport_name': 'Geneva',
    'arrival_airport_iata': 'GVA',
    'arrival_time': '2026-11-05T14:40:00+01:00',
}
ADDRESS = {
    'street_address': 'Calle Larios 1',
    'locality': 'Malaga',
    'region': 'Andalucia',
    'postal_code': '29005',
    'country': 'Spain',
}


def flight_reservation(name, code, flight_number, dep, arr, dep_time, arr_time, terminal=None):
    flight = {
        '@type': 'Flight',
        'flightNumber': flight_number,
        'airline': {'@type': 'Airline', 'name': 'easyJet', 'iataCode': code},
        'departureAirport': {'@type': 'Airport', 'name': dep[0], 'iataCode': dep[1]},
        'departureTime': dep_time,
        'arrivalAirport': {'@type': 'Airport', 'name': arr[0], 'iataCode': arr[1]},
        'arrivalTime': arr_time,
    }
    if terminal:
        flight['departureTerminal'] = terminal
    return {
        '@context': 'http://schema.org',
        '@type': 'FlightReservation',
        'reservationNumber': 'TEST1105',
        'reservationStatus': 'http://schema.org/ReservationConfirmed',
        'underName': {'@type': 'Person', 'name': name},
        'reservationFor': flight,
    }


def test_flight_golden():
    email = markup.flight_email(
        'TEST1105',
        ['Jane Doe'],
        [markup.FlightLeg(**OUTBOUND), markup.FlightLeg(**RETURN)],
    )
    assert email.items == [
        flight_reservation(
            'Jane Doe', 'EC', '1234', ('Geneva', 'GVA'), ('Malaga', 'AGP'),
            '2026-11-01T09:05:00+01:00', '2026-11-01T11:35:00+01:00', terminal='1',
        ),
        flight_reservation(
            'Jane Doe', 'EJU', '1235', ('Malaga', 'AGP'), ('Geneva', 'GVA'),
            '2026-11-05T12:10:00+01:00', '2026-11-05T14:40:00+01:00',
        ),
    ]
    assert email.subject == 'Flight reservation TEST1105'


def test_hotel_golden():
    email = markup.hotel_email(
        'TEST1105',
        'Jane Doe',
        'Hotel Test',
        markup.Address(**ADDRESS),
        datetime.fromisoformat('2026-11-01T15:00:00+01:00'),
        datetime.fromisoformat('2026-11-05T11:00:00+01:00'),
    )
    assert email.items == [{
        '@context': 'http://schema.org',
        '@type': 'LodgingReservation',
        'reservationNumber': 'TEST1105',
        'reservationStatus': 'http://schema.org/ReservationConfirmed',
        'underName': {'@type': 'Person', 'name': 'Jane Doe'},
        'reservationFor': {
            '@type': 'LodgingBusiness',
            'name': 'Hotel Test',
            'address': {
                '@type': 'PostalAddress',
                'streetAddress': 'Calle Larios 1',
                'addressLocality': 'Malaga',
                'addressRegion': 'Andalucia',
                'postalCode': '29005',
                'addressCountry': 'Spain',
            },
        },
        'checkinDate': '2026-11-01T15:00:00+01:00',
        'checkoutDate': '2026-11-05T11:00:00+01:00',
    }]


def test_one_reservation_per_passenger_per_leg():
    email = markup.flight_email(
        'TEST1105',
        ['Jane Doe', 'John Doe'],
        [markup.FlightLeg(**OUTBOUND), markup.FlightLeg(**RETURN)],
    )
    assert [(i['reservationFor']['flightNumber'], i['underName']['name']) for i in email.items] == [
        ('1234', 'Jane Doe'),
        ('1234', 'John Doe'),
        ('1235', 'Jane Doe'),
        ('1235', 'John Doe'),
    ]


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError, match='timezone'):
        markup.FlightLeg(**{**OUTBOUND, 'departure_time': '2026-11-01T09:05:00'})


def test_airline_code_iata_or_icao():
    for code in ('EC', 'U2', 'EJU'):
        markup.FlightLeg(**{**OUTBOUND, 'airline_code': code})
    with pytest.raises(ValidationError, match='airline_code'):
        markup.FlightLeg(**{**OUTBOUND, 'airline_code': 'EASYJET'})


def test_arrival_before_departure_rejected():
    with pytest.raises(ValidationError, match='`arrival_time` must be after'):
        markup.FlightLeg(**{**OUTBOUND, 'arrival_time': '2026-11-01T08:00:00+01:00'})


def test_html_embeds_jsonld():
    email = markup.flight_email('TEST1105', ['Jane Doe'], [markup.FlightLeg(**OUTBOUND)])
    script = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', email.html(), re.DOTALL
    )
    assert json.loads(script.group(1)) == email.items
    assert 'EC1234 GVA -&gt; AGP, 2026-11-01 09:05 -&gt; 2026-11-01 11:35' in email.html()


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def apps_script(monkeypatch):
    """Route the Apps Script POST to a fake that records requests and returns `reply`."""
    monkeypatch.setenv('APPS_SCRIPT_URL', 'https://script.google.com/macros/s/fake/exec')
    monkeypatch.setenv('APPS_SCRIPT_TOKEN', 'test-token')
    fake = {'requests': [], 'reply': {'ok': True}}

    def handler(request):
        fake['requests'].append(json.loads(request.content))
        return httpx.Response(200, json=fake['reply'])

    monkeypatch.setattr(
        server.httpx,
        'AsyncClient',
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    return fake


@pytest.mark.anyio
async def test_add_flight_sends_markup(apps_script):
    async with Client(server.server) as client:
        result = await client.call_tool(
            'add_flight',
            {'reservation_number': 'TEST1105', 'names': ['Jane Doe'], 'legs': [OUTBOUND]},
        )
    assert not result.is_error, result.content
    [sent] = apps_script['requests']
    assert sent['token'] == 'test-token'
    assert sent['subject'] == 'Flight reservation TEST1105'
    assert '"@type": "FlightReservation"' in sent['htmlBody']


@pytest.mark.anyio
async def test_apps_script_error_fails_loud(apps_script):
    apps_script['reply'] = {'ok': False, 'error': 'unauthorised'}
    async with Client(server.server) as client:
        result = await client.call_tool(
            'add_flight',
            {'reservation_number': 'TEST1105', 'names': ['Jane Doe'], 'legs': [OUTBOUND]},
        )
    assert result.is_error
    assert 'unauthorised' in result.content[0].text


@pytest.mark.anyio
async def test_checkout_before_checkin_reaches_model(apps_script):
    async with Client(server.server) as client:
        result = await client.call_tool('add_hotel', {
            'reservation_number': 'TEST1105',
            'guest_name': 'Jane Doe',
            'hotel_name': 'Hotel Test',
            'address': ADDRESS,
            'checkin': '2026-11-05T15:00:00+01:00',
            'checkout': '2026-11-01T11:00:00+01:00',
        })
    assert result.is_error
    assert 'checkout must be after checkin' in result.content[0].text
    assert apps_script['requests'] == []


BUS = {
    'bus_company': 'FlixBus',
    'departure_stop': 'Geneva Bus Station',
    'departure_time': '2026-11-01T10:00:00+01:00',
    'arrival_stop': 'Lyon Perrache',
    'arrival_time': '2026-11-01T12:00:00+01:00',
}


def test_bus_without_reference_gets_stable_generated_one():
    email = markup.bus_email(None, ['Jane Doe'], [markup.BusLeg(**BUS)])
    reference = email.items[0]['reservationNumber']
    assert re.fullmatch(r'NOREF-[0-9A-F]{8}', reference)
    assert email.subject == f'Bus reservation {reference}'
    assert f'generated {reference}' in email.lines[-1]

    # Same trip, worded differently and given in UTC: same reference, so re-runs don't duplicate
    reworded = {
        **BUS,
        'bus_company': 'Flixbus GmbH',
        'departure_stop': 'Geneve, gare routiere',
        'departure_time': '2026-11-01T09:00:00Z',
    }
    again = markup.bus_email(None, ['Jane Doe'], [markup.BusLeg(**reworded)])
    assert again.items[0]['reservationNumber'] == reference

    moved = {**BUS, 'departure_time': '2026-11-01T11:00:00+01:00'}
    other = markup.bus_email(None, ['Jane Doe'], [markup.BusLeg(**moved)])
    assert other.items[0]['reservationNumber'] != reference


def test_bus_with_reference_keeps_it():
    email = markup.bus_email('NOREF-0000ABCD', ['Jane Doe'], [markup.BusLeg(**BUS)])
    assert email.items[0]['reservationNumber'] == 'NOREF-0000ABCD'
    assert not any('generated' in line for line in email.lines)


@pytest.mark.anyio
async def test_add_bus_without_reference(apps_script):
    async with Client(server.server) as client:
        result = await client.call_tool('add_bus', {'names': ['Jane Doe'], 'legs': [BUS]})
    assert not result.is_error, result.content
    [sent] = apps_script['requests']
    assert re.fullmatch(r'Bus reservation NOREF-[0-9A-F]{8}', sent['subject'])


def test_train_without_reference_ignores_station_wording():
    leg = {
        'departure_station': 'Geneve',
        'departure_time': '2026-11-01T10:00:00+01:00',
        'arrival_station': 'Lausanne',
        'arrival_time': '2026-11-01T10:40:00+01:00',
    }
    reworded = {**leg, 'departure_station': 'Geneve-Cornavin', 'train_company': 'SBB'}
    first = markup.train_email(None, ['Jane Doe'], [markup.TrainLeg(**leg)])
    second = markup.train_email(None, ['Jane Doe'], [markup.TrainLeg(**reworded)])
    assert first.items[0]['reservationNumber'] == second.items[0]['reservationNumber']
    assert first.subject.startswith('Train reservation NOREF-')


def hotel_reference(hotel_name, checkin, checkout, postal_code='29005'):
    email = markup.hotel_email(
        None, 'Jane Doe', hotel_name, markup.Address(**{**ADDRESS, 'postal_code': postal_code}),
        datetime.fromisoformat(checkin), datetime.fromisoformat(checkout),
    )
    return email.items[0]['reservationNumber']


def test_hotel_without_reference_ignores_times_and_wording_noise():
    stay = hotel_reference('Hotel Test', '2026-11-01T15:00:00+01:00', '2026-11-05T11:00:00+01:00')
    assert stay.startswith('NOREF-')
    # Guessed check-in/out times differ between runs: same stay, same reference
    assert stay == hotel_reference(
        'Hotel Test', '2026-11-01T14:00:00+01:00', '2026-11-05T12:00:00+01:00'
    )
    # Case, punctuation and accents are not part of the identity
    assert stay == hotel_reference(
        'Hôtel-TEST', '2026-11-01T15:00:00+01:00', '2026-11-05T11:00:00+01:00'
    )
    assert stay != hotel_reference(
        'Hotel Test', '2026-11-02T15:00:00+01:00', '2026-11-05T11:00:00+01:00'
    )


def test_hotels_with_same_dates_do_not_collide():
    dates = ('2026-11-01T15:00:00+01:00', '2026-11-05T11:00:00+01:00')
    references = {
        hotel_reference('Hotel Test', *dates),
        hotel_reference('Other Hotel', *dates),
        hotel_reference('Hotel Test', *dates, postal_code='18010'),
    }
    assert len(references) == 3


def event_reference(event_name, end=None):
    email = markup.event_email(
        None, 'Jane Doe', event_name, datetime.fromisoformat('2026-11-03T20:00:00+01:00'),
        'Victoria Hall', markup.Address(**ADDRESS),
        datetime.fromisoformat(end) if end else None,
    )
    return email.items[0]['reservationNumber']


def test_event_without_reference_ignores_end():
    assert event_reference('Concert') == event_reference('Concert', '2026-11-03T22:30:00+01:00')
    assert event_reference('Concert').startswith('NOREF-')


def test_events_starting_together_do_not_collide():
    assert event_reference('Film A') != event_reference('Film B')


@pytest.mark.parametrize('names', [[''], ['   '], ['Jane Doe', '']])
def test_blank_names_rejected(names):
    with pytest.raises(ValidationError):
        pydantic.TypeAdapter(markup.Names).validate_python(names)


def test_blank_required_field_rejected():
    with pytest.raises(ValidationError, match='bus_company'):
        markup.BusLeg(**{**BUS, 'bus_company': '  '})
    with pytest.raises(ValidationError, match='street_address'):
        markup.Address(**{**ADDRESS, 'street_address': ''})


@pytest.mark.anyio
@pytest.mark.parametrize('field', ['hotel_name', 'reservation_number'])
async def test_blank_tool_argument_reaches_model(apps_script, field):
    args = {
        'guest_name': 'Jane Doe',
        'hotel_name': 'Hotel Test',
        'address': ADDRESS,
        'checkin': '2026-11-01T15:00:00+01:00',
        'checkout': '2026-11-05T11:00:00+01:00',
        field: ' ',
    }
    async with Client(server.server) as client:
        result = await client.call_tool('add_hotel', args)
    assert result.is_error
    assert field in result.content[0].text
    assert apps_script['requests'] == []
