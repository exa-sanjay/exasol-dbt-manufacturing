# Query Reference — What to Check After Each Step

Run these queries in DbVisualizer (or any SQL client) to verify each step completed correctly.
Connect to `localhost:8563` with user `sys` / password `exasol`.

---

## After Step 2 — Seed data

### IoT sensor readings (native Exasol)

```sql
-- Expect ~260 000 rows (10 machines × 90 days × ~288 readings/day)
SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS;

-- Preview a few rows
SELECT machine_id, ts, temperature_c, vibration_mm_s, power_kw
FROM IOT_RAW.SENSOR_READINGS
ORDER BY ts DESC
LIMIT 10;
```

### Downtime events (native Exasol)

```sql
-- Expect ~240–260 rows
SELECT COUNT(*) FROM IOT_RAW.DOWNTIME_EVENTS;

SELECT machine_id, reason_code, started_at, ended_at,
       SECONDS_BETWEEN(started_at, ended_at) / 3600.0 AS downtime_hrs
FROM IOT_RAW.DOWNTIME_EVENTS
ORDER BY started_at DESC
LIMIT 10;
```

### ERP data (PostgreSQL via Virtual Schema)

```sql
-- Expect 10 rows — if this fails, the Virtual Schema isn't ready yet
SELECT COUNT(*) FROM ERP_PG.MACHINES;

SELECT machine_id, machine_name, machine_type, production_line
FROM ERP_PG.MACHINES
ORDER BY machine_id;
```

```sql
-- Expect ~2 700 rows (10 machines × 90 days × 3 shifts)
SELECT COUNT(*) FROM ERP_PG.PRODUCTION_ORDERS;
```

```sql
-- Expect ~130 rows
SELECT COUNT(*) FROM ERP_PG.DEFECTS;
```

---

## After Step 3 — Run dbt models

### Staging views (STAGING schema)

```sql
-- These are views — no row counts, just confirm they're queryable
SELECT * FROM STAGING.STG_ERP__MACHINES LIMIT 5;
SELECT * FROM STAGING.STG_IOT__SENSOR_READINGS LIMIT 5;
SELECT * FROM STAGING.STG_IOT__DOWNTIME_EVENTS LIMIT 5;
```

### OEE daily mart (MARTS schema)

```sql
-- Expect ~900 rows (10 machines × 90 days)
SELECT COUNT(*) FROM MARTS.MART_OEE_DAILY;

-- OEE summary by machine — world-class is ≥ 85%
SELECT machine_id,
       ROUND(AVG(oee) * 100, 1)          AS avg_oee_pct,
       ROUND(MIN(oee) * 100, 1)          AS min_oee_pct,
       ROUND(MAX(oee) * 100, 1)          AS max_oee_pct
FROM MARTS.MART_OEE_DAILY
GROUP BY machine_id
ORDER BY avg_oee_pct ASC;
```

### Machine health mart

```sql
-- Expect ~900 rows (10 machines × 90 days)
SELECT COUNT(*) FROM MARTS.MART_MACHINE_HEALTH;

-- See which machines have the most anomaly days
SELECT machine_id,
       SUM(CASE WHEN anomaly_flag = TRUE THEN 1 ELSE 0 END) AS anomaly_days,
       COUNT(*)                                               AS total_days
FROM MARTS.MART_MACHINE_HEALTH
GROUP BY machine_id
ORDER BY anomaly_days DESC;
```

### Production summary mart

```sql
-- Expect ~130 rows (10 machines × ~13 weeks)
SELECT COUNT(*) FROM MARTS.MART_PRODUCTION_SUMMARY;

SELECT machine_id, week_start,
       ROUND(avg_oee * 100, 1) AS avg_oee_pct,
       total_downtime_hrs
FROM MARTS.MART_PRODUCTION_SUMMARY
ORDER BY week_start DESC, avg_oee_pct ASC
LIMIT 20;
```

---

## After Step 4 — Set up AI layer

### Failure pattern embeddings

```sql
-- Expect ~241 rows (one per historical downtime event that has sensor data)
SELECT COUNT(*) FROM AI_SCHEMA.FAILURE_PATTERNS;

-- Browse stored patterns — description shows what was embedded
SELECT pattern_id, machine_type, event_date, reason_code, downtime_hrs,
       LEFT(description, 120) AS description_preview
FROM AI_SCHEMA.FAILURE_PATTERNS
ORDER BY event_date DESC
LIMIT 10;
```

