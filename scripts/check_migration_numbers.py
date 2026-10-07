#!/usr/bin/env python3
"""Fail when two migrations claim the same sql/NNN_ number.

WHY THIS IS A LINT AND NOT A REVIEW COMMENT. The number has now drifted twice:
once in a plan written against a stale local checkout, and once when PR #105
added `sql/088_discount_reports_equity_areas.sql` while `main` already carried
`sql/088_standardise_movement_radius.sql`. A reviewer who catches that most of
the time is strictly worse than a check that catches it every time, because the
thing being checked is a fact about filenames.

WHY A COLLISION IS NOT MERELY UNTIDY. `src/pg.py`'s `run_migrations` tracks
applied files BY FILENAME and applies them in `sorted()` order, so two files
sharing a number both apply — ordered by whichever suffix happens to sort
first, not by intent. Worse, the orderings diverge: a fresh CI database applies
both in one alphabetical pass, while production applied one of them weeks ago
and picks up the other on the next deploy. If they touch the same object, CI
and production end up with different effective histories, and CI is the one
that looks fine.

TWO CHECKS, because `main` cannot see every way this happens:

  * against main (always, no credentials) — the PR adds a number that already
    exists upstream. This is the #105 case, and it is invisible on the branch
    alone until someone merges main in, which is exactly when it is too late to
    be cheap.
  * against other OPEN PRs (only with a token) — two branches independently
    picking the same next number, neither colliding with main. Nothing in a
    single checkout can see this.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict

NAME = re.compile(r"^sql/(\d{3})_[A-Za-z0-9_.-]+\.sql$")
API = "https://api.github.com"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def numbered(paths: list[str]) -> dict[str, list[str]]:
    """sql/NNN_*.sql paths grouped by NNN. Anything else is ignored rather than
    rejected: this check owns numbering, not naming."""
    out: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        if m := NAME.match(p.strip()):
            out[m.group(1)].append(p.strip())
    return out


def collisions(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    return {n: sorted(set(f)) for n, f in groups.items() if len(set(f)) > 1}


def next_free(groups: dict[str, list[str]]) -> str:
    return f"{max((int(n) for n in groups), default=0) + 1:03d}"


def head_and_base(base: str) -> tuple[dict[str, list[str]], set[str]]:
    """(every numbered migration visible from HEAD or `base`, the ones THIS
    branch adds).

    The union is the point. A branch that has not merged the base in does not
    contain the base's newest migrations, so comparing HEAD against itself
    cannot see the collision this exists to catch.

    The second return value is what makes the check adoptable. `main` already
    carries two benign collisions (061 and 069 — each pair touches disjoint
    objects, which is why nobody noticed). A check that failed every PR over
    somebody else's old filename would be turned off within a day, so only
    numbers THIS branch claims are blocking.
    """
    # TRACKED FILES ONLY, plus untracked-but-present ones. `ls-files` alone
    # misses a migration that exists on disk and has not been `git add`ed, which
    # is the state a developer is in at the moment they would most like to be
    # told. CI is unaffected either way (a clean checkout has nothing untracked),
    # so this is purely so the local run answers the same question CI will.
    here = [l.strip() for l in _git("ls-files", "sql").splitlines() if l.strip()]
    here += [l.strip() for l in
             _git("ls-files", "--others", "--exclude-standard", "sql").splitlines()
             if l.strip()]
    try:
        there = [l.strip() for l in
                 _git("ls-tree", "-r", "--name-only", base, "sql").splitlines() if l.strip()]
    except subprocess.CalledProcessError:
        print(
            f"warn: {base} not available — checking HEAD only. In CI this means\n"
            f"      actions/checkout needs fetch-depth: 0 (or an explicit fetch\n"
            f"      of {base}), or the upstream half of this check does nothing.",
            file=sys.stderr,
        )
        there = []
    return numbered(here + there), set(here) - set(there)


def _api(path: str, token: str) -> list[dict]:
    """Every page, following `Link: rel="next"`.

    An earlier version sent `per_page=100` and stopped there, so a repo with
    more than 100 open PRs — or a PR whose 101st changed file was the colliding
    migration — was silently under-checked. A check whose failure mode is
    "quietly looked at less than you think" is the failure mode this whole
    script exists to remove, so it pages properly rather than documenting a cap.
    """
    out: list[dict] = []
    url = f"{API}{path}"
    while url:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "check-migration-numbers",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            page = json.load(r)
            out.extend(page if isinstance(page, list) else [page])
            url = _next_link(r.headers.get("Link", ""))
    return out


def _next_link(header: str) -> str | None:
    """The `rel="next"` URL from a GitHub Link header, or None on the last page."""
    for part in header.split(","):
        if 'rel="next"' in part and "<" in part and ">" in part:
            return part[part.index("<") + 1:part.index(">")]
    return None


def open_pr_migrations(repo: str, token: str, skip_pr: str | None) -> dict[str, list[str]]:
    """{NNN: ["#105 sql/088_...", ...]} for migrations ADDED by open PRs.

    Added only: a PR that merely has the file because it branched late is not
    claiming the number, and counting those would flag every PR against every
    other one.
    """
    out: dict[str, list[str]] = defaultdict(list)
    for pr in _api(f"/repos/{repo}/pulls?state=open&per_page=100", token):
        num = str(pr["number"])
        if skip_pr and num == skip_pr:
            continue
        for f in _api(f"/repos/{repo}/pulls/{num}/files?per_page=100", token):
            if f.get("status") != "added":
                continue
            if m := NAME.match(f.get("filename", "")):
                out[m.group(1)].append(f"#{num} {f['filename']}")
    return out


def main() -> int:
    base = os.environ.get("MIGRATION_BASE_REF", "origin/main")
    repo = os.environ.get("GITHUB_REPOSITORY", "z280/scooter-fyi-api")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    this_pr = os.environ.get("PR_NUMBER") or None

    groups, mine = head_and_base(base)

    if token:
        try:
            for n, where in open_pr_migrations(repo, token, this_pr).items():
                groups.setdefault(n, []).extend(where)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as e:
            # A cross-PR check that cannot reach the API must not fail the build:
            # the local half still ran, and a flaky network is not a collision.
            print(f"warn: cross-PR check skipped ({type(e).__name__}: {e})", file=sys.stderr)
    else:
        print(
            f"note: no GITHUB_TOKEN — checked against {base} only. Two open PRs "
            "both adding the same new number will not be caught.",
            file=sys.stderr,
        )

    bad = collisions(groups)
    # Blocking only where THIS branch is one of the claimants. Another PR's
    # collision is blocked on that PR, where somebody can actually fix it.
    blocking = {n: f for n, f in bad.items() if any(x in mine for x in f)}
    other = {n: f for n, f in bad.items() if n not in blocking}

    for n in sorted(other):
        files = ", ".join(sorted(set(other[n])))
        print(f"warn: {n} is claimed twice, not by this branch: {files}", file=sys.stderr)

    if not blocking:
        print(f"migration numbers OK ({len(groups)} numbers; next free: {next_free(groups)})")
        return 0

    print("\nDUPLICATE MIGRATION NUMBER ADDED BY THIS BRANCH\n", file=sys.stderr)
    for n in sorted(blocking):
        print(f"  {n}:", file=sys.stderr)
        for f in sorted(set(blocking[n])):
            print(f"      {f}{'   <-- yours' if f in mine else ''}", file=sys.stderr)
    print(
        f"\nRename yours to {next_free(groups)} or the next free number above it.\n"
        "Both files WILL apply (src/pg.py tracks by filename), ordered by whichever\n"
        "suffix sorts first — and a database that already applied one of them picks\n"
        "up the other later, so CI's order is not production's.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
