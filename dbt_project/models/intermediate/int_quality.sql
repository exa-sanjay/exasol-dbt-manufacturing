-- Quality = good units / total units produced per machine per day.
-- Good units = actual output minus defective units reported in the ERP.

WITH orders_agg AS (
    SELECT
        machine_id,
        shift_date,
        SUM(actual_qty) AS total_units
    FROM {{ ref('stg_erp__production_orders') }}
    GROUP BY machine_id, shift_date
),

defects_agg AS (
    SELECT
        po.machine_id,
        po.shift_date,
        SUM(d.qty_defective) AS total_defects
    FROM {{ ref('stg_erp__defects') }} d
    JOIN {{ ref('stg_erp__production_orders') }} po
        ON d.order_id = po.order_id
    GROUP BY po.machine_id, po.shift_date
)

SELECT
    o.machine_id,
    o.shift_date,
    o.total_units,
    COALESCE(da.total_defects, 0)                                            AS total_defects,
    o.total_units - COALESCE(da.total_defects, 0)                           AS good_units,
    CAST(
        (o.total_units - COALESCE(da.total_defects, 0)) / NULLIF(o.total_units, 0)
    AS DECIMAL(5,4))                                                         AS quality_rate
FROM orders_agg o
LEFT JOIN defects_agg da
    ON o.machine_id = da.machine_id
    AND o.shift_date = da.shift_date
