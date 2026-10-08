"""Every secret the deploy step declares must also be in its `envs:` list.

WHY THIS EXISTS. `.github/workflows/deploy.yml` hands secrets to the remote
host through `appleboy/ssh-action`, which copies ONLY the names listed in its
`envs:` parameter into the remote shell. A name added to the step's `env:` block
and forgotten in `envs:` expands to an empty string inside the `.env` heredoc —
silently, with the deploy still green. The workflow carries a comment saying
exactly that.

It then happened anyway: `VEO_PLACES_KEY` (sql/096, src/place_crypto.py) shipped
to `main` with the app depending on it and the deploy not passing it, which in
production means `place_crypto.configured()` is False and every saved-places
write 503s. Nothing failed; there was nothing to fail.

A comment cannot enforce itself, so this does. It is a lint on the workflow
file, not a test of any runtime behaviour, which is why it needs no database and
no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"

#: Names the remote shell gets from somewhere other than this step's `env:` —
#: injected by the action itself or set in the heredoc as a literal. Listed
#: explicitly so adding one is a decision somebody made on purpose.
_NOT_FROM_STEP_ENV: frozenset[str] = frozenset()


def _deploy_step() -> dict:
    spec = yaml.safe_load(WORKFLOW.read_text())
    steps = [
        step
        for job in spec["jobs"].values()
        for step in (job.get("steps") or [])
        if isinstance(step, dict) and "envs" in (step.get("with") or {})
    ]
    assert len(steps) == 1, (
        "expected exactly one step passing `envs:` to the ssh action; "
        f"found {len(steps)}. If the deploy grew a second one, this lint has "
        "to cover both."
    )
    return steps[0]


def _declared_and_passed() -> tuple[set[str], set[str]]:
    step = _deploy_step()
    declared = set((step.get("env") or {}).keys())
    passed = {
        name.strip()
        for name in str(step["with"]["envs"]).split(",")
        if name.strip()
    }
    return declared, passed


def test_every_declared_secret_is_actually_passed_to_the_host():
    declared, passed = _declared_and_passed()
    missing = sorted(declared - passed - _NOT_FROM_STEP_ENV)
    assert not missing, (
        "These are in the deploy step's `env:` but missing from its `envs:` "
        "list, so they reach the remote .env as EMPTY STRINGS and the deploy "
        f"still goes green: {missing}"
    )


def test_nothing_is_passed_that_is_never_declared():
    # The other direction. A name in `envs:` with no `env:` entry is already
    # empty today — either a leftover from a removed secret, or a typo for one
    # that is silently not being delivered.
    declared, passed = _declared_and_passed()
    unknown = sorted(passed - declared - _NOT_FROM_STEP_ENV)
    assert not unknown, (
        "These are listed in `envs:` but never declared in the step's `env:`, "
        f"so they arrive empty: {unknown}"
    )


def test_the_saved_places_key_reaches_production():
    """The specific one that got away, pinned by name.

    `src/place_crypto.py` requires it, has no dev fallback on purpose, and
    fails writes rather than degrading to plaintext — so an unset key is a
    rider-visible 503 on every saved-places write, not a quiet downgrade.
    """
    declared, passed = _declared_and_passed()
    for name in ("VEO_PLACES_KEY", "VEO_PLACES_KEY_OLD"):
        assert name in declared, f"{name} is not declared in the deploy step's env:"
        assert name in passed, f"{name} is not in the deploy step's envs: list"

    # And it has to land in the rendered .env, which is a heredoc in the script
    # rather than anything YAML can check structurally.
    script = _deploy_step()["with"]["script"]
    assert "VEO_PLACES_KEY=$VEO_PLACES_KEY" in script
    assert "VEO_PLACES_KEY_OLD=$VEO_PLACES_KEY_OLD" in script


def test_the_compose_file_forwards_it_into_the_container():
    # The last hop: the host .env is useless if compose does not pass the name
    # through to the app container.
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    )
    envs = [
        svc.get("environment") or {}
        for svc in compose["services"].values()
        if isinstance(svc, dict)
    ]
    assert any("VEO_PLACES_KEY" in e for e in envs), (
        "no service forwards VEO_PLACES_KEY into its container"
    )
