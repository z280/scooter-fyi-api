FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake in polygon data; DB lives on a mounted volume at /app/db/
COPY data/ data/
COPY src/ src/

VOLUME ["/app/db"]
EXPOSE 8080

ENV VEO_AUDIT_DIR=/app
ENV VEO_AUDIT_DB=/app/db/snapshots.db
ENV VEO_AUDIT_V1_POLY=/app/data/v1_opportunity_areas.geojson
ENV VEO_AUDIT_V2_POLY=/app/data/v2_opportunity_areas.geojson
ENV VEO_AUDIT_POLL_INTERVAL=300
ENV VEO_AUDIT_HEALTH_PORT=8080
ENV VEO_AUDIT_HEALTH_MAX_AGE=360

ENTRYPOINT ["python", "-m", "src.main"]
