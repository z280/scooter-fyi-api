"""Decode a map route's response in tests.

/api/v1/devices/current, /user/devices/current and /h3/aggregates return
precomputed bytes (src/payload_cache.py) — gzip when the request accepts it —
rather than a dict for FastAPI to encode. Called directly, the handler hands
back that Response; this turns it into the JSON a client would parse, and
passes a 304 (or anything that is not a body) through unchanged.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from fastapi import Response


def decoded(out: Any) -> Any:
    if not isinstance(out, Response) or out.status_code != 200:
        return out
    body = out.body
    if out.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    return json.loads(body)
