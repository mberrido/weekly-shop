FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/shop.db \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    TZ=Europe/London

# tzdata so TZ=Europe/London gives the right "today" and week boundaries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Git commit the image was built from, shown by /healthz (set by CI; "dev" locally).
ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

# Default non-root user; docker-compose overrides the uid/gid with PUID/PGID.
RUN useradd --system --uid 1000 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app /data
USER 1000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# --proxy-headers: take the client IP from X-Forwarded-For, but only when the
# connection comes from FORWARDED_ALLOW_IPS (the Docker bridge gateway that
# DSM's reverse proxy / tailscale serve connect through).
CMD ["sh", "-c", "exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --no-server-header"]
