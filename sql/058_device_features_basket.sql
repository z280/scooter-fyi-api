-- A fourth crowdsourced feature: the basket (sql/055 added the first three).
--
-- WHY THIS IS NOT COSMO-ONLY -----------------------------------------------
-- The question shipped in the client as "Cosmos only" for about a day, on the
-- theory that the Cosmo is the model with an optional front basket. That was
-- wrong about the fleet: the Trike carries a cargo basket as STANDARD
-- equipment, and a cargo basket is exactly the thing that gets bent. Gating
-- the question on the model would have made a damaged Trike basket
-- permanently unreportable.
--
-- So every rider is asked about all four features on every device, and a "no"
-- on an Astro is real data rather than a wasted tap: it is what lets the map
-- answer "show me the ones with a basket" without a hole where the models
-- nobody asked about should be.
--
-- WHY has_basket IS NULLABLE ON THE REPORTS TABLE ---------------------------
-- The other three presence columns are NOT NULL, because the client has
-- required all three since the day the table existed. This one cannot be:
-- the frontend already deployed sends no `has_basket` at all, and a NOT NULL
-- column (or a required Pydantic field) would 422 every report from it the
-- moment this migration lands.
--
-- NULL therefore means "this reporter's client never asked" — NOT "no
-- basket". src/device_features.py treats it as an abstention: it is excluded
-- from `answers_agree` and from the consensus vote, so a current client and
-- an old one reporting the same scooter agree about the three features they
-- both asked about instead of ping-ponging the vehicle into needs_review over
-- a question only one of them put to the rider.
--
-- The abstention path is a ROLLOUT AFFORDANCE, not a permanent rule of the
-- system. Once no client in the wild omits the field, every new row carries
-- an opinion and this column behaves exactly like the other three; the
-- NULL-handling can be dropped then, or left as the harmless dead branch it
-- will have become.

-- ---------------------------------------------------------------------------
-- 1. The submission log.
-- ---------------------------------------------------------------------------
ALTER TABLE device_feature_reports
    ADD COLUMN IF NOT EXISTS has_basket BOOLEAN;

-- ---------------------------------------------------------------------------
-- 2. The consensus view.
-- ---------------------------------------------------------------------------
-- Nullable for the same reason sql/055's three are: NULL is "nobody has told
-- us", which is what feature_status = 'needs_features_confirmed' says in the
-- payload. Every vehicle confirmed BEFORE this migration keeps its existing
-- consensus with a NULL basket, which the payload renders as `false` — the
-- same answer it gave before the question existed, and one that a single
-- reconfirmation corrects.
ALTER TABLE device_state
    ADD COLUMN IF NOT EXISTS has_basket BOOLEAN;

-- No new index, no new CHECK, and no change to feature_status: this migration
-- widens what a report says about a vehicle, not the state machine that
-- grades it. sql/055's idx_device_state_feature_status still answers "which
-- devices still need features confirmed?" unchanged.
