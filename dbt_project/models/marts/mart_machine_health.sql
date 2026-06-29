-- Daily sensor health summary per machine with anomaly detection.
-- Anomaly flag triggers when peak readings exceed 15% above the day's mean
-- (temperature) or 50% above the day's mean (vibration) — simple thresholds
-- chosen to surface genuine stress events without false-positives on normal load curves.

SELECT
    machine_id,
    reading_date,
    COUNT(*)                                              AS reading_count,
    CAST(AVG(temperature_c)   AS DECIMAL(6,2))           AS avg_temp_c,
    CAST(MAX(temperature_c)   AS DECIMAL(6,2))           AS max_temp_c,
    CAST(AVG(vibration_mm_s)  AS DECIMAL(7,3))           AS avg_vibration_mm_s,
    CAST(MAX(vibration_mm_s)  AS DECIMAL(7,3))           AS max_vibration_mm_s,
    CAST(AVG(power_kw)        AS DECIMAL(8,3))           AS avg_power_kw,
    CAST(MAX(power_kw)        AS DECIMAL(8,3))           AS max_power_kw,
    CASE
        WHEN MAX(temperature_c)  > AVG(temperature_c)  * 1.15 THEN TRUE
        WHEN MAX(vibration_mm_s) > AVG(vibration_mm_s) * 1.50 THEN TRUE
        ELSE FALSE
    END                                                   AS anomaly_flag
FROM {{ ref('stg_iot__sensor_readings') }}
GROUP BY machine_id, reading_date
