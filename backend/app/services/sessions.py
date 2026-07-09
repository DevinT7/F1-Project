"""Session / driver lookup (Person 1). Maps raw OpenF1 dicts -> contract models."""
from __future__ import annotations

from ..clients.openf1 import get_client
from ..models.schemas import Driver, Session


async def list_sessions(year: int | None = None, country_name: str | None = None,
                        session_name: str | None = None) -> list[Session]:
    rows = await get_client().sessions(
        year=year, country_name=country_name, session_name=session_name)
    return [
        Session(
            session_key=r["session_key"],
            meeting_key=r.get("meeting_key", 0),
            year=r.get("year", year or 0),
            country_name=r.get("country_name"),
            circuit_short_name=r.get("circuit_short_name"),
            session_name=r.get("session_name"),
            session_type=r.get("session_type"),
            date_start=r.get("date_start"),
        )
        for r in rows
    ]


async def list_drivers(session_key: int | str) -> list[Driver]:
    rows = await get_client().drivers(session_key=session_key)
    return [_to_driver(r) for r in rows]


async def get_driver(session_key: int | str, driver_number: int) -> Driver:
    rows = await get_client().drivers(session_key=session_key, driver_number=driver_number)
    if not rows:
        return Driver(driver_number=driver_number)
    return _to_driver(rows[0])


def _to_driver(r: dict) -> Driver:
    return Driver(
        driver_number=r["driver_number"],
        full_name=r.get("full_name"),
        name_acronym=r.get("name_acronym"),
        team_name=r.get("team_name"),
        team_colour=r.get("team_colour"),
        headshot_url=r.get("headshot_url"),
    )
