"""Lock in the CORS regex behavior so a config typo can't silently
break Cloudflare Pages preview deploys."""

import re

from src.config import load


def test_pages_dev_preview_origins_match():
    cfg = load()
    assert cfg.cors_origin_patterns, "expected at least one regex pattern"
    combined = "|".join(f"(?:{p})" for p in cfg.cors_origin_patterns)
    pat = re.compile(combined)

    # Cloudflare Pages preview URLs we want to allow
    matching = [
        "https://abc123ef.denver-scooter-fyi.pages.dev",
        "https://feature-branch.denver-scooter-fyi.pages.dev",
        "https://pr-42.denver-scooter-fyi.pages.dev",
        "https://main.denver-scooter-fyi.pages.dev",
    ]
    for o in matching:
        assert pat.fullmatch(o), f"should match: {o}"


def test_unrelated_origins_do_not_match():
    cfg = load()
    combined = "|".join(f"(?:{p})" for p in cfg.cors_origin_patterns)
    pat = re.compile(combined)

    # Anything we definitely do NOT want to allow
    rejecting = [
        "https://denver-scooter-fyi.pages.dev",   # bare (no subdomain) — covered by exact-match list, not regex
        "https://evil.example",
        "http://abc.denver-scooter-fyi.pages.dev",   # http, not https
        "https://abc.denver-scooter-fyi.pages.dev.evil.com",  # suffix-spoof attempt
        "https://abc.other-project.pages.dev",
        "https://denver-scooter-fyi.pages.dev/path",  # path tacked on
    ]
    for o in rejecting:
        assert not pat.fullmatch(o), f"should NOT match: {o}"
