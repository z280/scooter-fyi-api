"""Sentry SDK init + cron-monitor decorator helpers.

No-op when SENTRY_DSN is unset — every helper here checks _INITIALIZED
first, so unconfigured environments (local dev, smoke tests) pass
through without raising.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from .config import sentry_dsn

log = logging.getLogger(__name__)

_INITIALIZED = False


def init() -> bool:
    global _INITIALIZED
    if _INITIALIZED:
        return True
    dsn = sentry_dsn()
    if not dsn:
        log.info("SENTRY_DSN not set — Sentry instrumentation disabled.")
        return False
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0.0,
        send_default_pii=False,
        integrations=[FastApiIntegration()],
    )
    _INITIALIZED = True
    return True


def set_cycle_tag(cycle_id: str) -> None:
    if not _INITIALIZED:
        return
    sentry_sdk.set_tag("cycle_id", cycle_id)


def capture_exception(exc: BaseException) -> None:
    if not _INITIALIZED:
        return
    sentry_sdk.capture_exception(exc)


def monitor(slug: str, monitor_config: dict[str, Any]) -> Callable:
    """Decorator that wires a function up as a Sentry cron monitor.

    Sentry auto-creates the monitor on first check-in using monitor_config,
    so no manual setup in the Sentry UI is required. The decorator is a
    no-op when SENTRY_DSN is unset, so this is safe to apply unconditionally
    to CLI subcommands.

    Status semantics:
      - 'in_progress' check-in sent when the function starts
      - 'ok' on normal return
      - 'error' if the function raises (the exception is also re-raised so
        the CLI exit code still reflects failure)
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _INITIALIZED:
                return fn(*args, **kwargs)
            check_in_id = sentry_sdk.crons.capture_checkin(
                monitor_slug=slug,
                status="in_progress",
                monitor_config=monitor_config,
            )
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                sentry_sdk.crons.capture_checkin(
                    monitor_slug=slug,
                    check_in_id=check_in_id,
                    status="error",
                    monitor_config=monitor_config,
                )
                raise
            else:
                sentry_sdk.crons.capture_checkin(
                    monitor_slug=slug,
                    check_in_id=check_in_id,
                    status="ok",
                    monitor_config=monitor_config,
                )
                return result

        return wrapper

    return decorator
