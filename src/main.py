import sys
import threading

from .config import V1_POLY_PATH, V2_POLY_PATH
from .geometry import PolygonIndex, HAVE_SHAPELY
from .poll import run_loop, _log
from .server import run_server


def main() -> None:
    _log(f"veo-audit starting (shapely={HAVE_SHAPELY})")
    _log(f"Loading v1 polygon: {V1_POLY_PATH}")
    _log(f"Loading v2 polygon: {V2_POLY_PATH}")

    try:
        v1 = PolygonIndex(V1_POLY_PATH)
        v2 = PolygonIndex(V2_POLY_PATH)
    except Exception as exc:
        _log(f"FATAL: failed to load polygons: {exc!r}")
        sys.exit(2)

    flask_thread = threading.Thread(target=run_server, daemon=True, name="flask")
    flask_thread.start()
    _log("HTTP server started")

    run_loop(v1, v2)


if __name__ == "__main__":
    main()
