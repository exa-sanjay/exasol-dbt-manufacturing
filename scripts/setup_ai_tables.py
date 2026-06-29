"""Create AI_SCHEMA tables in Exasol and seed the FAILURE_PATTERNS vector store.

This script:
  1. Creates AI_SCHEMA.FAILURE_PATTERNS  — Exasol acting as a 6-dimensional vector store.
     Each row is a historical downtime event represented as a feature vector derived from
     sensor readings and OEE data on the day of the failure.
  2. Creates AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS — the AI agent writes here; the dbt
     mart mart_ai_maintenance_queue reads it to close the loop.
  3. Seeds FAILURE_PATTERNS by joining downtime_events + mart_machine_health + mart_oee_daily.

Machine type encoding (v_machine_type dimension):
  CNC_MILL=1  LATHE=2  WELDING_ROBOT=3  ASSEMBLY_BOT=4  INJECTION_MOLD=5  QUALITY_SCANNER=6
"""

import math
import os
import sys

import pyexasol

# ── Connection ────────────────────────────────────────────────────────────────
EXA_DSN      = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER     = os.environ.get("EXASOL_USER", "sys")
EXA_PASSWORD = os.environ.get("EXASOL_PASSWORD", "exasol")

MACHINE_TYPE_CODES = {
    "CNC_MILL":       1,
    "LATHE":          2,
    "WELDING_ROBOT":  3,
    "ASSEMBLY_BOT":   4,
    "INJECTION_MOLD": 5,
    "QUALITY_SCANNER":6,
}


def log(msg):
    print(f"  {msg}", flush=True)


def create_schema_and_tables(con):
    log("Creating AI_SCHEMA ...")
    con.execute("CREATE SCHEMA IF NOT EXISTS AI_SCHEMA")

    log("Creating AI_SCHEMA.FAILURE_PATTERNS ...")
    con.execute("""
        CREATE OR REPLACE TABLE AI_SCHEMA.FAILURE_PATTERNS (
            pattern_id      INT            PRIMARY KEY,
            machine_id      INT            NOT NULL,
            machine_type    VARCHAR(50)    NOT NULL,
            event_date      DATE           NOT NULL,
            reason_code     VARCHAR(50)    NOT NULL,
            -- 6D feature vector
            v_machine_type  DECIMAL(5,2)   NOT NULL,
            v_temp_zscore   DECIMAL(8,4)   NOT NULL,
            v_vib_zscore    DECIMAL(8,4)   NOT NULL,
            v_pwr_zscore    DECIMAL(8,4)   NOT NULL,
            v_oee_drop      DECIMAL(8,4)   NOT NULL,
            v_downtime_hrs  DECIMAL(8,3)   NOT NULL,
            vector_norm     DECIMAL(12,6)  NOT NULL
        )
    """)

    log("Creating AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS ...")
    con.execute("""
        CREATE OR REPLACE TABLE AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS (
            rec_id                    INT IDENTITY PRIMARY KEY,
            machine_id                INT          NOT NULL,
            machine_name              VARCHAR(100) NOT NULL,
            generated_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
            anomaly_score             DECIMAL(6,3),
            root_cause                VARCHAR(1000),
            recommended_action        VARCHAR(1000),
            estimated_hours_to_failure DECIMAL(6,1),
            confidence                VARCHAR(20),
            similar_pattern_ids       VARCHAR(200)
        )
    """)
    log("Tables created.")


def fetch_historical_failures(con):
    """Retrieve all downtime events joined with sensor stats and OEE on the event day."""
    rows = con.execute("""
        SELECT
            d.machine_id,
            m.machine_type,
            CAST(DATE_TRUNC('DAY', d.started_at) AS DATE)          AS event_date,
            d.reason_code,
            -- sensor stats on the event day
            h.avg_temp_c,
            h.max_temp_c,
            h.avg_vibration_mm_s,
            h.max_vibration_mm_s,
            h.avg_power_kw,
            h.max_power_kw,
            -- OEE on the event day
            COALESCE(o.oee, 0.70)                                   AS oee,
            -- downtime duration
            CAST(SECONDS_BETWEEN(d.started_at, d.ended_at) / 3600.0 AS DECIMAL(8,3)) AS downtime_hrs
        FROM IOT_RAW.DOWNTIME_EVENTS d
        JOIN ERP_PG.MACHINES m
            ON d.machine_id = m.machine_id
        LEFT JOIN MARTS.MART_MACHINE_HEALTH h
            ON d.machine_id = h.machine_id
            AND CAST(DATE_TRUNC('DAY', d.started_at) AS DATE) = h.reading_date
        LEFT JOIN MARTS.MART_OEE_DAILY o
            ON d.machine_id = o.machine_id
            AND CAST(DATE_TRUNC('DAY', d.started_at) AS DATE) = o.shift_date
        WHERE h.avg_temp_c IS NOT NULL
    """).fetchall()
    return [{k.lower(): v for k, v in r.items()} for r in rows]


