import logging
from datetime import datetime, timezone

from flask import Flask, jsonify

logging.getLogger("werkzeug").setLevel(logging.ERROR)

from .config import HEALTH_MAX_AGE, HEALTH_PORT
from .db import get_latest

app = Flask(__name__)


@app.route("/health")
def health():
    row = get_latest()
    if row is None:
        return jsonify({"status": "no_data"}), 503

    last_dt = datetime.fromisoformat(row["ts_utc"])
    age = (datetime.now(timezone.utc) - last_dt).total_seconds()

    if age <= HEALTH_MAX_AGE:
        return jsonify({"status": "ok", "last_ts": row["ts_utc"], "age_seconds": int(age)}), 200

    return jsonify({"status": "stale", "last_ts": row["ts_utc"], "age_seconds": int(age)}), 500


@app.route("/latest")
def latest():
    row = get_latest()
    if row is None:
        return jsonify({"error": "no data yet"}), 503
    return jsonify(row), 200


def run_server() -> None:
    app.run(host="0.0.0.0", port=HEALTH_PORT, use_reloader=False)
