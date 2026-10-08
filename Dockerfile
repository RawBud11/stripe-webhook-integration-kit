# IntegrationKit API. Same Python minor version as the local venv (3.13).
FROM python:3.13.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime dependencies only (pinned in pyproject.toml); no dev or test extras.
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && rm -rf build

COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/uvicorn-logging.json ./docker/uvicorn-logging.json

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# Apply migrations, then serve. exec makes uvicorn PID 1 so it receives signals.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn integrationkit.app:create_app --factory --host 0.0.0.0 --port 8000 --log-config docker/uvicorn-logging.json"]
