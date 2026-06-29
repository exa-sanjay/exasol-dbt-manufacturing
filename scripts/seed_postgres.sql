-- ============================================================
-- ERP / MES seed data for the Exasol + DBT Manufacturing demo
-- Runs automatically when the postgres container first starts.
-- ============================================================

-- ── Schema ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS machines (
    machine_id            SERIAL PRIMARY KEY,
    machine_name          VARCHAR(100) NOT NULL,
    machine_type          VARCHAR(50)  NOT NULL,
    production_line       VARCHAR(10)  NOT NULL,
    theoretical_rate_per_hr INT        NOT NULL,  -- units/hour at 100% performance
    install_date          DATE         NOT NULL
);

CREATE TABLE IF NOT EXISTS production_orders (
    order_id      SERIAL PRIMARY KEY,
    machine_id    INT          NOT NULL REFERENCES machines(machine_id),
    product_sku   VARCHAR(20)  NOT NULL,
    shift_label   VARCHAR(20)  NOT NULL,  -- MORNING / AFTERNOON / NIGHT
    shift_start   TIMESTAMP    NOT NULL,
    shift_end     TIMESTAMP    NOT NULL,
    planned_qty   INT          NOT NULL,
    actual_qty    INT          NOT NULL
);

CREATE TABLE IF NOT EXISTS defects (
    defect_id     SERIAL PRIMARY KEY,
    order_id      INT         NOT NULL REFERENCES production_orders(order_id),
    defect_type   VARCHAR(50) NOT NULL,
    qty_defective INT         NOT NULL CHECK (qty_defective > 0)
);

CREATE TABLE IF NOT EXISTS maintenance_schedules (
    schedule_id   SERIAL PRIMARY KEY,
    machine_id    INT         NOT NULL REFERENCES machines(machine_id),
    window_start  TIMESTAMP   NOT NULL,
    window_end    TIMESTAMP   NOT NULL,
    maint_type    VARCHAR(20) NOT NULL  -- PLANNED / UNPLANNED
);

-- ── Machines ─────────────────────────────────────────────────
-- 10 machines across 3 production lines

INSERT INTO machines (machine_name, machine_type, production_line, theoretical_rate_per_hr, install_date) VALUES
('CNC Mill A1',       'CNC_MILL',       'LINE_A', 120, '2020-03-15'),
('CNC Mill A2',       'CNC_MILL',       'LINE_A', 120, '2020-05-20'),
('Lathe A3',          'LATHE',          'LINE_A',  90, '2019-11-10'),
('Lathe A4',          'LATHE',          'LINE_A',  90, '2021-01-08'),
('Welding Robot B1',  'WELDING_ROBOT',  'LINE_B', 200, '2022-06-01'),
('Welding Robot B2',  'WELDING_ROBOT',  'LINE_B', 200, '2022-06-01'),
('Assembly Bot B3',   'ASSEMBLY_BOT',   'LINE_B', 150, '2021-09-14'),
('Inj Mold C1',       'INJECTION_MOLD', 'LINE_C', 300, '2018-04-22'),
('Inj Mold C2',       'INJECTION_MOLD', 'LINE_C', 300, '2019-07-30'),
('Quality Scanner C3','QUALITY_SCANNER','LINE_C', 500, '2023-02-11');

-- ── Production Orders ────────────────────────────────────────
-- 90 days × 3 shifts × 10 machines = 2 700 orders
-- Performance varies per machine and shift (85-98% realistic range)
-- MORNING=06:00-14:00, AFTERNOON=14:00-22:00, NIGHT=22:00+06:00

