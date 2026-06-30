{{
    config(
        materialized='incremental',
        unique_key=['machine_id', 'shift_date'],
        incremental_strategy='delete+insert',
        on_schema_change='fail'
    )
}}

-- OEE = Availability × Performance × Quality  per machine per day.
-- This is the primary business-facing model — one row per machine per shift_date.

SELECT
    m.machine_id,
    m.machine_name,
    m.production_line,
    m.machine_type,
    m.theoretical_rate_per_hr,
    a.shift_date,
    a.planned_time_hrs,
    a.downtime_hrs,
    a.available_time_hrs,
    a.availability_rate,
    p.total_actual_qty,
    p.theoretical_output,
    p.performance_rate,
    q.total_units,
    q.good_units,
    q.total_defects,
    q.quality_rate,
    CAST(
        a.availability_rate * p.performance_rate * q.quality_rate
    AS DECIMAL(5,4))                                                 AS oee
FROM {{ ref('stg_erp__machines') }} m
JOIN {{ ref('int_availability') }} a
    ON m.machine_id = a.machine_id
JOIN {{ ref('int_performance') }} p
    ON m.machine_id = p.machine_id
    AND a.shift_date = p.shift_date
JOIN {{ ref('int_quality') }} q
    ON m.machine_id = q.machine_id
    AND a.shift_date = q.shift_date
{% if is_incremental() %}
WHERE a.shift_date >= CAST(ADD_DAYS(CURRENT_DATE, -3) AS DATE)
{% endif %}
