-- "Recommend this device" yes/no (requirement #11). One recommendation
-- per (account, vehicle) — resubmitting UPDATES the answer rather than
-- accumulating a log, since this is a standing opinion, not a per-ride
-- event. The eligibility gate ("only accepted when the account has a
-- completed ride against this vehicle_identifier in the last 24h") is
-- enforced in the app against tracked_rides (sql/027) — NOT stored
-- redundantly here. No points are awarded for this action (it's absent
-- from the points list).
CREATE TABLE IF NOT EXISTS device_recommendations (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vehicle_identifier  TEXT NOT NULL,
    recommend           BOOLEAN NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_device_recommendations_account_vehicle
    ON device_recommendations (account_id, vehicle_identifier);
CREATE INDEX IF NOT EXISTS idx_device_recommendations_vehicle
    ON device_recommendations (vehicle_identifier);
