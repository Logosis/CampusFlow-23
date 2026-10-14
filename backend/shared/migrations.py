from alembic import context
from sqlalchemy import create_engine, pool
from importlib import import_module
from backend.shared.settings import Settings

def run_migrations(module: str):
    url = Settings.load(module).database_url
    if not url:
        raise RuntimeError("数据库连接未配置")
    metadata = import_module(f"backend.{module}.models").Base.metadata
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