INSERT INTO production_orders (machine_id, product_sku, shift_label, shift_start, shift_end, planned_qty, actual_qty)
SELECT
    m.machine_id,
    'SKU-' || LPAD((m.machine_id * 100 + (shift_num % 5) + 1)::TEXT, 4, '0') AS product_sku,
    CASE shift_num
        WHEN 0 THEN 'MORNING'
        WHEN 1 THEN 'AFTERNOON'
        ELSE       'NIGHT'
    END AS shift_label,
    -- shift start timestamps
    CASE shift_num
        WHEN 0 THEN (gen_date + INTERVAL '6 hours')::TIMESTAMP
        WHEN 1 THEN (gen_date + INTERVAL '14 hours')::TIMESTAMP
        ELSE       (gen_date + INTERVAL '22 hours')::TIMESTAMP
    END AS shift_start,
    CASE shift_num
        WHEN 0 THEN (gen_date + INTERVAL '14 hours')::TIMESTAMP
        WHEN 1 THEN (gen_date + INTERVAL '22 hours')::TIMESTAMP
        ELSE       (gen_date + INTERVAL '30 hours')::TIMESTAMP
    END AS shift_end,
    -- planned qty = 8h × theoretical_rate
    (m.theoretical_rate_per_hr * 8) AS planned_qty,
    -- actual qty = planned × performance factor (0.82..0.98, varies by machine+day)
    ROUND(
        (m.theoretical_rate_per_hr * 8) *
        (0.82 + (
            (SIN(EXTRACT(DOY FROM gen_date) * m.machine_id * 0.3 + shift_num) + 1) / 2.0
        ) * 0.16)
    )::INT AS actual_qty
FROM
    generate_series(
        CURRENT_DATE - INTERVAL '90 days',
        CURRENT_DATE - INTERVAL '1 day',
        INTERVAL '1 day'
    ) AS gs(gen_date)
CROSS JOIN machines m
CROSS JOIN generate_series(0, 2) AS shift(shift_num);

-- ── Defects ──────────────────────────────────────────────────
-- ~4-6% of orders have defects; quantity is 1-3% of actual_qty

INSERT INTO defects (order_id, defect_type, qty_defective)
SELECT
    po.order_id,
    CASE (po.order_id % 5)
        WHEN 0 THEN 'DIMENSIONAL_ERROR'
        WHEN 1 THEN 'SURFACE_SCRATCH'
        WHEN 2 THEN 'WELD_DEFECT'
        WHEN 3 THEN 'ASSEMBLY_GAP'
        ELSE       'MATERIAL_FLAW'
    END AS defect_type,
    GREATEST(1, ROUND(po.actual_qty * (0.005 + (po.order_id % 7) * 0.003)))::INT AS qty_defective
FROM production_orders po
WHERE po.order_id % 17 = 0   -- ~6% of orders
   OR po.order_id % 23 = 0;  -- additional ~4%

-- ── Maintenance Schedules ────────────────────────────────────
-- 2 planned windows per machine + a few unplanned ones

INSERT INTO maintenance_schedules (machine_id, window_start, window_end, maint_type)
SELECT
    m.machine_id,
    (CURRENT_DATE - INTERVAL '75 days' + (m.machine_id * INTERVAL '3 days'))::TIMESTAMP AS window_start,
    (CURRENT_DATE - INTERVAL '75 days' + (m.machine_id * INTERVAL '3 days') + INTERVAL '4 hours')::TIMESTAMP AS window_end,
    'PLANNED' AS maint_type
FROM machines m
UNION ALL
SELECT
    m.machine_id,
    (CURRENT_DATE - INTERVAL '30 days' + (m.machine_id * INTERVAL '2 days'))::TIMESTAMP,
    (CURRENT_DATE - INTERVAL '30 days' + (m.machine_id * INTERVAL '2 days') + INTERVAL '2 hours')::TIMESTAMP,
    'PLANNED'
FROM machines m
UNION ALL
-- Unplanned stoppages on 4 machines
SELECT machine_id,
       (CURRENT_DATE - INTERVAL '45 days' + INTERVAL '10 hours')::TIMESTAMP,
       (CURRENT_DATE - INTERVAL '45 days' + INTERVAL '13 hours')::TIMESTAMP,
       'UNPLANNED'
FROM machines WHERE machine_id IN (2, 5, 7, 9);

-- ── Indexes ──────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_po_machine_date  ON production_orders (machine_id, shift_start);
CREATE INDEX IF NOT EXISTS idx_defects_order    ON defects (order_id);
CREATE INDEX IF NOT EXISTS idx_maint_machine    ON maintenance_schedules (machine_id, window_start);
