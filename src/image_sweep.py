"""Images must not outlive their records: the orphan sweep and account deletion.

The privacy policy says receipt images, model-report photos and ride
screenshots are deleted after 18 months, and that a deleted ride or account
is gone. The retention jobs in src/cli.py find images ONLY through table rows,
so any object whose row disappeared first — a cascade, a hand-run account
deletion, a crash between an upload and its INSERT — was invisible to them and
lived forever. This module closes that from both ends:

  * `sweep(apply=...)`         lists every user-image prefix in R2 and deletes
                                the objects no row references (weekly cron,
                                `python -m src.cli sweep_orphan_images --apply`);
  * `delete_account(id, ...)`  deletes an account row (everything cascades) and
                                then every image under that account's prefixes
                                (`python -m src.cli delete_account`).

WHERE IMAGES LIVE (verified against the writers, 2026-10-08):

  prefix              bucket                            writer
  receipts/           R2_RECEIPTS_BUCKET (private)      receipts.store_receipt
  model-reports/      R2_RECEIPTS_BUCKET (private)      receipts.store_model_photo
  ride-screenshots/   R2_RECEIPTS_BUCKET (private)      ride_screenshots.store_screenshot
  device-photos/      R2_BUCKET_NAME (archive bucket)   device_photos.store_device_photo

Every key is `<prefix><account_id>/<uuid>.jpg`.

WHAT COUNTS AS REFERENCED — every column that stores one of those keys
(`REFERENCE_COLUMNS`; tests/test_image_sweep.py fails if a migration adds an
`*r2_key` column that is not listed). A key in ANY of them protects the object
in EITHER bucket: the union is deliberately bucket-blind, so a misfiled key can
only ever make the sweep keep too much, never delete too much.

SAFETY RULES of the sweep:

  * dry-run unless `apply=True`;
  * the object listing is taken BEFORE the referenced set is read, so a row
    committed while the listing runs is still seen;
  * a 7-day grace on LastModified: an upload is PUT before its row INSERT
    commits, so a young unreferenced object may simply be in flight;
  * only keys of the exact shape `<prefix><digits>/<name>` are candidates;
    anything else under the prefix is counted as `unrecognised` and left;
  * a per-prefix circuit breaker: if more than half of a prefix's objects (and
    more than 20) look orphaned, that prefix is SKIPPED unless `force=True`,
    because that is what a broken reference query looks like;
  * logs and returns counts and byte totals only, never keys or account ids.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Iterator

from .pg import connection

log = logging.getLogger(__name__)

#: (table, column) of every column that stores an R2 object key for a user
#: image. Grep `r2_key` in sql/ when adding one; the test enforces it.
REFERENCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("discount_reports", "receipt_r2_key"),
    # Historical (sql/095): plan screenshots, stored under receipts/.
    ("discount_reports", "plan_evidence_r2_key"),
    ("model_reports", "photo_r2_key"),
    ("ride_transaction_screenshots", "r2_key"),
    ("device_photos", "r2_key"),
)

PRIVATE = "private"   # R2_RECEIPTS_BUCKET
ARCHIVE = "archive"   # R2_BUCKET_NAME

#: prefix -> which bucket it lives in.
PREFIXES: dict[str, str] = {
    "receipts/": PRIVATE,
    "model-reports/": PRIVATE,
    "ride-screenshots/": PRIVATE,
    "device-photos/": ARCHIVE,
}

GRACE = timedelta(days=7)

#: Circuit breaker: skip a prefix in apply mode when more than this share of
#: its objects (and more than BREAKER_MIN_COUNT of them) would be deleted.
BREAKER_FRACTION = 0.5
BREAKER_MIN_COUNT = 20

_KEY_SHAPE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in PREFIXES) + r")\d+/[^/]+$")

#: S3 DeleteObjects takes at most 1000 keys per call.
_DELETE_BATCH = 1000


# --- R2 access ---------------------------------------------------------------

@dataclass
class Bucket:
    """A bucket name plus the boto3 client that can reach it."""
    name: str
    client: Any


def resolve_buckets() -> dict[str, Bucket]:
    """Both buckets, through the same helpers the writers use, so the sweep
    can never look somewhere the uploads did not go. Raises if either is
    unconfigured: a sweep that silently skipped a bucket would report a
    clean run it never did."""
    from . import device_photos, receipts

    private, archive = receipts.receipts_bucket(), device_photos.device_photos_bucket()
    if not private or not archive:
        raise RuntimeError("R2 not configured: need R2 credentials, "
                           "R2_RECEIPTS_BUCKET and R2_BUCKET_NAME")
    return {PRIVATE: Bucket(private, receipts._r2_client()),
            ARCHIVE: Bucket(archive, device_photos._r2_client())}


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime


def list_objects(bucket: Bucket, prefix: str) -> Iterator[StoredObject]:
    paginator = bucket.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket.name, Prefix=prefix):
        for obj in page.get("Contents") or []:
            yield StoredObject(obj["Key"], int(obj.get("Size") or 0),
                               obj["LastModified"])


def delete_keys(bucket: Bucket, keys: list[str]) -> tuple[int, int]:
    """Batch delete. Returns (deleted, failed). Never logs a key."""
    deleted = failed = 0
    for i in range(0, len(keys), _DELETE_BATCH):
        chunk = keys[i:i + _DELETE_BATCH]
        try:
            resp = bucket.client.delete_objects(
                Bucket=bucket.name,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
        except Exception as exc:  # noqa: BLE001 — count it, keep going
            log.warning("R2 batch delete failed (%s)", type(exc).__name__)
            failed += len(chunk)
            continue
        errors = len((resp or {}).get("Errors") or [])
        failed += errors
        deleted += len(chunk) - errors
    return deleted, failed


# --- references --------------------------------------------------------------

def referenced_keys(cur) -> set[str]:
    """Every stored key in every REFERENCE_COLUMNS column. Raises on any
    query failure — an incomplete set must never reach the delete step."""
    keys: set[str] = set()
    for table, column in REFERENCE_COLUMNS:
        # Identifiers come from the constant above, never from input.
        cur.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
        keys.update(r[0] for r in cur.fetchall() if r[0])
    return keys


# --- the sweep ---------------------------------------------------------------

@dataclass
class PrefixReport:
    listed: int = 0
    listed_bytes: int = 0
    referenced: int = 0
    unrecognised: int = 0
    in_grace: int = 0
    orphaned: int = 0
    orphaned_bytes: int = 0
    deleted: int = 0
    failed: int = 0
    skipped_by_breaker: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def sweep(*, apply: bool = False, force: bool = False,
          buckets: dict[str, Bucket] | None = None,
          referenced: Callable[[], set[str]] | None = None,
          now: datetime | None = None) -> dict[str, Any]:
    """Find (and with `apply`, delete) unreferenced user images.

    `buckets` / `referenced` / `now` are injection points for tests; in
    production they resolve to R2 and the live tables.
    """
    buckets = buckets or resolve_buckets()
    now = now or datetime.now(timezone.utc)
    cutoff = now - GRACE

    # 1. List first (see the module docstring for why the order matters).
    listings: dict[str, list[StoredObject]] = {
        prefix: list(list_objects(buckets[kind], prefix))
        for prefix, kind in PREFIXES.items()
    }

    # 2. Then the referenced set.
    if referenced is None:
        with connection() as conn:
            with conn.cursor() as cur:
                refs = referenced_keys(cur)
            conn.rollback()
    else:
        refs = referenced()

    # 3. Classify and (maybe) delete, per prefix.
    reports: dict[str, PrefixReport] = {}
    for prefix, kind in PREFIXES.items():
        rep = PrefixReport()
        orphans: list[StoredObject] = []
        for obj in listings[prefix]:
            rep.listed += 1
            rep.listed_bytes += obj.size
            if obj.key in refs:
                rep.referenced += 1
            elif not _KEY_SHAPE.match(obj.key) or not obj.key.startswith(prefix):
                rep.unrecognised += 1
            elif obj.last_modified > cutoff:
                rep.in_grace += 1
            else:
                orphans.append(obj)
        rep.orphaned = len(orphans)
        rep.orphaned_bytes = sum(o.size for o in orphans)

        tripped = (rep.orphaned > BREAKER_MIN_COUNT
                   and rep.orphaned > BREAKER_FRACTION * rep.listed)
        if apply and orphans:
            if tripped and not force:
                rep.skipped_by_breaker = True
                log.warning(
                    "sweep_orphan_images: %s would delete %d of %d objects; "
                    "skipped by the circuit breaker (re-run with --force after "
                    "checking the dry-run)", prefix, rep.orphaned, rep.listed)
            else:
                # Re-check membership at the last moment: belt and braces
                # against a future refactor that reorders steps 1-3.
                keys = [o.key for o in orphans if o.key not in refs]
                rep.deleted, rep.failed = delete_keys(buckets[kind], keys)
        reports[prefix] = rep
        log.info(
            "sweep_orphan_images: %s listed=%d (%d bytes) referenced=%d "
            "in_grace=%d unrecognised=%d orphaned=%d (%d bytes) deleted=%d "
            "failed=%d%s", prefix, rep.listed, rep.listed_bytes, rep.referenced,
            rep.in_grace, rep.unrecognised, rep.orphaned, rep.orphaned_bytes,
            rep.deleted, rep.failed,
            " SKIPPED(breaker)" if rep.skipped_by_breaker else "")

    totals = PrefixReport()
    for rep in reports.values():
        for k, v in rep.as_dict().items():
            if isinstance(v, bool):
                setattr(totals, k, getattr(totals, k) or v)
            else:
                setattr(totals, k, getattr(totals, k) + v)
    return {"dry_run": not apply, "force": force, "grace_days": GRACE.days,
            "prefixes": {p: r.as_dict() for p, r in reports.items()},
            "totals": totals.as_dict()}


# --- account deletion ----------------------------------------------------------

def _account_row_keys(cur, account_id: int) -> dict[str, set[str]]:
    """Keys the account's rows reference, by bucket kind."""
    out: dict[str, set[str]] = {PRIVATE: set(), ARCHIVE: set()}
    queries: Iterable[tuple[str, str]] = (
        (PRIVATE, "SELECT receipt_r2_key FROM discount_reports WHERE account_id = %s"),
        (PRIVATE, "SELECT plan_evidence_r2_key FROM discount_reports WHERE account_id = %s"),
        (PRIVATE, "SELECT photo_r2_key FROM model_reports WHERE account_id = %s"),
        (PRIVATE, "SELECT r2_key FROM ride_transaction_screenshots WHERE account_id = %s"),
        (ARCHIVE, "SELECT r2_key FROM device_photos WHERE account_id = %s"),
    )
    for kind, sql in queries:
        cur.execute(sql, (account_id,))
        out[kind].update(r[0] for r in cur.fetchall() if r[0])
    return out


