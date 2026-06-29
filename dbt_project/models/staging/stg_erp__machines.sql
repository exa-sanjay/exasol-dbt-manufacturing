-- Staging: machine master from PostgreSQL via Virtual Schema
-- Casts types, uppercases categoricals, no business logic here.

SELECT
    CAST(machine_id           AS INT)         AS machine_id,
    TRIM(machine_name)                         AS machine_name,
    UPPER(TRIM(machine_type))                  AS machine_type,
    UPPER(TRIM(production_line))               AS production_line,
    CAST(theoretical_rate_per_hr AS INT)       AS theoretical_rate_per_hr,
    CAST(install_date AS DATE)                 AS install_date
FROM {{ source('erp_pg', 'machines') }}
