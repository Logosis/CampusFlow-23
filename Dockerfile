FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
COPY scripts ./scripts
COPY tests/fixtures ./tests/fixtures
COPY alembic.ini ./
CMD ["uv", "run", "--no-sync", "python", "scripts/serve.py", "d", "--host", "0.0.0.0", "--port", "8000"]
