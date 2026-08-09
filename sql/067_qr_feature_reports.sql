-- QR-validated feature confirmations.
--
-- The Confirm Features modal's anti-abuse story has been the typed plate:
-- you cannot read the plate under a scooter's QR code from your sofa. A QR
-- scan is the same proof-of-presence with the typo risk removed — the
-- sticker encodes the plate (see src/qr.py's extract_plate), so a scan both
-- validates the report and, when the rider tapped one scooter but scanned
-- its neighbour, identifies which vehicle the answers actually belong to.
-- From this migration on, POST /api/v1/reports/device-features accepts an
-- optional qr_raw_value and the endpoint attaches the report to the vehicle
-- the QR resolves to, not the one the client claimed.
--
-- Two audit columns, both nullable (every pre-QR report abstains):
--
--   qr_raw_value — the decoded QR payload, VERBATIM, same rationale as
--     device_qr_codes.qr_raw_value (sql/032): kept for audit/debugging if
--     Veo ever changes the QR payload shape. NULL = no scan accompanied
--     this report (the typed-plate flow, and every row before this
--     migration).
--
--   claimed_vehicle_identifier — the vehicle the CLIENT said the report was
--     about, recorded only when the QR resolved to a different one and the
--     report was re-attached. A rash of these on adjacent vehicles is the
--     same rider-mixed-up-two-scooters signal near-miss plates already
--     give us, now with the correction applied instead of merely detected.
--     NULL = the claim and the scan agreed (or there was no scan, or no
--     claim — the QR-only tools-drawer flow sends no vehicle at all).
--
-- vehicle_identifier on the row remains, as everywhere, the vehicle the
-- report is ABOUT — after any QR re-targeting — so the ten-minute
-- processor (src/device_features.py) needs no change: it keeps reading
-- rows keyed by the vehicle they truly describe.

ALTER TABLE device_feature_reports
    ADD COLUMN IF NOT EXISTS qr_raw_value TEXT,
    ADD COLUMN IF NOT EXISTS claimed_vehicle_identifier TEXT;
