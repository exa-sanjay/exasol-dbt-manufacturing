-- Staging: unplanned downtime events from Exasol IOT_RAW
-- Calculates duration in hours for downstream availability calculations.

SELECT
    CAST(event_id   AS INT)                                                         AS event_id,
    CAST(machine_id AS INT)                                                         AS machine_id,
    CAST(started_at AS TIMESTAMP)                                                   AS started_at,
    CAST(ended_at   AS TIMESTAMP)                                                   AS ended_at,
    CAST(DATE_TRUNC('DAY', started_at) AS DATE)                                     AS event_date,
    CAST(SECONDS_BETWEEN(started_at, ended_at) / 3600.0 AS DECIMAL(8,4))            AS duration_hrs,
    UPPER(TRIM(reason_code))                                                        AS reason_code
FROM {{ source('iot', 'downtime_events') }}
