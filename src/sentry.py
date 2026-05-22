"""Sentry SDK init — no-op when SENTRY_DSN is unset."""

from __future__ import annotations

import logging

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
