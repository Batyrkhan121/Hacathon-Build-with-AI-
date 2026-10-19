# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_DOTENV_DISABLED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860 \
    LLM_ENABLED=false \
    QADAM_CACHE_DIR=/tmp/qadam-cache

WORKDIR /app

# No credentials or build-time API calls are needed to install dependencies.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 qadam \
    && useradd --uid 1000 --gid 1000 --create-home qadam \
    && mkdir -p /tmp/qadam-cache \
    && chown 1000:1000 /tmp/qadam-cache

# Explicit copies plus the context allowlist keep local files out of all layers.
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY web/ ./web/
COPY data/processed/ ./data/processed/
COPY data/index/ ./data/index/
COPY data/landmarks.json ./data/landmarks.json

USER 1000:1000
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import json, os, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:' + (os.environ.get('PORT') or '7860') + '/api/health', timeout=3); d = json.load(r); raise SystemExit(0 if d.get('status') in ('ok', 'degraded') and d.get('poi_count', 0) > 0 else 1)"]

# exec forwards termination signals; one worker avoids duplicating in-memory data.
CMD ["sh", "-c", "exec python -m uvicorn app.api:app --host 0.0.0.0 --port \"${PORT:-7860}\" --workers 1 --no-access-log"]
