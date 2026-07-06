FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# supercronic: container-friendly cron daemon used by the `scheduler` service.
# Single static binary; logs to stdout; proper signal handling. The same
# image runs both the worker (uvicorn entrypoint) and the scheduler (compose
# overrides command to invoke supercronic).
ARG SUPERCRONIC_VERSION=v0.2.29
ARG SUPERCRONIC_SHA256=87625cd179eff21226f0be6f2f47dd357037064598e6c1f9ffcbd0335d402bbd
RUN curl -fsSL -o /usr/local/bin/supercronic \
       "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake in static assets; runtime state goes to mounted volumes
COPY data/ data/
COPY sql/ sql/
COPY src/ src/
COPY config.json .
COPY crontab /app/crontab
COPY scripts/run-scheduler.sh /usr/local/bin/run-scheduler.sh
RUN chmod +x /usr/local/bin/run-scheduler.sh
# Operator-run analysis scripts only — scripts/client/ is frontend assets
# and stays out of the image.
COPY scripts/analyze_range_signal.py scripts/

EXPOSE 8080

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VEO_CONFIG=/app/config.json

# tini as PID 1 — handles SIGTERM cleanly for both uvicorn and supercronic
# children when docker compose stops the container. --proxy-headers +
# --forwarded-allow-ips="*" so the worker honors X-Forwarded-Proto from
# cloudflared (not 127.0.0.1); otherwise OAuth redirects construct http://.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
