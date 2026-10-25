from pathlib import Path
import argparse
import secrets
import re
from urllib.parse import quote
from dotenv import dotenv_values
from _bootstrap import ROOT

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--complete", action="store_true", help="仅为已有.env补充缺少的D会话密钥")
    args = parser.parse_args()
    target = ROOT / ".env"
    if target.exists():
        if args.complete:
            current = dotenv_values(target, interpolate=False).get("D_SESSION_SECRET")
            if current:
                if re.fullmatch(r"[0-9a-f]{64}", current) is None:
                    raise SystemExit("已有D_SESSION_SECRET格式不正确；保留原配置，请负责人核对。")
                print("D会话密钥已存在；未修改配置。")
                return
            content = target.read_text(encoding="utf-8")
            separator = "" if not content or content.endswith("\n") else "\n"
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(separator + "D_SESSION_SECRET=" + secrets.token_hex(32) + "\n")
            print("已补充D会话密钥；其他配置保留，秘密未输出。")
            return
        raise SystemExit(".env 已存在；保留现有配置，不覆盖。")
    if args.complete:
        raise SystemExit(".env不存在；请先按开发指导生成配置。")
    data = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            data[key] = value
    data["APP_ENV"] = "test" if args.test else "development"
    data["D_SESSION_SECRET"] = secrets.token_hex(32)
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
