# dbt & Dagster — Concepts and Flow

This guide explains how dbt and Dagster work in this project, using the actual models,
assets, and data sources you have running.

---

## dbt

### What dbt does here

dbt transforms raw data inside Exasol into analytics-ready tables.
It does not move data — it runs SQL inside Exasol and materialises the results as views or tables.

```
Raw data (sources)  →  dbt models (SQL)  →  Analytics tables (marts)
```

### The three-layer model

```
SOURCES          STAGING              INTERMEDIATE         MARTS
(raw)            (clean + typed)      (OEE components)     (final analytics)

erp_pg.*    ──►  stg_erp__machines         ┐
erp_pg.*    ──►  stg_erp__production_orders ├──► int_availability ──┐
erp_pg.*    ──►  stg_erp__defects           ┘    int_performance  ──┼──► mart_oee_daily
                                                 int_quality      ──┘    mart_production_summary
iot.*       ──►  stg_iot__sensor_readings ──────────────────────────────►mart_machine_health
iot.*       ──►  stg_iot__downtime_events ──────────────────────────────►int_availability

ai.*        ─────────────────────────────────────────────────────────────►mart_ai_maintenance_queue
```

### Layer by layer

#### Staging (`STAGING` schema — views)

One staging model per source table. Does nothing except:
- Cast columns to the right types
- Rename columns to a consistent convention
- Derive simple calculated columns (e.g. `shift_duration_hrs` from start/end times)

No business logic here. If the source changes shape, only the staging model changes.

| Model | Source |
|---|---|
| `stg_erp__machines` | `ERP_PG.MACHINES` (PostgreSQL via Virtual Schema) |
| `stg_erp__production_orders` | `ERP_PG.PRODUCTION_ORDERS` |
| `stg_erp__defects` | `ERP_PG.DEFECTS` |
| `stg_iot__sensor_readings` | `IOT_RAW.SENSOR_READINGS` (native Exasol) |
| `stg_iot__downtime_events` | `IOT_RAW.DOWNTIME_EVENTS` (native Exasol) |

#### Intermediate (`INTERMEDIATE` schema — views)

Calculates the three OEE components. Each model is one formula applied across all machines and days.

| Model | Formula |
|---|---|
| `int_availability` | `(planned_time − downtime) / planned_time` per machine per day |
| `int_performance` | `actual_output / (theoretical_rate × available_time)` per machine per day |
| `int_quality` | `(total_units − defects) / total_units` per machine per day |

These are views — they are not stored, just referenced by the mart models above them.

#### Marts (`MARTS` schema — tables)

Final analytics tables, materialised as physical tables in Exasol.

| Model | Grain | What it contains |
|---|---|---|
| `mart_oee_daily` | machine × day | `OEE = Availability × Performance × Quality` |
| `mart_machine_health` | machine × day | Sensor averages, anomaly flag |
| `mart_production_summary` | machine × week | Trend data for management reporting |
| `mart_ai_maintenance_queue` | machine (latest) | OEE trend + sensor anomaly + AI recommendation |

### Sources

Sources are declared in `dbt_project/models/sources.yml`. dbt treats them as read-only inputs.

This project has three source groups:

**`erp_pg`** — PostgreSQL tables federated into Exasol via Virtual Schema.
dbt queries `ERP_PG.MACHINES` as if it were a native Exasol table.
Underneath, Exasol pushes the query down to PostgreSQL over JDBC — no data movement.

**`iot`** — Native Exasol tables. `IOT_RAW.SENSOR_READINGS` holds ~260,000 rows of 5-minute sensor readings.

**`ai`** — `AI_SCHEMA` tables written by `factory_ai_agent.py`.
`AI_SCHEMA.FAILURE_PATTERNS` holds `nomic-embed-text` embeddings (768-dimensional) of historical
downtime events — one row per event, embedding stored as a comma-separated VARCHAR.
`AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` is not a dbt model — it is written by a Python script.
Both are declared as dbt sources so dbt can test them and they appear in the lineage graph.
This is what makes the AI output first-class in dbt docs.

### `ref()` and `source()`

dbt models reference each other with two functions:

```sql
-- reference another dbt model
FROM {{ ref('mart_oee_daily') }}

-- reference a raw source table
FROM {{ source('iot', 'sensor_readings') }}
```

dbt resolves these at compile time into the correct schema-qualified table names,
and uses them to build the dependency graph that determines execution order.

`mart_ai_maintenance_queue` is the only model that bypasses this — it queries
`AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` directly (not via `source()`) because the table
is written by the AI agent mid-pipeline, not before dbt runs.

### `generate_schema_name` macro

By default dbt prefixes schema names with the target schema from `profiles.yml`.
This project overrides that in `macros/generate_schema_name.sql` so models land in
`STAGING`, `INTERMEDIATE`, and `MARTS` directly — not `DBT_MFG_STAGING` etc.

### Running dbt

```powershell
.\run.ps1 dbt-run     # build all 12 models
.\run.ps1 dbt-test    # run OEE bounds + nullability tests
.\run.ps1 docs        # lineage graph at http://localhost:8082
```

Under the hood, `dbt run` executes in dependency order — staging first, then intermediate,
then marts. `mart_oee_daily` and `mart_machine_health` use `incremental` materialisation
(`delete+insert` on a 3-day window) so subsequent runs only process recent data rather than
rebuilding from scratch. Use `dbt run --full-refresh` to force a complete rebuild.

---

## Dagster

### What Dagster does here

