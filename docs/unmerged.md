# Unmerged branches — scooter-fyi-api

Remote branches with commits that are not on `main`, as of 2026-10-08.
Written during the 2026-10-08 branch cleanup: every branch whose work was already on
`main` (merged PR, or an ancestor of `main`) was deleted, and its tip SHA logged so it can be
restored. What is left here was **not** deleted because it holds work that is not on
`main`. Each one needs a decision: merge it, open a PR, or delete it.

This is a snapshot; it goes stale as branches change. Re-check with
`git fetch --prune && git log origin/main..origin/<branch>`.

| Branch | Last commit | Author | Ahead | Behind | PR |
|---|---|---|---|---|---|
| `claude/along-way-upgrades-feature-piml2p` | 2026-10-08 | ZekeNeill | 1 | 0 | #107 merged, #115 merged, #120 merged |
| `fix/settled-battery-reading` | 2026-08-10 | manager | 1 | 192 | #69 closed |
| `chore/deploy-comms-secrets` | 2026-07-29 | zneill | 2 | 284 | #33 closed |

## What each branch holds

Commits marked *equivalent change already on main* landed some other way (e.g. a squash
merge); a branch made only of those is safe to delete.

### `claude/along-way-upgrades-feature-piml2p`
- 1771607 Saved places, encrypted at rest

### `fix/settled-battery-reading`
- dbf1e8c Take the end-of-ride battery from the settled sample, not the first one

### `chore/deploy-comms-secrets`
- 347eb61 Join comms-net so http://comms:8090 survives a deploy
- 1ea60d7 Deploy: pass COMMS_TOKEN / COMMS_BASE_URL through to the container

