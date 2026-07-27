-- Per-device QR code content registry (requirement #15). One row per
-- vehicle_identifier — Veo doesn't reprint stickers, so successive scans
-- UPDATE the same row (first/last scan + a running count) rather than
-- accumulating a scan-event log. The per-account "already scanned"
-- eligibility check for the +100pt bonus does NOT live here — it queries
-- user_points directly (action='qr_scan', account_id, vehicle_identifier)
-- so there's exactly one source of truth for "has this account earned
-- this bonus for this device" (see src/points.py:credit_qr_scan_points).
--
-- The raw plate is deliberately NOT stored here — vehicle identity stays
-- keyed by vehicle_identifier only, matching src/identity.py's privacy
-- model (device_state.vehicle_plate remains the one internal-only plate
-- store). qr_raw_value is whatever string the client's scanner decoded,
-- kept verbatim for audit/debugging if Veo ever changes the QR payload
-- shape.
CREATE TABLE IF NOT EXISTS device_qr_codes (
    vehicle_identifier  TEXT PRIMARY KEY,
    qr_raw_value        TEXT NOT NULL,
    first_scanned_by    BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    first_scanned_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_scanned_by     BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    last_scanned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scan_count          INTEGER NOT NULL DEFAULT 1 CHECK (scan_count > 0)
);

CREATE INDEX IF NOT EXISTS idx_device_qr_codes_last_scanned_by
    ON device_qr_codes (last_scanned_by);
