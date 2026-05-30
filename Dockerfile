FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake in static assets; runtime state goes to mounted volumes
COPY data/ data/
COPY sql/ sql/
COPY src/ src/
COPY config.json .

EXPOSE 8080

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VEO_CONFIG=/app/config.json

# --proxy-headers + --forwarded-allow-ips="*" so the worker honors
# X-Forwarded-Proto/For from cloudflared (which sits on the same Docker
# network, not 127.0.0.1). Without these flags, FastAPI's url_for and
# request.url.scheme would report "http" even on HTTPS requests, breaking
# OAuth redirects and any other URL construction.
ENTRYPOINT ["uvicorn", "src.main:app", \
            "--host", "0.0.0.0", "--port", "8080", \
            "--proxy-headers", "--forwarded-allow-ips=*"]
