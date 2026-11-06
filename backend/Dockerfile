# TallyVeda backend as a container (for a self-hosted VM; Render keeps using render.yaml).
# Build from the backend folder:  docker build -t tallyveda-backend backend
FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TC_DATA_DIR=/data
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
RUN useradd --system --uid 10001 tallyveda && mkdir -p /data && chown tallyveda /data
USER tallyveda

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
# Behind Caddy: trust its X-Forwarded-For so per-IP throttles see the real client.
# The port is only published on 127.0.0.1, so nothing else can send those headers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\""]
