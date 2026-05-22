import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("VEO_AUDIT_DIR", os.path.dirname(HERE))

V1_POLY_PATH = os.environ.get(
    "VEO_AUDIT_V1_POLY",
    os.path.join(ROOT, "data", "v1_opportunity_areas.geojson"),
)
V2_POLY_PATH = os.environ.get(
    "VEO_AUDIT_V2_POLY",
    os.path.join(ROOT, "data", "v2_opportunity_areas.geojson"),
)
DB_PATH = os.environ.get(
    "VEO_AUDIT_DB",
    os.path.join(ROOT, "db", "snapshots.db"),
)
GBFS_URL = os.environ.get(
    "VEO_AUDIT_GBFS_URL",
    "https://cluster-prod.veoride.com/api/shares/name/den/gbfs/free_bike_status",
)
TIMEOUT = int(os.environ.get("VEO_AUDIT_TIMEOUT", "30"))
POLL_INTERVAL = int(os.environ.get("VEO_AUDIT_POLL_INTERVAL", "300"))  # 5 minutes
HEALTH_PORT = int(os.environ.get("VEO_AUDIT_HEALTH_PORT", "8080"))
HEALTH_MAX_AGE = int(os.environ.get("VEO_AUDIT_HEALTH_MAX_AGE", "360"))  # 6 minutes

# Rough Denver bounding box — filters out garbage coordinates (~22°N 114°E, etc.)
DENVER_BBOX = (-105.3, 39.5, -104.7, 40.0)  # (min_lon, min_lat, max_lon, max_lat)

CONTRACT_PCT = 30.0
