from pathlib import Path
import argparse
import secrets
from urllib.parse import quote
from _bootstrap import ROOT

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    target = ROOT / ".env"
    if target.exists():
        raise SystemExit(".env 已存在；保留现有配置，不覆盖。")
    data = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            data[key] = value
    data["APP_ENV"] = "test" if args.test else "development"
    for module in ("D", "M1", "M2", "M3"):
        data[f"SERVICE_CREDENTIAL_{module}"] = secrets.token_hex(24)
        password = secrets.token_hex(16)
        data[f"{module}_DB_PASSWORD"] = password
        db = f"campusflow_{module.lower()}"
        data[f"{module}_DATABASE_URL"] = f"postgresql+psycopg://{db}:{quote(password)}@127.0.0.1:5432/{db}"
    data["POSTGRES_PASSWORD"] = secrets.token_hex(24)
    if args.test:
        data["TEST_CLOCK_FILE"] = "tmp/clock/now.txt"
    target.write_text("\n".join(f"{k}={v}" for k, v in data.items()) + "\n", encoding="utf-8")
    print("已生成 .env；秘密未输出。")

if __name__ == "__main__":
    main()
