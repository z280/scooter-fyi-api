-- The end-ride survey's Cosmo basket answer becomes a device-feature report.
--
-- WHY -----------------------------------------------------------------------
-- The survey (sql/052) has been asking Cosmo riders "does it have a front
-- basket?" and filing the answer into ride_surveys.model_bonus, where the
-- map's basket filter (sql/058's crowdsourced consensus) never sees it. Two
-- systems asking riders the same question about the same scooter, one of
-- them into a drawer. From this migration on, src/api_ride_surveys.py also
-- writes the answer here as a report the ten-minute processor folds into the
-- consensus like any other.
--
-- A SURVEY REPORT ANSWERS ONE QUESTION --------------------------------------
-- The survey asks about the basket and nothing else, so its report abstains
-- on bell / cup_holder / phone_holder — exactly the mechanism sql/058 built
-- for has_basket during its client rollout (NULL = "this reporter was never
-- asked", excluded from agreement checks and from the consensus vote). That
-- mechanism now applies to all four features, so the three originally
-- NOT NULL answer columns become nullable.
--
-- The modal endpoint still REQUIRES all of them (its Pydantic model is
-- unchanged) — nothing about what a confirm-features client must send is
-- relaxed here. Only the survey writes partial rows.
--
-- WHAT A PARTIAL REPORT CAN AND CANNOT DO (src/device_features.py):
--   * On a vehicle with a stored basket answer: agrees (reconfirmation) or
--     disagrees (needs_review) on the basket ALONE. It can never conflict
--     with — or so much as vote on — a feature it didn't answer.
--   * On a vehicle whose consensus predates the basket question: fills the
--     basket in, exactly like a post-058 modal report would.
--   * On a vehicle nobody has ever reported: publishes the basket answer but
--     does NOT confirm the vehicle — feature_status stays
--     'needs_features_confirmed', because three of four questions were never
--     put to anyone and the map should keep asking.
--
-- WHY submitted_plate GOES NULLABLE -----------------------------------------
-- The plate question is the modal's proof-of-presence: you cannot read a
-- sticker from your sofa. A survey report has a stronger anchor — it exists
-- only for a ride the reporter started on that exact vehicle_identifier and
-- reported ending — so there is no plate to store and nothing for one to
-- prove. NULL means "no plate was asked", the same statement NULL already
-- makes in the answer columns; the modal endpoint still requires a typed
-- plate as before.

ALTER TABLE device_feature_reports
    ALTER COLUMN has_bell DROP NOT NULL,
    ALTER COLUMN has_cup_holder DROP NOT NULL,
    ALTER COLUMN has_phone_holder DROP NOT NULL,
    ALTER COLUMN submitted_plate DROP NOT NULL;

-- Where the report came from. 'modal' is the confirm-features flow (the only
-- writer until now, hence the default — every existing row is one);
-- 'ride_survey' is src/api_ride_surveys.py. Audit answers like "why did this
-- device flip to needs_review?" need the two distinguishable, because a
-- survey row legitimately has no plate and no bell answer where a modal row
-- with either missing would be a bug.
ALTER TABLE device_feature_reports
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'modal';

-- Same guarded shape as sql/055's status CHECK: a fixed vocabulary, so an
-- existing constraint of this name is left exactly as it is.
DO $$
DECLARE
    current_def text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO current_def
      FROM pg_constraint
     WHERE conname = 'device_feature_reports_source_allowed'
       AND conrelid = 'device_feature_reports'::regclass
       AND contype = 'c';

    IF current_def IS NULL THEN
        ALTER TABLE device_feature_reports
            ADD CONSTRAINT device_feature_reports_source_allowed
            CHECK (source IN ('modal', 'ride_survey'));
    END IF;
END $$;
