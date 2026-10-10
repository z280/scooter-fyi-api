-- Precomputed, pre-compressed map payloads (src/payload_cache.py; owner,
-- 2026-10-10). One row per payload variant ("devices|h3+ranks|..." ,
-- "h3|9"), overwritten when a new cycle or report stamp lands, so the table
-- holds a handful of rows and never needs pruning.
--
-- UNLOGGED: no WAL for megabytes rewritten every two minutes, and a crash
-- that truncates it costs exactly one rebuild. It exists so a restarted API
-- worker serves warm bytes on its first request; the worker serves from its
-- own memory otherwise.
--
-- body is a raw DEFLATE stream ended with Z_SYNC_FLUSH (no gzip header or
-- trailer); crc and size are the CRC-32 and length of its uncompressed
-- bytes, which payload_cache.assemble needs to finish the gzip stream.
CREATE UNLOGGED TABLE IF NOT EXISTS payload_cache (
    cache_key  TEXT PRIMARY KEY,
    cycle_id   TEXT NOT NULL,
    stamp      TEXT NOT NULL,
    body       BYTEA NOT NULL,
    crc        BIGINT NOT NULL,
    size       BIGINT NOT NULL,
    meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
    built_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
