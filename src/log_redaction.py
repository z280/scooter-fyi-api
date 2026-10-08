"""Query-string redaction for logs and Sentry, for routes whose parameters are
personal data.

Two routes take something a rider would not want written down in a request
line:

  * `GET /api/v1/vehicles/resolve?plate=…`  — a vehicle plate;
  * `GET /api/v1/geocode/reverse?lat=…&lng=…` — a coordinate, which for a
    saved place IS the rider's home or workplace.

Neither handler logs its parameters. What would is everything AROUND the
handler, so each of these is covered here, in one place:

  * uvicorn's access log, which records the full request line
    (`RedactSensitiveQuery` on the `uvicorn.access` logger);
  * httpx's own INFO line for the upstream call the reverse geocoder makes,
    `HTTP Request: GET http://photon:2322/reverse?lat=…&lon=…` — the root
    logger runs at INFO, so without this every reverse lookup would land in
    the container log anyway (the same filter on the `httpx` logger);
  * Sentry, which records the request's query string on an error event and
    the upstream URL in an http breadcrumb, independently of logging
    (`scrub_sentry_event` / `scrub_sentry_breadcrumb`, wired in src/sentry.py).

Matching is on the exact path, so every other line is untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

#: path -> the query parameters to redact on it.
REDACTED_QUERY_PARAMS: dict[str, tuple[str, ...]] = {
    "/api/v1/vehicles/resolve": ("plate",),
    "/api/v1/geocode/reverse": ("lat", "lng"),
    # The Photon sidecar's reverse endpoint, as httpx and Sentry see the
    # upstream call (`http://photon:2322/reverse?lat=…&lon=…`). Photon spells
    # it `lon`; `lng` is listed too so a client-style spelling is covered.
    "/reverse": ("lat", "lon", "lng"),
}

REDACTED = "[redacted]"

_PARAM_RES: dict[str, re.Pattern[str]] = {
    path: re.compile(r"((?:^|[?&])(?:" + "|".join(map(re.escape, params)) + r")=)[^&#]*")
    for path, params in REDACTED_QUERY_PARAMS.items()
}


def _keep_name(m: re.Match[str]) -> str:
    return m.group(1) + REDACTED


def _path_of(url_or_path: str) -> str:
    """The path component of a full URL or of a bare `/path?query`."""
    head = url_or_path.split("?", 1)[0].split("#", 1)[0]
    if "://" in head:
        head = urlsplit(head).path
    return head.rstrip("/") or "/"


def redact_query(path: str, query: str) -> str:
    """Redact `query` (no leading `?`) if `path` is a covered route."""
    pattern = _PARAM_RES.get(_path_of(path))
    if pattern is None or not query:
        return query
    return pattern.sub(_keep_name, query)


def redact_url(url_or_path: str) -> str:
    """`/api/v1/geocode/reverse?lat=39.7&lng=-105` ->
    `/api/v1/geocode/reverse?lat=[redacted]&lng=[redacted]`. Any other
    path, or a path with no query string, comes back unchanged."""
    if "?" not in url_or_path:
        return url_or_path
    head, query = url_or_path.split("?", 1)
    pattern = _PARAM_RES.get(_path_of(head))
    if pattern is None:
        return url_or_path
    return f"{head}?{pattern.sub(_keep_name, query)}"


def _redact_arg(arg: Any) -> Any:
    # uvicorn passes the request path as a str; httpx passes an httpx.URL.
    # Matching on the class name keeps httpx out of this module's imports.
    if isinstance(arg, str):
        return redact_url(arg) if "?" in arg else arg
    if type(arg).__name__ == "URL":
        text = str(arg)
        redacted = redact_url(text)
        return redacted if redacted != text else arg
    return arg


class RedactSensitiveQuery(logging.Filter):
    """Rewrites any URL-shaped argument of a log record whose path is in
    REDACTED_QUERY_PARAMS.

    uvicorn.access logs with args (client, method, full_path, http_version,
    status); httpx logs `HTTP Request: %s %s "%s %d %s"` with the URL second.
    Only the matching argument is replaced, so every other access line is
    untouched, and the filter never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            new = tuple(_redact_arg(a) for a in args)
            if any(n is not o for n, o in zip(new, args)):
                record.args = new
        return True


def install() -> None:
    """Attach the filter to every logger that writes a request URL. Called
    once from src/main.py at import time (uvicorn configures its loggers
    before importing the app, so a filter added then sticks). Idempotent."""
    for name in ("uvicorn.access", "httpx"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactSensitiveQuery) for f in logger.filters):
            logger.addFilter(RedactSensitiveQuery())


# --- Sentry ------------------------------------------------------------------

def scrub_sentry_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """`before_send`: redact the request query string (and a URL carrying
    one) on an error raised while serving a covered route.
    send_default_pii=False does not cover query strings; Sentry sends them."""
    try:
        req = event.get("request")
        if isinstance(req, dict):
            url = req.get("url") or ""
            if isinstance(req.get("query_string"), str):
                req["query_string"] = redact_query(url, req["query_string"])
            if isinstance(url, str) and "?" in url:
                req["url"] = redact_url(url)
        crumbs = event.get("breadcrumbs")
        values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
        if isinstance(values, list):
            for crumb in values:
                if isinstance(crumb, dict):
                    scrub_sentry_breadcrumb(crumb)
    except Exception:  # noqa: BLE001 — scrubbing must never drop the event
        pass
    return event


def scrub_sentry_breadcrumb(crumb: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """`before_breadcrumb`: httpx/stdlib http breadcrumbs carry the upstream
    URL in `data.url` and its query in `data["http.query"]`."""
    try:
        data = crumb.get("data")
        if isinstance(data, dict):
            url = data.get("url")
            if isinstance(url, str):
                if isinstance(data.get("http.query"), str):
                    data["http.query"] = redact_query(url, data["http.query"])
                data["url"] = redact_url(url)
        msg = crumb.get("message")
        if isinstance(msg, str) and "?" in msg:
            crumb["message"] = " ".join(redact_url(part) for part in msg.split(" "))
    except Exception:  # noqa: BLE001
        pass
    return crumb
