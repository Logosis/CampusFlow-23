from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("d", "m1", "m2", "m3")
PORTS = {"d": 8010, "m1": 8011, "m2": 8012, "m3": 8013}

@dataclass(frozen=True)
class Settings:
    module: str
    app_env: str
    database_url: str
    peer_urls: dict[str, str]
    credentials: dict[str, str]
    peer_timeout: float

    @classmethod
    def load(cls, module: str):
        if module not in MODULES:
            raise ValueError("Unknown module")
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            module=module,
            app_env=os.getenv("APP_ENV", "development"),
            database_url=os.getenv("DATABASE_URL") or os.getenv(f"{module.upper()}_DATABASE_URL", ""),
            peer_urls={m: os.getenv(f"{m.upper()}_BASE_URL", f"http://127.0.0.1:{PORTS[m]}").rstrip("/") for m in MODULES},
            credentials={m: os.getenv(f"SERVICE_CREDENTIAL_{m.upper()}", "") for m in MODULES},
            peer_timeout=float(os.getenv("PEER_TIMEOUT_SECONDS", "5")),
        )
