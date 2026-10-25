import asyncio
import json
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from backend.m1.models import M1Config, OpeningRange, OpeningSchedule
from backend.shared.client import Actor, call_peer
from backend.shared.clock import CAMPUS_TZ, utc_now
from backend.shared.db import session_factory
from backend.shared.settings import Settings


ROOT = Path(__file__).resolve().parents[2]


async def _verify_master_data(data: dict) -> None:
    settings = Settings.load("m1")
    for space in data["spaces"]:
        result = await call_peer(
            settings, "d", "query_master",
            {"operation": "GET_SPACE", "spaceId": space["spaceId"], "includeUnregistered": True},
            Actor("SYSTEM"),
        )
        if result.get("spaceId") != space["spaceId"]:
            raise RuntimeError("D主数据教室标识不一致")
    for seat in data["seats"]:
        result = await call_peer(
            settings, "d", "query_master",
            {"operation": "GET_SEAT", "seatId": seat["seatId"], "includeUnregistered": True},
            Actor("SYSTEM"),
        )
        if result.get("seatId") != seat["seatId"] or result.get("spaceId") != seat["spaceId"]:
            raise RuntimeError("D主数据座位标识不一致")


def seed():
    data = json.loads((ROOT / "tests/fixtures/bootstrap.json").read_text(encoding="utf-8"))
    asyncio.run(_verify_master_data(data))
    m1 = data["m1"]
    factory = session_factory("m1")
    with factory() as db:
        if db.get(M1Config, "default") is None:
            db.add(M1Config(
                key="default", max_reservation_hours=m1["maxReservationHours"], version=1
            ))
        campus_date = utc_now().astimezone(CAMPUS_TZ).date()
        start_hour, start_minute = map(int, m1["opening"]["campusStart"].split(":"))
        end_hour, end_minute = map(int, m1["opening"]["campusEnd"].split(":"))
        for space in data["spaces"]:
            space_id = space["spaceId"]
            exists = db.scalar(select(OpeningSchedule.space_id).where(
                OpeningSchedule.space_id == space_id
            ))
            if exists is not None:
                continue
            schedule = OpeningSchedule(space_id=space_id, version=m1["opening"]["version"])
            for offset in range(m1["opening"]["days"]):
                day = campus_date + timedelta(days=offset)
                start = datetime.combine(day, time(start_hour, start_minute), CAMPUS_TZ)
                end = datetime.combine(day, time(end_hour, end_minute), CAMPUS_TZ)
                schedule.ranges.append(OpeningRange(start_time=start, end_time=end))
            db.add(schedule)
        db.commit()
