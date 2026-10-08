# docs

Where this repo's documentation lives. The root [README.md](../README.md)
covers backend internals.

## Active plans — `docs/`

Plans being written or built.

- [ALONG_THE_WAY_PLAN.md](ALONG_THE_WAY_PLAN.md): the Along the Way program (master plan and API lane). Phase 1 shipped, Phase 4 retired, the rest still open.
- [API_REQUIREMENTS.md](API_REQUIREMENTS.md): the backend requirements behind the frontend phases, with the per-PR status table. Mostly implemented; the equity boundary migration (§1.1a) and §7.2 are still open.
- [ANALYTICS_TIER2_BACKLOG.md](ANALYTICS_TIER2_BACKLOG.md): the analytics rollup backlog. The hourly rollup and the equity cut shipped; repair time and the rider-story figure are still open.
- [ATLANTA_PLAN.md](ATLANTA_PLAN.md): an assessment of running Atlanta (Bird and Lime) as city #2. Nothing built yet.
- [FLEET_REPORTS_PLAN.md](FLEET_REPORTS_PLAN.md): fleet reports that persist until the vehicle moves, and the stewardship they need. Specified, not started.
- [MULTI_TENANCY_PLAN.md](MULTI_TENANCY_PLAN.md): the multi-provider, multi-city proposal. Nothing built yet.
- [PLAN_EQUITY_RECEIPTS.md](PLAN_EQUITY_RECEIPTS.md): equity receipt review. Phases 1–2 shipped; automated analysis, the human portal and reporting are still open.

## Implemented — `docs/implemented/`

Plans that are finished and shipped. Kept for the record.

- [FEATURE_PLAN_2026-07.md](implemented/FEATURE_PLAN_2026-07.md): profiles, SMS sign-in, ride report fields and area leaders (sql/042–048).
- [PLAN_FLEET_ANALYTICS.md](implemented/PLAN_FLEET_ANALYTICS.md): fleet analytics rollups (sql/094) and the seven `/api/v1/analytics/*` endpoints.
- [PLAN_RIDE_MODE_API.md](implemented/PLAN_RIDE_MODE_API.md): ride mode overhaul, API phases A1–A4 (sql/047–053).
- [RIDE_MODE_OVERHAUL_PLAN.md](implemented/RIDE_MODE_OVERHAUL_PLAN.md): ride mode overhaul master program plan, shared with the frontend repo.

## Deferred — `docs/deferred/`

Plans that were deferred, parked, abandoned or superseded.

- [VEO_AUDIT.md](deferred/VEO_AUDIT.md): the original standalone GBFS compliance poller (CSV and shapely). Superseded by this service.

## Reference — `docs/reference/`

Living documents that describe the system as it is now.

- [API.md](reference/API.md): the public API reference, and the contract for frontend consumers.
- [MIGRATION.md](reference/MIGRATION.md): the VPS migration runbook, plus the post-rename checklist and the names that deliberately stay `veo-audit`.
- [build_photon_index.md](reference/build_photon_index.md): runbook for building the Photon geocoder index (seed once, then refresh quarterly).

- [`unmerged.md`](unmerged.md): remote branches holding work not on `main` (snapshot, 2026-10-08).
