-- AI-augmented maintenance queue.
-- Surfaces machines with declining OEE or active sensor anomalies alongside
-- Ollama-generated root cause + maintenance recommendations from AI_SCHEMA.
--
-- AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS is written by the factory_ai_agent.py
-- script, not by dbt — this is the cross-boundary join that makes the AI output
-- first-class in the dbt lineage graph.

WITH oee_max_date AS (
    SELECT machine_id, MAX(shift_date) AS max_date
    FROM {{ ref('mart_oee_daily') }}
    GROUP BY machine_id
),

oee_trend AS (
    SELECT
        o.machine_id,
        AVG(CASE WHEN o.shift_date >= ADD_DAYS(d.max_date, -7)
                 THEN o.oee END)  AS oee_last_7d,
        AVG(CASE WHEN o.shift_date >= ADD_DAYS(d.max_date, -14)
                  AND o.shift_date <  ADD_DAYS(d.max_date, -7)
                 THEN o.oee END)  AS oee_prior_7d
    FROM {{ ref('mart_oee_daily') }} o
    JOIN oee_max_date d ON o.machine_id = d.machine_id
    GROUP BY o.machine_id
),

health_max_date AS (
    SELECT MAX(reading_date) AS max_date FROM {{ ref('mart_machine_health') }}
),

latest_health AS (
    SELECT
        h.machine_id,
        MAX(h.reading_date)        AS reading_date,
        MAX(h.anomaly_flag)        AS anomaly_flag,
        AVG(h.avg_temp_c)          AS avg_temp_c,
        MAX(h.max_temp_c)          AS max_temp_c,
        AVG(h.avg_vibration_mm_s)  AS avg_vibration_mm_s,
        MAX(h.max_vibration_mm_s)  AS max_vibration_mm_s
    FROM {{ ref('mart_machine_health') }} h
    CROSS JOIN health_max_date d
    WHERE h.reading_date >= ADD_DAYS(d.max_date, -7)
    GROUP BY h.machine_id
),

latest_rec AS (
    SELECT
        rec_id,
        machine_id,
        generated_at,
        anomaly_score,
        root_cause,
        recommended_action,
        estimated_hours_to_failure,
        confidence,
        similar_pattern_ids
    FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS
    QUALIFY ROW_NUMBER() OVER (PARTITION BY machine_id ORDER BY generated_at DESC) = 1
)

SELECT
    m.machine_id,
    m.machine_name,
    m.production_line,
    m.machine_type,
    -- Sensor health (latest day)
    h.reading_date,
    h.anomaly_flag,
    CAST(h.avg_temp_c          AS DECIMAL(6,2))  AS avg_temp_c,
    CAST(h.max_temp_c          AS DECIMAL(6,2))  AS max_temp_c,
    CAST(h.avg_vibration_mm_s  AS DECIMAL(7,3))  AS avg_vibration_mm_s,
    CAST(h.max_vibration_mm_s  AS DECIMAL(7,3))  AS max_vibration_mm_s,
    -- OEE trend
    CAST(t.oee_last_7d   AS DECIMAL(5,4))        AS oee_last_7d,
    CAST(t.oee_prior_7d  AS DECIMAL(5,4))        AS oee_prior_7d,
    CAST(t.oee_last_7d - t.oee_prior_7d AS DECIMAL(6,4)) AS oee_trend_delta,
    -- AI recommendation (from Ollama via factory_ai_agent.py)
    r.anomaly_score,
    r.root_cause,
    r.recommended_action,
    r.estimated_hours_to_failure,
    r.confidence,
    r.similar_pattern_ids,
    r.generated_at                               AS ai_generated_at,
    -- Urgency tier derived from hours-to-failure estimate
    CASE
        WHEN r.estimated_hours_to_failure <= 8   THEN 'CRITICAL'
        WHEN r.estimated_hours_to_failure <= 24  THEN 'HIGH'
        WHEN r.estimated_hours_to_failure <= 72  THEN 'MEDIUM'
        ELSE                                          'LOW'
    END                                          AS urgency_tier
FROM {{ ref('stg_erp__machines') }} m
JOIN latest_health h ON m.machine_id = h.machine_id
JOIN oee_trend t     ON m.machine_id = t.machine_id
LEFT JOIN latest_rec r ON m.machine_id = r.machine_id
WHERE h.anomaly_flag = TRUE
   OR (t.oee_last_7d IS NOT NULL AND t.oee_prior_7d IS NOT NULL
       AND t.oee_last_7d < t.oee_prior_7d - 0.03)
ORDER BY
    CASE urgency_tier WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
    r.estimated_hours_to_failure ASC NULLS LAST
