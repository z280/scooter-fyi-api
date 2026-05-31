"""Extract the real client IP behind a reverse proxy.

The production deployment runs FastAPI behind a Cloudflare Tunnel
(`cloudflared` container). With that topology, `request.client.host`
is the loopback IP of the cloudflared sidecar, NOT the real reporter.
Anything that stores or rate-limits on `reporter_ip` must use a
forwarded header instead.

Preference order:

  1. CF-Connecting-IP    — Cloudflare's gold standard. Cloudflare strips
                            any client-supplied value at the edge and
                            sets this from the real TCP connection, so
                            spoofing is not possible when traffic
                            actually traverses Cloudflare. Trust it
                            unconditionally on this deployment.
  2. X-Forwarded-For     — first value (leftmost = original client per
                            convention). Used as a fallback for non-CF
                            environments (local dev behind nginx, etc).
  3. request.client.host — direct TCP peer. Last resort; the value will
                            be wrong behind any proxy but at least
                            indicates SOMETHING was recorded.

For development without any proxy, all three coincide.
"""

from __future__ import annotations

from fastapi import Request


def real_client_ip(request: Request) -> str | None:
    """Return the best-guess real client IP for the request, or None."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # XFF is "client, proxy1, proxy2, ..." — leftmost is the original.
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else None
