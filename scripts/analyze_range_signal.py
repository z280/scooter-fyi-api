#!/usr/bin/env python3
"""Characterize current_range_meters as a battery/SoC signal, from the R2 archive.

Context (live-feed finding, 2026-07-06): a single GBFS snapshot shows exactly
100 distinct current_range_meters values across ~8.7k devices, with the SAME
value set for every vehicle_type_id (identical min/max/gaps despite different
rated max_range_meters). That implies the field is an integer state-of-charge
percentage pushed through one fleet-wide, vendor-side lookup table — not a
per-vehicle range prediction. This script tests that hypothesis (and its
consequences) against the month-scale Parquet archive:

  1. TABLE STABILITY — is the distinct-value set the same every day? If it
     drifts, SoC must be recovered per-day by rank, not by a fixed lookup.
  2. SOC RECOVERY — map each value to its rank in the global table; report
     the recovered percent grid.
  3. PER-MODEL CAPS — max value per vehicle model over the archive. If every
     model shares one cap, the field carries no per-model battery information
     (which also weakens sql/011's max-observed-range classification premise).
  4. DELTA SIGNAL — for consecutive same-device snapshot pairs (5-20 min
     apart): regression of SoC drop vs straight-line distance for moved
     pairs, per model. Slope = %SoC per km; r² = how usable the signal is.
  5. OPS/NOISE FLOOR — battery-swap frequency (large positive jumps), idle
     drain (stationary pairs), and post-ride rebound (stationary pair
     immediately after a moved pair).

Run on the VPS (R2_* env vars present, e.g. inside the worker container):

    docker compose exec pipeline_worker python scripts/analyze_range_signal.py

Or against local Parquet files (testing / a downloaded subset):

    python scripts/analyze_range_signal.py --local 'path/to/*.parquet'

Read-only: never writes to R2 or Postgres.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import duckdb

# Consecutive-pair windows: the ingest cadence is 10 min; accept 5-20 min so
# a single missed cycle doesn't manufacture 20-minute "trips" silently.
PAIR_GAP_MIN_S = 5 * 60
PAIR_GAP_MAX_S = 20 * 60
MOVED_METERS = 50.0  # same intent as device_state.py's MOVED threshold
# A positive jump this large (in recovered SoC %) is a battery swap, not
# charging-in-place; large negative jumps are swap-outs/resets. Both are
# excluded from the consumption regression.
SWAP_JUMP_PCT = 20.0


def _r2_source() -> tuple[str, dict[str, str]]:
    """Build the s3:// glob + DuckDB secret settings from the environment."""
    missing = [
        k
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
        if not os.environ.get(k)
    ]
    if missing:
        sys.exit(
            f"missing env: {', '.join(missing)} — run where the archive creds "
            "live (the VPS / worker container), or use --local '<glob>'"
        )
    cfg_path = os.environ.get("VEO_CONFIG", str(Path(__file__).resolve().parent.parent / "config.json"))
    endpoint_template = json.load(open(cfg_path))["r2"]["endpoint_template"]
    endpoint = endpoint_template.format(account_id=os.environ["R2_ACCOUNT_ID"]).removeprefix("https://")
    glob = f"s3://{os.environ['R2_BUCKET_NAME']}/raw/*/*/*/*.parquet"
    return glob, {
        "s3_endpoint": endpoint,
        "s3_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "s3_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "s3_region": "auto",
        "s3_url_style": "path",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", metavar="GLOB", help="read local parquet files instead of R2")
    ap.add_argument("--workdir", default="/tmp/range_analysis",
                    help="disk-backed db + spill dir; needs a few GB free (default %(default)s)")
    ap.add_argument("--memory-limit", default="600MB",
                    help="DuckDB memory cap — keep well under the container's "
                         "cgroup limit (default %(default)s, sized for the 1 GiB worker)")
    ap.add_argument("--threads", type=int, default=2,
                    help="DuckDB threads; fewer threads = less peak memory (default %(default)s)")
    ap.add_argument("--buckets", type=int, default=16,
                    help="device-hash passes for the pair window; more buckets "
                         "= less peak memory, slightly slower (default %(default)s)")
    args = ap.parse_args()

    # File-backed database + explicit memory cap. DuckDB sizes its memory
    # budget from HOST RAM, not the container's cgroup limit, so an
    # in-memory run inside the 1 GiB worker gets OOM-killed at the section-4
    # window sort over the month-scale archive. A disk-backed db + capped
    # memory_limit lets the big sort/materialization spill to disk instead.
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    dbfile = workdir / "range_analysis.duckdb"
    dbfile.unlink(missing_ok=True)
    con = duckdb.connect(str(dbfile))
    con.execute(f"SET memory_limit='{args.memory_limit}';")
    con.execute(f"SET threads={args.threads};")
    con.execute(f"SET temp_directory='{workdir / 'spill'}';")
    con.execute("SET preserve_insertion_order=false;")

    if args.local:
        glob = args.local
    else:
        glob, s3 = _r2_source()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        for k, v in s3.items():
            con.execute(f"SET {k}='{v}';")

    # Stage once: a single R2 scan into a compact local table that every
    # section queries. FLOAT lat/lon keep ~1 m precision — plenty against
    # the 50 m moved threshold — at half the width.
    # union_by_name: columns were added over time (003: range/identifier,
    # 016: model name) — older files simply yield NULLs for newer columns.
    print("staging archive locally (single R2 scan) ...", flush=True)
    con.execute(
        f"""
        CREATE TABLE pts AS
        SELECT vehicle_identifier,
               snapshot_time,
               CAST(latitude AS FLOAT)  AS lat,
               CAST(longitude AS FLOAT) AS lon,
               CAST(current_range_meters AS INTEGER) AS r,
               COALESCE(vehicle_model_name, form_factor) AS model
        FROM read_parquet('{glob}', union_by_name=true)
        WHERE current_range_meters IS NOT NULL
          AND vehicle_identifier IS NOT NULL
        """
    )

    # ::VARCHAR — fetching TIMESTAMPTZ values into Python requires pytz,
    # which isn't in the image; all we do with these is print them.
    n_rows, t_min, t_max = con.execute(
        "SELECT COUNT(*), MIN(snapshot_time)::VARCHAR, MAX(snapshot_time)::VARCHAR FROM pts"
    ).fetchone()
    print(f"archive: {n_rows:,} points, {t_min} → {t_max}")
    if not n_rows:
        sys.exit("no rows with current_range_meters — nothing to analyze")

    # ---- 1. Lookup-table stability --------------------------------------
    print("\n== 1. value-table stability ==")
    n_distinct = con.execute("SELECT COUNT(DISTINCT r) FROM pts").fetchone()[0]
    print(f"global distinct current_range_meters values: {n_distinct}")
    daily = con.execute(
        """
        SELECT snapshot_time::DATE AS d, COUNT(DISTINCT r) AS nv,
               MIN(r) AS lo, MAX(r) AS hi
        FROM pts GROUP BY d ORDER BY d
        """
    ).fetchall()
    stable = len({(row[2], row[3]) for row in daily}) == 1
    print(f"days: {len(daily)}; per-day distinct min..max: "
          f"{min(r[1] for r in daily)}..{max(r[1] for r in daily)}; "
          f"same (min,max) every day: {stable}")
    if n_distinct > 150:
        print("  !! far more than ~101 values — the fixed-lookup hypothesis is "
              "wrong or the table drifts; rank-based SoC below is per-archive, "
              "treat cross-day deltas with suspicion")

    # ---- 2. SoC recovery by rank ----------------------------------------
    # rank 0..N-1 over the global value table → percent grid. If the table
    # really is integer-percent, N≈101 and pct is exact.
    con.execute(
        """
        CREATE TABLE soc_lut AS
        SELECT r,
               100.0 * (ROW_NUMBER() OVER (ORDER BY r) - 1)
                     / NULLIF(COUNT(*) OVER () - 1, 0) AS pct
        FROM (SELECT DISTINCT r FROM pts)
        """
    )
    print("\n== 2. SoC recovery ==")
    print(f"rank grid: {n_distinct} steps → 1 step = "
          f"{100.0 / max(n_distinct - 1, 1):.2f}% SoC")
    top, step_m = con.execute(
        "SELECT MAX(r), MAX(r) / NULLIF(COUNT(*) - 1, 0) FROM soc_lut"
    ).fetchone()
    print(f"top value {top} m; mean meters per step ≈ {step_m:.0f}")

    # ---- 3. Per-model caps ----------------------------------------------
    print("\n== 3. per-model max (does any model exceed the shared cap?) ==")
    for model, mx, n in con.execute(
        "SELECT model, MAX(r), COUNT(*) FROM pts GROUP BY model ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {model or '<null>':<12} max={mx:>7,} m  ({n:,} points)")

    # ---- consecutive pairs ----------------------------------------------
    # Bucketed by device hash: the window sort must hold whole device
    # partitions in memory, and one pass over all 44M points exceeds any
    # sane cap inside the 1 GiB worker (observed: OutOfMemoryException at
    # the 600MB limit). Devices never span buckets, so N passes over 1/N of
    # the fleet each are exact, and peak memory scales down by N.
    # prev_dist (the previous pair's distance, for section 5's rebound
    # check) is computed here in the same per-bucket window rather than in
    # a second full-table window later.
    con.execute(
        "CREATE TABLE pairs (model VARCHAR, dist_m FLOAT, dsoc FLOAT, prev_dist FLOAT)"
    )
    print(f"\nbuilding consecutive-pair table in {args.buckets} device buckets ...",
          flush=True)
    for b in range(args.buckets):
        con.execute(
            f"""
            INSERT INTO pairs
            WITH lagged AS (
                SELECT vehicle_identifier, model, snapshot_time, lat, lon, r,
                       LAG(snapshot_time) OVER w AS t0,
                       LAG(lat) OVER w AS lat0,
                       LAG(lon) OVER w AS lon0,
                       LAG(r)   OVER w AS r0
                FROM pts
                WHERE HASH(vehicle_identifier) % {args.buckets} = {b}
                WINDOW w AS (PARTITION BY vehicle_identifier ORDER BY snapshot_time)
            ),
            pair_rows AS (
                SELECT lagged.vehicle_identifier,
                       lagged.snapshot_time,
                       lagged.model,
                       CAST(111320.0 * SQRT(POW(lat - lat0, 2)
                            + POW((lon - lon0) * COS(RADIANS(lat)), 2)) AS FLOAT) AS dist_m,
                       CAST(lut1.pct - lut0.pct AS FLOAT) AS dsoc
                FROM lagged
                JOIN soc_lut lut1 ON lut1.r = lagged.r
                JOIN soc_lut lut0 ON lut0.r = lagged.r0
                WHERE t0 IS NOT NULL
                  AND EPOCH(snapshot_time - t0) BETWEEN {PAIR_GAP_MIN_S} AND {PAIR_GAP_MAX_S}
            )
            SELECT model, dist_m, dsoc,
                   LAG(dist_m) OVER (PARTITION BY vehicle_identifier
                                     ORDER BY snapshot_time) AS prev_dist
            FROM pair_rows
            """
        )
        print(f"  bucket {b + 1}/{args.buckets}", flush=True)
    n_pairs = con.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
    print(f"\nconsecutive same-device pairs ({PAIR_GAP_MIN_S // 60}-"
          f"{PAIR_GAP_MAX_S // 60} min apart): {n_pairs:,}")

    # ---- 4. Delta signal: SoC burn vs distance ---------------------------
    print("\n== 4. SoC burn vs straight-line distance (moved pairs, swaps excluded) ==")
    rows = con.execute(
        f"""
        SELECT model,
               COUNT(*)                                   AS n,
               AVG(-dsoc)                                 AS mean_burn_pct,
               AVG(dist_m) / 1000.0                       AS mean_km,
               REGR_SLOPE(-dsoc, dist_m / 1000.0)         AS pct_per_km,
               REGR_R2(-dsoc, dist_m / 1000.0)            AS r2,
               SUM(CASE WHEN dsoc = 0 THEN 1 ELSE 0 END)  AS zero_delta
        FROM pairs
        WHERE dist_m > {MOVED_METERS}
          AND ABS(dsoc) < {SWAP_JUMP_PCT}
        GROUP BY model ORDER BY n DESC
        """
    ).fetchall()
    print(f"  {'model':<12} {'pairs':>8} {'burn%/pair':>10} {'km/pair':>8} "
          f"{'%SoC/km':>8} {'r²':>6} {'Δ=0 share':>9}")
    for model, n, burn, km, slope, r2, zeros in rows:
        print(f"  {model or '<null>':<12} {n:>8,} {burn:>10.2f} {km:>8.2f} "
              f"{(slope if slope is not None else float('nan')):>8.3f} "
              f"{(r2 if r2 is not None else float('nan')):>6.3f} "
              f"{zeros / n if n else 0:>9.1%}")
    print("  (Δ=0 share = moved pairs where quantization swallowed the burn "
          "entirely — the short-trip noise floor)")

    # ---- 5. Ops & noise floor --------------------------------------------
    print("\n== 5. swaps, idle drain, rebound ==")
    swaps = con.execute(
        f"SELECT COUNT(*) FROM pairs WHERE dsoc >= {SWAP_JUMP_PCT}"
    ).fetchone()[0]
    days = len(daily)
    print(f"battery swaps (jump ≥ +{SWAP_JUMP_PCT:.0f}% SoC): {swaps:,} "
          f"(~{swaps / max(days, 1):.0f}/day)")
    idle = con.execute(
        f"""
        SELECT COUNT(*), AVG(dsoc), STDDEV(dsoc),
               SUM(CASE WHEN dsoc = 0 THEN 1 ELSE 0 END)
        FROM pairs WHERE dist_m <= {MOVED_METERS} AND ABS(dsoc) < {SWAP_JUMP_PCT}
        """
    ).fetchone()
    print(f"idle pairs: {idle[0]:,}; mean ΔSoC {idle[1]:+.4f}%, "
          f"σ {idle[2]:.3f}%, exactly-zero {idle[3] / max(idle[0], 1):.1%}")
    rebound = con.execute(
        f"""
        SELECT COUNT(*), AVG(dsoc),
               SUM(CASE WHEN dsoc > 0 THEN 1 ELSE 0 END)
        FROM pairs
        WHERE dist_m <= {MOVED_METERS}          -- now stationary
          AND prev_dist > {MOVED_METERS}        -- but just finished a trip
          AND ABS(dsoc) < {SWAP_JUMP_PCT}
        """
    ).fetchone()
    print(f"post-ride stationary pairs: {rebound[0]:,}; mean ΔSoC "
          f"{rebound[1]:+.4f}%, positive (rebound) share "
          f"{rebound[2] / max(rebound[0], 1):.1%}")

    con.close()
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