```sql
-- Breakdown by failure type
SELECT reason_code,
       COUNT(*)               AS occurrences,
       ROUND(AVG(downtime_hrs), 1) AS avg_downtime_hrs
FROM AI_SCHEMA.FAILURE_PATTERNS
GROUP BY reason_code
ORDER BY occurrences DESC;
```

```sql
-- Confirm embedding is present (not NULL or empty)
SELECT pattern_id,
       LENGTH(embedding)                        AS embedding_chars,
       REGEXP_COUNT(embedding, ',') + 1         AS embedding_dims
FROM AI_SCHEMA.FAILURE_PATTERNS
LIMIT 5;
-- embedding_dims should be 768 for nomic-embed-text
```

---

## After Step 5 — Run AI agent

### Maintenance recommendations

```sql
-- Expect at least 1 row (one per at-risk machine detected today)
SELECT COUNT(*) FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS;

SELECT machine_name,
       anomaly_score,
       confidence,
       estimated_hours_to_failure,
       root_cause,
       recommended_action,
       generated_at
FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS
ORDER BY generated_at DESC;
```

### AI maintenance queue (the full picture)

```sql
-- The final mart joining OEE + sensor health + AI recommendations
SELECT machine_name,
       urgency_tier,
       estimated_hours_to_failure,
       oee_last_7d,
       oee_delta,
       anomaly_flag,
       confidence,
       root_cause,
       recommended_action,
       ai_recommendation_time
FROM MARTS.MART_AI_MAINTENANCE_QUEUE
ORDER BY
    CASE urgency_tier
        WHEN 'CRITICAL' THEN 1
        WHEN 'HIGH'     THEN 2
        WHEN 'MEDIUM'   THEN 3
        ELSE 4
    END,
    estimated_hours_to_failure ASC;
```

```sql
-- Urgency breakdown
SELECT urgency_tier, COUNT(*) AS machines
FROM MARTS.MART_AI_MAINTENANCE_QUEUE
GROUP BY urgency_tier
ORDER BY CASE urgency_tier
    WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
    WHEN 'MEDIUM'   THEN 3 ELSE 4 END;
```

---

## Quick health check (run all at once)

```sql
SELECT 'IOT_RAW.SENSOR_READINGS'              AS table_name, COUNT(*) AS rows FROM IOT_RAW.SENSOR_READINGS
UNION ALL
SELECT 'IOT_RAW.DOWNTIME_EVENTS',                              COUNT(*) FROM IOT_RAW.DOWNTIME_EVENTS
UNION ALL
SELECT 'ERP_PG.MACHINES (Virtual Schema)',                     COUNT(*) FROM ERP_PG.MACHINES
UNION ALL
SELECT 'MARTS.MART_OEE_DAILY',                                 COUNT(*) FROM MARTS.MART_OEE_DAILY
UNION ALL
SELECT 'MARTS.MART_MACHINE_HEALTH',                            COUNT(*) FROM MARTS.MART_MACHINE_HEALTH
UNION ALL
SELECT 'MARTS.MART_PRODUCTION_SUMMARY',                        COUNT(*) FROM MARTS.MART_PRODUCTION_SUMMARY
UNION ALL
SELECT 'AI_SCHEMA.FAILURE_PATTERNS',                           COUNT(*) FROM AI_SCHEMA.FAILURE_PATTERNS
UNION ALL
SELECT 'AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS',                COUNT(*) FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS
UNION ALL
SELECT 'MARTS.MART_AI_MAINTENANCE_QUEUE',                      COUNT(*) FROM MARTS.MART_AI_MAINTENANCE_QUEUE;
```

Expected counts after a full clean run:

| Table | Expected rows |
|---|---|
| `IOT_RAW.SENSOR_READINGS` | ~260 000 |
| `IOT_RAW.DOWNTIME_EVENTS` | ~241 |
| `ERP_PG.MACHINES` | 10 |
| `MARTS.MART_OEE_DAILY` | ~900 |
| `MARTS.MART_MACHINE_HEALTH` | ~900 |
| `MARTS.MART_PRODUCTION_SUMMARY` | ~130 |
| `AI_SCHEMA.FAILURE_PATTERNS` | ~241 |
| `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` | ≥ 1 |
| `MARTS.MART_AI_MAINTENANCE_QUEUE` | ≥ 1 |
