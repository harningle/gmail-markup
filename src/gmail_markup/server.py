"""MCP server whose tools email reservation markup to myself through the Apps Script web app

Gmail only honours markup from registered senders or in emails sent to yourself, so the Apps
Script web app (running as me) does the sending; this server only validates and renders.
"""

import os
from typing import Annotated, Optional

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from gmail_markup import markup

ENV_VARS = ('APPS_SCRIPT_URL', 'APPS_SCRIPT_TOKEN', 'MCP_PATH_SECRET', 'PUBLIC_HOST')
PORT = 8002

server = MCPServer(
    'gmail-markup',
    instructions=(
        'Turn a booking confirmation into a Google Calendar event: each tool emails Gmail '
        'reservation markup to the user, and Gmail adds the event to their calendar. Call one '
        'tool per booking. All times are local to where they happen, with that place\'s UTC '
        'offset, e.g. 2026-11-01T09:05:00+01:00. Sending the same reservation number again '
        'updates the existing event.'
    ),
)

async def send(email: markup.Email) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        # Apps Script answers the POST with a 302 to the result, which httpx follows as a GET
        response = await client.post(
            os.environ['APPS_SCRIPT_URL'],
            json={
                'token': os.environ['APPS_SCRIPT_TOKEN'],
                'subject': email.subject,
                'htmlBody': email.html(),
            },
        )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError:
        raise ToolError(f'Apps Script returned non-JSON: {response.text[:500]}') from None
    if result != {'ok': True}:
        raise ToolError(f'Apps Script failed: {result}')
    return '\n'.join([f'Sent "{email.subject}":', *email.lines])


@server.tool()
async def add_flight(
    reservation_number: markup.Text,
    names: markup.Names,
    legs: Annotated[list[markup.FlightLeg], Field(min_length=1)],
) -> str:
    """Add a flight booking to Google Calendar

    Include every leg (outbound and return) and every passenger of the booking in one call.
    """
    return await send(markup.flight_email(reservation_number, names, legs))


@server.tool()
async def add_train(
    names: markup.Names,
    legs: Annotated[list[markup.TrainLeg], Field(min_length=1)],
    reservation_number: Optional[markup.Text] = None,
) -> str:
    """Add a train booking to Google Calendar

    Include every leg and every passenger of the booking in one call. If the booking has no
    reference, leave `reservation_number` out and a NOREF-... one is generated. To update such a
    booking after a change, find the earlier "Train reservation NOREF-..." email in Gmail and pass
    that reference with the new details, so the existing event is updated, not duplicated.
    """
    return await send(markup.train_email(reservation_number, names, legs))


@server.tool()
async def add_bus(
    names: markup.Names,
    legs: Annotated[list[markup.BusLeg], Field(min_length=1)],
    reservation_number: Optional[markup.Text] = None,
) -> str:
    """Add a bus or coach booking to Google Calendar

    Include every leg and every passenger of the booking in one call. If the booking has no
    reference, leave `reservation_number` out and a NOREF-... one is generated. To update such a
    booking after a change, find the earlier "Bus reservation NOREF-..." email in Gmail and pass
    that reference with the new details, so the existing event is updated, not duplicated.
    """
    return await send(markup.bus_email(reservation_number, names, legs))


@server.tool()
async def add_hotel(
    guest_name: markup.Text,
    hotel_name: markup.Text,
    address: markup.Address,
    checkin: markup.LocalTime,
    checkout: markup.LocalTime,
    telephone: Optional[markup.Text] = None,
    reservation_number: Optional[markup.Text] = None,
) -> str:
    """Add a hotel or other accommodation booking to Google Calendar

    Use the property's check-in and check-out times; if the booking gives none, use 15:00 and
    11:00 local time. If the booking has no reference, leave `reservation_number` out and a
    NOREF-... one is generated. To update such a booking after a change, find the earlier
    "Hotel reservation NOREF-..." email in Gmail and pass that reference with the new details,
    so the existing event is updated, not duplicated.
    """
    try:
        email = markup.hotel_email(
            reservation_number, guest_name, hotel_name, address, checkin, checkout, telephone
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return await send(email)


@server.tool()
async def add_event(
    attendee_name: markup.Text,
    event_name: markup.Text,
    start: markup.LocalTime,
    venue_name: markup.Text,
    venue_address: markup.Address,
    end: Optional[markup.LocalTime] = None,
    reservation_number: Optional[markup.Text] = None,
) -> str:
    """Add a ticketed event (concert, match, show, ...) to Google Calendar

    If the booking has no reference, leave `reservation_number` out and a NOREF-... one is
    generated. To update such a booking after a change, find the earlier "Event reservation
    NOREF-..." email in Gmail and pass that reference with the new details, so the existing
    event is updated, not duplicated.
    """
    try:
        email = markup.event_email(
            reservation_number, attendee_name, event_name, start, venue_name, venue_address, end
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return await send(email)


def main() -> None:
    missing = [var for var in ENV_VARS if not os.environ.get(var)]
    if missing:
        raise SystemExit(f'Missing environment variables: {", ".join(missing)}')
    server.run(
        'streamable-http',
        host='127.0.0.1',
        port=PORT,
        streamable_http_path=f'/gmail-markup/{os.environ["MCP_PATH_SECRET"]}/mcp',
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=['127.0.0.1:*', 'localhost:*', os.environ['PUBLIC_HOST']],
            allowed_origins=['http://127.0.0.1:*', 'http://localhost:*', 'https://claude.ai'],
        ),
    )
