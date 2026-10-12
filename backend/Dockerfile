# Built on the laptop, never on the VM. Build context is the repository root.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/backend

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/ ./
# The React console is built on the laptop (npm run build) and served as static files.
COPY frontend/dist/ /srv/frontend/dist/

# Run as non-root; data directories are owned by the app user so named volumes inherit it.
RUN useradd --system --uid 10001 --home /srv app \
    && mkdir -p /data/objects /data/spool \
    && chown -R app:app /data

USER app

ENV STORAGE_DIR=/data/objects \
    SPOOL_PATH=/data/spool/usage-events.jsonl \
    STATIC_DIR=/srv/frontend/dist

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers"]
