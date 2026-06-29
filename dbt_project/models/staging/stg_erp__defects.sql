-- Staging: quality defects from PostgreSQL via Virtual Schema

SELECT
    CAST(defect_id     AS INT)        AS defect_id,
    CAST(order_id      AS INT)        AS order_id,
    UPPER(TRIM(defect_type))          AS defect_type,
    CAST(qty_defective AS INT)        AS qty_defective
FROM {{ source('erp_pg', 'defects') }}
