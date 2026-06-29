-- Availability = (planned time - downtime) / planned time  per machine per day.
-- Downtime hours come from IoT-detected unplanned stoppages (downtime_events).
-- The rate is clamped to [0, 1] in case downtime exceeds recorded shift time.

WITH shifts AS (
    SELECT
        machine_id,
        shift_date,
        SUM(shift_duration_hrs) AS planned_time_hrs
    FROM {{ ref('stg_erp__production_orders') }}
    GROUP BY machine_id, shift_date
),

downtime AS (
    SELECT
        machine_id,
        event_date,
        SUM(duration_hrs) AS downtime_hrs
    FROM {{ ref('stg_iot__downtime_events') }}
    GROUP BY machine_id, event_date
)

SELECT
    s.machine_id,
    s.shift_date,
    s.planned_time_hrs,
    COALESCE(d.downtime_hrs, 0)                                              AS downtime_hrs,
    GREATEST(0, s.planned_time_hrs - COALESCE(d.downtime_hrs, 0))           AS available_time_hrs,
    CAST(
        GREATEST(0,
            (s.planned_time_hrs - COALESCE(d.downtime_hrs, 0)) / NULLIF(s.planned_time_hrs, 0)
        )
    AS DECIMAL(5,4))                                                         AS availability_rate
FROM shifts s
LEFT JOIN downtime d
    ON s.machine_id = d.machine_id
    AND s.shift_date = d.event_date
