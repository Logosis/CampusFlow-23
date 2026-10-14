import sys
import _bootstrap
import httpx
from backend.shared.settings import Settings

def main():
    failed = False
    for module in ("d", "m1", "m2", "m3"):
        settings = Settings.load(module)
        try:
            response = httpx.get(settings.peer_urls[module] + "/health", timeout=5, trust_env=False)
            body = response.json()
            data = body["data"]
            missing = data["unfinishedFunctions"]
            bad = response.status_code != 200 or body["ok"] is not True or missing or not data["workerConfigured"] or data["workerError"]
            print(module.upper(), "未就绪" if bad else "接口和定时入口已注册", ", ".join(missing))
            failed |= bool(bad)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            print(module.upper(), "不可连接或健康返回不合法")
            failed = True
    # 这里只检查接入入口，不代表业务测试通过。
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