def compute_machine_baselines(con):
    """Compute per-machine mean and std of sensor readings for Z-score normalization."""
    rows = con.execute("""
        SELECT
            machine_id,
            AVG(temperature_c)                                        AS mean_temp,
            STDDEV_POP(temperature_c)                                 AS std_temp,
            AVG(vibration_mm_s)                                       AS mean_vib,
            STDDEV_POP(vibration_mm_s)                                AS std_vib,
            AVG(power_kw)                                             AS mean_pwr,
            STDDEV_POP(power_kw)                                      AS std_pwr
        FROM IOT_RAW.SENSOR_READINGS
        GROUP BY machine_id
    """).fetchall()
    rows = [{k.lower(): v for k, v in r.items()} for r in rows]
    return {r["machine_id"]: r for r in rows}


def safe_zscore(value, mean, std, default=0.0):
    if std is None or float(std) == 0:
        return default
    return (float(value) - float(mean)) / float(std)


def build_feature_vector(row, baselines):
    mid      = row["machine_id"]
    mtype    = (row["machine_type"] or "").upper().strip()
    bl       = baselines.get(mid, {})

    v1 = float(MACHINE_TYPE_CODES.get(mtype, 0))
    v2 = safe_zscore(row["max_temp_c"],        bl.get("mean_temp"), bl.get("std_temp"))
    v3 = safe_zscore(row["max_vibration_mm_s"],bl.get("mean_vib"),  bl.get("std_vib"))
    v4 = safe_zscore(row["max_power_kw"],      bl.get("mean_pwr"),  bl.get("std_pwr"))
    v5 = max(0.0, 1.0 - float(row["oee"] or 0))
    v6 = min(float(row["downtime_hrs"] or 0), 24.0)   # cap at 24h to keep scale

    norm = math.sqrt(v1**2 + v2**2 + v3**2 + v4**2 + v5**2 + v6**2)
    return (v1, v2, v3, v4, v5, v6, norm if norm > 0 else 1.0)


def seed_failure_patterns(con, rows, baselines):
    log(f"Building feature vectors for {len(rows)} historical failure events ...")
    batch = []
    for pid, row in enumerate(rows, start=1):
        v1, v2, v3, v4, v5, v6, norm = build_feature_vector(row, baselines)
        batch.append((
            pid,
            row["machine_id"],
            (row["machine_type"] or "UNKNOWN").upper().strip(),
            row["event_date"],
            (row["reason_code"] or "UNKNOWN").upper().strip(),
            v1, v2, v3, v4, v5, v6,
            norm,
        ))

    if not batch:
        log("No failure events found — skipping seed. Run 'make seed' first.")
        return 0

    con.import_from_iterable(batch, ("AI_SCHEMA", "FAILURE_PATTERNS"))
    log(f"Inserted {len(batch)} failure patterns into AI_SCHEMA.FAILURE_PATTERNS.")
    return len(batch)


def main():
    print("\n==> Setting up AI_SCHEMA tables ...")
    try:
        con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASSWORD,
                               websocket_sslopt={"cert_reqs": 0}, fetch_dict=True)
    except Exception as e:
        print(f"ERROR: Cannot connect to Exasol at {EXA_DSN}: {e}")
        print("       Make sure 'make up' and 'make seed' and 'make dbt-run' have all completed.")
        sys.exit(1)

    create_schema_and_tables(con)

    print("\n==> Seeding FAILURE_PATTERNS (Exasol as vector store) ...")
    try:
        rows      = fetch_historical_failures(con)
        baselines = compute_machine_baselines(con)
        count     = seed_failure_patterns(con, rows, baselines)
        print(f"    Vector store ready — {count} failure pattern vectors stored in Exasol.")
    except Exception as e:
        print(f"  WARNING: Could not seed failure patterns: {e}")
        print("           This is normal if mart tables haven't been built yet.")
        print("           Run 'make dbt-run' first, then re-run 'make ai-setup'.")

    con.close()
    print("\n==> AI table setup complete.\n")


if __name__ == "__main__":
    main()
