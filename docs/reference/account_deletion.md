# Deleting an account

There is no self-service account deletion endpoint. A rider who asks for
their account to be deleted is handled by an operator with one CLI command,
which deletes the database rows **and** the images the rider uploaded.
Deleting the `accounts` row by hand is not enough: the rows cascade, but the
image files in R2 are not rows, and nothing would ever find them again.

## What an account owns

| Data | Where | What happens on delete |
|---|---|---|
| Sessions, rides, tracked rides, waypoints, points, preferences, favourites, discount reports, device photos, ride screenshots, ride surveys and routes, track donations not yet de-identified | Postgres, `account_id … ON DELETE CASCADE` | Deleted with the account row |
| Device reports, device-feature reports, model reports, route feedback, QR first/last-scanned-by | Postgres, `ON DELETE SET NULL` | Kept, de-linked from the account. Model-report photos are deleted (below) |
| Receipt images and historical plan screenshots (`receipts/<id>/…`) | R2, `R2_RECEIPTS_BUCKET` (private) | Deleted by the command |
| Model-report photos (`model-reports/<id>/…`) | R2, `R2_RECEIPTS_BUCKET` (private) | Deleted; the report row keeps its text with `photo_r2_key` cleared and `photo_deleted_at` stamped |
| Ride transaction screenshots (`ride-screenshots/<id>/…`) | R2, `R2_RECEIPTS_BUCKET` (private) | Deleted by the command |
| Device photos (`device-photos/<id>/…`) | R2, `R2_BUCKET_NAME` (the archive bucket) | Deleted by the command |
| Outstanding sign-in codes and magic links | `login_codes`, `magic_link_tokens` (keyed by email / phone, no FK) | Deleted by the command |

## Steps

Run inside the scheduler container (`docker compose exec scheduler …`).

1. **Find the account id.** From `/admin` or
   `SELECT id FROM accounts WHERE lower(email) = lower('<email>');`
2. **Dry run.** Nothing is changed. The output is counts only:
   ```sh
   python -m src.cli delete_account --account-id <id>
   ```
   Check `account_existed: true` and that `images_targeted` looks plausible.
3. **Apply.**
   ```sh
   python -m src.cli delete_account --account-id <id> --apply
   ```
   The command deletes the rows in one transaction, then deletes the images
   after the commit. It exits 1 if any image delete failed. A failure leaves
   only unreferenced objects behind, so re-running the same command (it works
   after the row is gone) or the weekly sweep removes them.
4. **Things the command does not touch:**
   * the admin allowlist (`python -m src.cli admin remove <email>` if they
     were an admin);
   * `referrals` rows that name their email or phone as someone else's
     invitee (they belong to the referrer's ledger; delete them by hand if
     the request covers them);
   * SMS consent and history held by the z280-comms broker, and anything in
     Postmark's send logs;
   * Sentry events and container logs, which age out on their own retention.

## Accounts deleted by hand before this command existed

`delete_account --account-id <id> --apply` also works when the row is
already gone: it finds nothing in Postgres and still deletes every object
under that id's prefixes. Without the id, run the sweep below.

## The orphan sweep

`python -m src.cli sweep_orphan_images [--apply] [--force]` lists
`receipts/`, `model-reports/`, `ride-screenshots/` and `device-photos/` and
deletes every object that **no row references** and that is **more than 7
days old** (an upload is stored before its row commits). The referenced set
is the union of every column that stores a key —
`discount_reports.receipt_r2_key`, `discount_reports.plan_evidence_r2_key`,
`model_reports.photo_r2_key`, `ride_transaction_screenshots.r2_key`,
`device_photos.r2_key` (`src/image_sweep.py: REFERENCE_COLUMNS`).

* Without `--apply` it is a dry run. It logs counts and byte totals per
  prefix, never keys or account ids.
* It runs weekly with `--apply` (Sunday 03:50, `crontab`) and is recorded on
  `/admin/scheduler` like every other job.
* If more than half of a prefix's objects (and more than 20) look orphaned,
  that prefix is skipped and the run logs a warning, because that is what a
  broken reference query looks like. Read a dry run, then re-run by hand with
  `--apply --force` if the numbers are right. Expect that on the first run
  after a backlog of hand-deleted accounts.