def delete_account(account_id: int, *, apply: bool = False,
                   buckets: dict[str, Bucket] | None = None) -> dict[str, Any]:
    """Delete one account and every image it uploaded.

    Order, and why:
      1. read the image keys its rows reference (the cascade erases them);
      2. in ONE transaction: detach its model-report photos (model_reports
         rows survive an account delete as ON DELETE SET NULL, keeping the
         catalog correction, so their key must be cleared and stamped or the
         sweep would treat the photo as referenced forever), drop its
         outstanding login codes / magic links, delete the account row —
         every other table cascades (sql/012 onward);
      3. after COMMIT, delete the referenced objects plus anything else under
         `<prefix><account_id>/` in either bucket (an earlier orphan). No grace
         period here: the account and its sessions are gone, so nothing can be
         in flight for it.

    A failure in step 3 leaves only unreferenced objects, which the weekly
    sweep removes. Works when the account row is already gone (a deletion
    done by hand before this command existed): steps 1-2 find nothing and
    step 3 still clears the prefixes.

    Dry-run (default) rolls back and reports counts only.
    """
    if account_id <= 0:
        raise ValueError("account id must be a positive integer")
    buckets = buckets or resolve_buckets()

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email, phone_number FROM accounts WHERE id = %s FOR UPDATE",
                        (account_id,))
            row = cur.fetchone()
            exists = row is not None
            email, phone = (row or (None, None))
            row_keys = _account_row_keys(cur, account_id)

            detached_model_photos = deleted_codes = deleted_links = 0
            if apply:
                cur.execute(
                    "UPDATE model_reports SET photo_r2_key = NULL, "
                    "       photo_deleted_at = COALESCE(photo_deleted_at, NOW()) "
                    "WHERE photo_r2_key IS NOT NULL "
                    "  AND (account_id = %s OR photo_r2_key LIKE %s)",
                    (account_id, f"model-reports/{account_id}/%"),
                )
                detached_model_photos = cur.rowcount
                if email:
                    cur.execute("DELETE FROM magic_link_tokens WHERE lower(email) = lower(%s)",
                                (email,))
                    deleted_links = cur.rowcount
                cur.execute(
                    "DELETE FROM login_codes WHERE (%s::text IS NOT NULL AND lower(email) = lower(%s)) "
                    "   OR (%s::text IS NOT NULL AND phone_number = %s)",
                    (email, email, phone, phone),
                )
                deleted_codes = cur.rowcount
                if exists:
                    cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
                conn.commit()
            else:
                conn.rollback()

    # Step 3: referenced keys + everything under this account's prefixes.
    targets: dict[str, set[str]] = {PRIVATE: set(row_keys[PRIVATE]),
                                    ARCHIVE: set(row_keys[ARCHIVE])}
    found_bytes = 0
    for prefix, kind in PREFIXES.items():
        for obj in list_objects(buckets[kind], f"{prefix}{account_id}/"):
            targets[kind].add(obj.key)
            found_bytes += obj.size

    deleted = failed = 0
    if apply:
        for kind, keys in targets.items():
            d, f = delete_keys(buckets[kind], sorted(keys))
            deleted, failed = deleted + d, failed + f

    result = {
        "dry_run": not apply,
        "account_existed": exists,
        "row_referenced_images": sum(len(v) for v in row_keys.values()),
        "images_targeted": sum(len(v) for v in targets.values()),
        "listed_bytes_under_prefixes": found_bytes,
        "model_report_photos_detached": detached_model_photos,
        "login_codes_deleted": deleted_codes,
        "magic_links_deleted": deleted_links,
        "images_deleted": deleted,
        "images_failed": failed,
    }
    log.info("delete_account: %s", {k: v for k, v in result.items()})
    return result
