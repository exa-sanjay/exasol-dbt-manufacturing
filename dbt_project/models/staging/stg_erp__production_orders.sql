-- Staging: production orders from PostgreSQL via Virtual Schema
-- Derives shift_date and shift_duration_hrs for downstream convenience.

SELECT
    CAST(order_id    AS INT)                         AS order_id,
    CAST(machine_id  AS INT)                         AS machine_id,
    TRIM(product_sku)                                AS product_sku,
    UPPER(TRIM(shift_label))                         AS shift_label,
    CAST(shift_start AS TIMESTAMP)                   AS shift_start,
    CAST(shift_end   AS TIMESTAMP)                   AS shift_end,
    CAST(DATE_TRUNC('DAY', shift_start) AS DATE)     AS shift_date,
    CAST(SECONDS_BETWEEN(shift_start, shift_end) / 3600.0 AS DECIMAL(5,2)) AS shift_duration_hrs,
    CAST(planned_qty AS INT)                         AS planned_qty,
    CAST(actual_qty  AS INT)                         AS actual_qty
FROM {{ source('erp_pg', 'production_orders') }}
