-- Weekly production summary rolled up per machine and production line.
-- Aggregates mart_oee_daily so consumers get a single weekly KPI view.

SELECT
    machine_id,
    machine_name,
    production_line,
    machine_type,
    CAST(DATE_TRUNC('WEEK', shift_date) AS DATE)         AS week_start,
    COUNT(shift_date)                                    AS days_in_week,
    CAST(AVG(oee)               AS DECIMAL(5,4))         AS avg_oee,
    CAST(MIN(oee)               AS DECIMAL(5,4))         AS min_oee,
    CAST(MAX(oee)               AS DECIMAL(5,4))         AS max_oee,
    SUM(total_units)                                     AS total_units_produced,
    SUM(good_units)                                      AS total_good_units,
    SUM(total_defects)                                   AS total_defects,
    SUM(downtime_hrs)                                    AS total_downtime_hrs,
    CAST(AVG(availability_rate) AS DECIMAL(5,4))         AS avg_availability,
    CAST(AVG(performance_rate)  AS DECIMAL(5,4))         AS avg_performance,
    CAST(AVG(quality_rate)      AS DECIMAL(5,4))         AS avg_quality
FROM {{ ref('mart_oee_daily') }}
GROUP BY machine_id, machine_name, production_line, machine_type,
         DATE_TRUNC('WEEK', shift_date)
