-- Performance = actual output / (theoretical rate × available time) per machine per day.
-- Uses int_availability for available_time_hrs so the denominator matches reality.
-- Clamped to [0, 1] — a rate > 1 would indicate a data quality issue, not super-performance.

WITH orders_agg AS (
    SELECT
        machine_id,
        shift_date,
        SUM(actual_qty)  AS total_actual_qty,
        SUM(planned_qty) AS total_planned_qty
    FROM {{ ref('stg_erp__production_orders') }}
    GROUP BY machine_id, shift_date
)

SELECT
    o.machine_id,
    o.shift_date,
    o.total_actual_qty,
    o.total_planned_qty,
    m.theoretical_rate_per_hr,
    a.available_time_hrs,
    CAST(m.theoretical_rate_per_hr * a.available_time_hrs AS DECIMAL(10,2)) AS theoretical_output,
    CAST(
        LEAST(1,
            o.total_actual_qty / NULLIF(m.theoretical_rate_per_hr * a.available_time_hrs, 0)
        )
    AS DECIMAL(5,4))                                                          AS performance_rate
FROM orders_agg o
JOIN {{ ref('stg_erp__machines') }} m
    ON o.machine_id = m.machine_id
JOIN {{ ref('int_availability') }} a
    ON o.machine_id = a.machine_id
    AND o.shift_date = a.shift_date
