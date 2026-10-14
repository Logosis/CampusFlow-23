"""只验证四模块启动与占位失败；不代表业务集成通过。"""
import os
import socket
import subprocess
import sys
import time
import _bootstrap
import httpx
from backend.shared.settings import PORTS
from backend.shared.registry import SERVICES

def main():
    processes = []
    try:
        for module, port in PORTS.items():
            try:
                with socket.socket() as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"{port}不可用，未启动验证进程") from exc
        for module in PORTS:
            processes.append(subprocess.Popen(
                [sys.executable, str(_bootstrap.ROOT / "scripts/serve.py"), module],
                cwd=_bootstrap.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        deadline = time.monotonic() + 20
        waiting = set(PORTS)
        while waiting:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("验证进程提前退出")
            if time.monotonic() > deadline:
                raise RuntimeError("四模块启动验证超时")
            for module in list(waiting):
                base = f"http://127.0.0.1:{PORTS[module]}"
                try:
                    health = httpx.get(base + "/health", timeout=1, trust_env=False).json()["data"]
                    assert health["module"] == module.upper()
                    schema = httpx.get(base + "/openapi.json", timeout=1, trust_env=False).json()
                    for name in SERVICES[module]:
                        assert f"/api/v1/{module}/{name}" in schema["paths"]
                    name = next(iter(SERVICES[module]))
                    response = httpx.post(base + f"/api/v1/{module}/{name}", json={}, timeout=1, trust_env=False)
                    assert response.status_code == 503
                    assert response.json()["error"]["code"] == "UNAVAILABLE"
                    print(f"{module.upper()}: 启动、接口说明与未实现返回均符合约定")
                    waiting.remove(module)
                except httpx.TransportError:
                    pass
            if waiting:
                time.sleep(0.2)
        print("仅共同基础验证通过；业务仍未实现。")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

if __name__ == "__main__":
    main()
