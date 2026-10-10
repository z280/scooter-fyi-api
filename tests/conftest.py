"""Pytest fixtures + ensure src package importable + dummy env for config.load()."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Point config loader at the repo's config.json with absolute path overrides
# (the JSON references /app/data/... which only exists in the container).
os.environ.setdefault("VEO_CONFIG", str(ROOT / "config.json"))
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
# Deterministic salt for hash_plate() — tests assert exact output.
os.environ.setdefault("VEHICLE_IDENTIFIER_SALT", "pytest-fixed-salt")
# The precomputed map-payload cache (src/payload_cache.py) is off: fake-DB
# fixtures reuse one cycle_id with different rows, and a cached body would
# leak between tests. Off, every request builds fresh and nothing touches the
# payload_cache table; tests/test_payload_cache.py turns it on locally.
os.environ.setdefault("PAYLOAD_CACHE", "0")
