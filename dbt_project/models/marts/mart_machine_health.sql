{{
    config(
        materialized='incremental',
        unique_key=['machine_id', 'reading_date'],
        incremental_strategy='delete+insert',
        on_schema_change='fail'
    )
}}

-- Daily sensor health summary per machine with anomaly detection.
-- Thresholds are configurable via dbt vars (defaults: temp ×1.15, vibration ×1.50).
-- Override at run time: dbt run --vars '{"temp_anomaly_ratio": 1.20, "vib_anomaly_ratio": 1.60}'

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
        WHEN MAX(temperature_c)  > AVG(temperature_c)  * {{ var('temp_anomaly_ratio') }} THEN TRUE
        WHEN MAX(vibration_mm_s) > AVG(vibration_mm_s) * {{ var('vib_anomaly_ratio')  }} THEN TRUE
        ELSE FALSE
    END                                                   AS anomaly_flag
FROM {{ ref('stg_iot__sensor_readings') }}
{% if is_incremental() %}
WHERE reading_date >= CAST(ADD_DAYS(CURRENT_DATE, -3) AS DATE)
{% endif %}
GROUP BY machine_id, reading_date
