# Kasflow productie-image.
#
# The image contains the FastAPI app plus the static frontend, and runs as a
# non-root user. One worker keeps the in-process rate limiter predictable; see
# DEPLOY.md when you need to scale out.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY . .

RUN groupadd --system kasflow \
    && useradd --system --gid kasflow --home-dir /app --shell /usr/sbin/nologin kasflow \
    && mkdir -p /app/backend/uploads \
    && chown -R kasflow:kasflow /app

USER kasflow

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=4).status == 200 else 1)"]

CMD ["sh", "-c", "exec uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8010 --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
