from functools import lru_cache
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.shared.settings import Settings

@lru_cache
def session_factory(module: str):
    url = Settings.load(module).database_url
    if not url:
        raise RuntimeError("数据库连接未配置")
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)
