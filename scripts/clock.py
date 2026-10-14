from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
from _bootstrap import ROOT
from backend.shared.clock import format_time, parse_time, CAMPUS_TZ
from backend.shared.settings import Settings
import os

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "advance"])
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    settings = Settings.load("d")
    if settings.app_env != "test":
        raise SystemExit("受控时间只允许隔离的test环境。")
    path = Path(os.getenv("TEST_CLOCK_FILE", ""))
    if not str(path) or str(path) == ".":
        raise SystemExit("TEST_CLOCK_FILE 未设置。")
    if not path.is_absolute():
        path = ROOT / path
    expected = (ROOT / "tmp" / "clock").resolve()
    if not path.resolve().is_relative_to(expected):
        raise SystemExit("测试时钟必须放在项目tmp/clock中。")
    if args.action == "init":
        if path.exists():
            raise SystemExit("时钟已存在，不重置运行中的测试。")
        campus = datetime.now(timezone.utc).astimezone(CAMPUS_TZ)
        value = (campus + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    else:
        if args.seconds is None or args.seconds <= 0:
            raise SystemExit("advance 需要正整数秒数。")
        value = parse_time(path.read_text(encoding="utf-8").strip()) + timedelta(seconds=args.seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".new")
    temp.write_text(format_time(value), encoding="utf-8")
    temp.replace(path)
    print("测试时钟已更新。")

if __name__ == "__main__":
    main()
