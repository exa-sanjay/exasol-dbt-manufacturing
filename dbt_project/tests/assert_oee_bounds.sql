-- Singular test: every row in mart_oee_daily must have OEE and all component
-- rates strictly within [0, 1]. Any row returned here is a test failure.

SELECT
    machine_id,
    shift_date,
    availability_rate,
    performance_rate,
    quality_rate,
    oee
FROM {{ ref('mart_oee_daily') }}
WHERE
    oee               < 0 OR oee               > 1
    OR availability_rate < 0 OR availability_rate > 1
    OR performance_rate  < 0 OR performance_rate  > 1
    OR quality_rate      < 0 OR quality_rate      > 1
    OR oee IS NULL
