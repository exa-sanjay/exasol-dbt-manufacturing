-- Staging: 5-minute sensor readings from Exasol IOT_RAW
-- Adds a truncated hour bucket for downstream aggregations.

SELECT
    CAST(machine_id      AS INT)                            AS machine_id,
    CAST(ts              AS TIMESTAMP)                      AS ts,
    CAST(DATE_TRUNC('HOUR', ts) AS TIMESTAMP)               AS ts_hour,
    CAST(DATE_TRUNC('DAY',  ts) AS DATE)                    AS reading_date,
    CAST(temperature_c   AS DECIMAL(5,2))                   AS temperature_c,
    CAST(vibration_mm_s  AS DECIMAL(6,3))                   AS vibration_mm_s,
    CAST(power_kw        AS DECIMAL(7,3))                   AS power_kw
FROM {{ source('iot', 'sensor_readings') }}
