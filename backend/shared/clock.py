from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os

CAMPUS_TZ = ZoneInfo("Asia/Shanghai")

def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timezone required")
    return dt.astimezone(timezone.utc)

def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timezone required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def utc_now() -> datetime:
    # Controlled time is confined to isolated integration-test services.
    clock_file = os.getenv("TEST_CLOCK_FILE")
    if os.getenv("APP_ENV") == "test" and clock_file:
        return parse_time(Path(clock_file).read_text(encoding="utf-8").strip())
    return datetime.now(timezone.utc)