Dagster orchestrates the full pipeline automatically when new IoT data arrives.
Without Dagster, you run each step manually in sequence. With Dagster, you run one step
and the sensor watches for new data and re-runs the pipeline as needed.

### Assets

A Dagster **asset** is a piece of data that a function produces. Dagster tracks when each
asset was last materialised and what its upstream dependencies are.

This project has four assets:

```
core_dbt_assets
      │
      ▼
 ai_tables
      │
      ▼
ai_recommendations
      │
      ▼
mart_ai_maintenance_queue_refresh
```

| Asset | What it does | Defined in |
|---|---|---|
| `core_dbt_assets` | Runs dbt for all 11 models except the AI mart | `assets.py` (DbtCliResource) |
| `ai_tables` | Re-seeds `AI_SCHEMA.FAILURE_PATTERNS` — embeds each failure event via `nomic-embed-text` | `assets.py` |
| `ai_recommendations` | Runs `factory_ai_agent.py` — calls Ollama, writes to Exasol | `assets.py` |
| `mart_ai_maintenance_queue_refresh` | Runs `dbt run --select mart_ai_maintenance_queue` | `assets.py` |

The dependency order means:
- dbt builds OEE metrics first
- The AI agent reads those metrics to find at-risk machines
- The final dbt model reads the AI output to build the queue

### Job

A Dagster **job** is a named execution of a selection of assets. This project has one job:

```
manufacturing_refresh_job  =  all four assets, in dependency order
```

It is tagged with `dagster/max_concurrent_runs: 1` — Dagster will not start a second
run while one is already in progress.

You can trigger it manually: **Dagster UI → Jobs → manufacturing_refresh_job → Materialize all**

### Sensor

A **sensor** is a function that Dagster calls on a schedule to decide whether to trigger a job.

`new_sensor_data_sensor` runs every 60 seconds and:

1. Queries `SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS`
2. Compares to the count stored in its cursor (last known count)
3. If the count is higher → new data arrived → yield a `RunRequest` (trigger the job)
4. If a run is already in progress → yield `SkipReason` and wait
5. Otherwise → yield `SkipReason("No new rows")`

The cursor persists across sensor evaluations so it remembers the last count even after Dagster restarts.

**Important:** the sensor resets to *paused* every time Dagster restarts.
Re-enable it in the UI after each `.\run.ps1 orchestrate`.

### The full automated flow

```
seed-uci-live inserts new rows into IOT_RAW.SENSOR_READINGS
        │
        ▼  (every 60 s)
new_sensor_data_sensor detects row count increase
        │
        ▼
manufacturing_refresh_job triggered
        │
        ├── core_dbt_assets     (dbt run — 11 models)
        │         │
        ├── ai_tables           (re-seed failure patterns)
        │         │
        ├── ai_recommendations  (AI agent → Ollama → Exasol)
        │         │
        └── mart_ai_maintenance_queue_refresh  (dbt run — 1 model)
                  │
                  ▼
        MARTS.MART_AI_MAINTENANCE_QUEUE updated
```

### Dagster UI landmarks

| Location | What to find |
|---|---|
| **Assets** tab | Visual asset graph with materialisation history |
| **Jobs** tab | `manufacturing_refresh_job` — trigger manually here |
| **Automation** tab | `new_sensor_data_sensor` — toggle on/off here |
| **Runs** tab | Full log of every pipeline run, with per-asset stdout |

---

## How dbt and Dagster connect

Dagster wraps dbt via the `dagster-dbt` integration. The `DbtCliResource` in `definitions.py`
points to the `dbt_project/` directory and runs dbt as a subprocess. Each dbt model becomes
a Dagster asset — visible individually in the asset graph, with its own materialisation
timestamp and logs.

```python
# definitions.py
DbtCliResource(
    project_dir=str(DBT_PROJECT_DIR),
    profiles_dir=str(DBT_PROFILES_DIR),
    dbt_executable=DBT_CMD,
)
```

When `core_dbt_assets` materialises, you can click into any individual dbt model in the
Dagster UI and see its SQL output, row counts, and timing.

---

## Glossary — Time Dimensions

Three different date/time columns appear across models. They are not interchangeable.

| Column | Grain | Where used | Meaning |
|---|---|---|---|
| `shift_date` | 1 day | `mart_oee_daily`, `int_availability`, `int_performance`, `int_quality` | The calendar date of an 8-hour production shift. Derived from `production_orders.started_at` in staging. |
| `reading_date` | 1 day | `mart_machine_health`, `stg_iot__sensor_readings` | The calendar date a sensor reading was recorded. Derived from `sensor_readings.ts` via `DATE_TRUNC('DAY', ts)`. |
| `generated_at` | timestamp | `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS`, `mart_ai_maintenance_queue` | The exact timestamp when the AI agent wrote a maintenance recommendation. |

`shift_date` and `reading_date` usually align (same machine, same day) but come from different source systems and are joined independently — don't assume they are always present for the same dates.

---

## Configurable Anomaly Thresholds

`mart_machine_health` flags a machine as anomalous when sensor peaks deviate significantly from the day's mean. The thresholds default to:

- Temperature: `max_temp > avg_temp × 1.15` (15% above daily average)
- Vibration: `max_vibration > avg_vibration × 1.50` (50% above daily average)

These are defined as dbt variables and can be overridden without editing any SQL:

```powershell
# Tighten temperature threshold, loosen vibration
dbt run --vars '{"temp_anomaly_ratio": 1.10, "vib_anomaly_ratio": 1.80}'
```

Or set project-wide defaults in `dbt_project.yml` under the `vars:` key.
