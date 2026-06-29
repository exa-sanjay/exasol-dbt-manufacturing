# Exasol + dbt Manufacturing OEE Demo

A self-contained demo showing how Exasol acts as the central analytics engine in a manufacturing architecture — federating ERP data from PostgreSQL and high-volume IoT sensor data, transforming both into **OEE (Overall Equipment Effectiveness)** metrics with dbt, and running a **local AI agent** that detects at-risk machines and generates maintenance recommendations — all without a cloud API.

---

## Architecture

```
┌─────────────────────────────┐     ┌────────────────────────────────────────────────┐
│  PostgreSQL  (ERP / MES)    │     │  Exasol  (Analytics + IoT)                     │
│  · machines                 │     │  · IOT_RAW.sensor_readings  (~260k rows)        │
│  · production_orders        │◄────│  · IOT_RAW.downtime_events                     │
│  · defects                  │     │  · ERP_PG  (Virtual Schema → PostgreSQL)        │
│  · maintenance_schedules    │     │  · AI_SCHEMA  (vector store + recommendations)  │
└─────────────────────────────┘     └────────────────────────────────────────────────┘
                                                       │
                                             dbt-exasol adapter
                                                       │
                                          ┌────────────▼────────────┐
                                          │  dbt  (12 models)        │
                                          │  staging → intermediate  │
                                          │        → marts           │
                                          └────────────┬────────────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │  Dagster Orchestration   │
                                          │  sensor polls Exasol     │
                                          │  auto-triggers pipeline  │
                                          │  on new IoT data         │
                                          └─────────────────────────┘
```

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker + Docker Compose | 20+ | Runs Exasol, PostgreSQL, and Ollama |
| Python | 3.10+ | Use a virtual environment (recommended) |

Install all Python dependencies (includes dbt-core, dbt-exasol, dagster):

```powershell
# Recommended: use a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

---

## Repository Layout

```
exasol-dbt-manufacturing/
├── docker-compose.yml          # Exasol, PostgreSQL, Ollama
├── Makefile                    # All runnable targets
├── requirements.txt
├── pyproject.toml              # Dagster code location config
│
├── scripts/
│   ├── setup_exasol.py         # Schema setup + IoT data seed
│   ├── seed_iot_from_uci.py    # Real-world UCI AI4I dataset loader
│   ├── setup_ai_tables.py      # AI vector store seed (failure patterns)
│   ├── factory_ai_agent.py     # AI agent: detect → retrieve → reason → recommend
│   └── pull_ollama_model.py    # Pull Ollama LLM model (first run only)
│
├── dbt_project/
│   ├── models/
│   │   ├── staging/            # 5 views — type-cast raw sources
│   │   ├── intermediate/       # 3 views — OEE component calculations
│   │   └── marts/              # 4 tables — final analytics + AI queue
│   └── profiles.yml (in ~/.dbt/)
│
├── orchestration/              # Dagster pipeline
│   ├── __init__.py
│   ├── assets.py               # 4 Dagster assets
│   ├── sensors.py              # Row-count sensor on IOT_RAW.SENSOR_READINGS
│   └── definitions.py          # Definitions, job, sensor wiring
│
└── docs/
    └── findings.html           # Implementation notes + Exasol quirks
```

---

## Quickstart — Full End-to-End

### Step 1 — Start the containers

```powershell
.\run.ps1 up
```

Starts three Docker services and waits for both databases to be ready:
- `exasol` — Exasol Nano on ports `8563` (SQL) / `2581` (BucketFS)
- `postgres` — PostgreSQL (ERP data)
- `ollama` — Local LLM server on port `11434`

First start takes ~90 seconds while Exasol initialises.

---

### Step 1b — Configure dbt connection

dbt needs a connection profile in your home directory:

```powershell
# Windows
New-Item -ItemType Directory -Force $HOME\.dbt | Out-Null
Copy-Item dbt_project\profiles.yml.template $HOME\.dbt\profiles.yml

# macOS / Linux
mkdir -p ~/.dbt
cp dbt_project/profiles.yml.template ~/.dbt/profiles.yml
```

The default values in the template connect to the local Docker Exasol — no edits needed for the demo.

---

### Step 2 — Seed the data

```powershell
.\run.ps1 seed
```

Creates all schemas, the Virtual Schema pointing to PostgreSQL, and loads:

| Table | Rows | Description |
|---|---|---|
| `ERP_PG.MACHINES` (via Virtual Schema) | 10 | Machine master data |
| `ERP_PG.PRODUCTION_ORDERS` | ~2 700 | 10 machines × 90 days × 3 shifts |
| `ERP_PG.DEFECTS` | ~130 | 4–6% of production orders |
| `IOT_RAW.SENSOR_READINGS` | ~260 000 | 5-min sensor readings, 90 days |
| `IOT_RAW.DOWNTIME_EVENTS` | ~260 | Derived from sensor anomalies |

---

### Step 3 — Run the dbt pipeline

```powershell
.\run.ps1 dbt-run
```

Builds all 12 models in dependency order. Takes ~25 seconds.

```powershell
.\run.ps1 dbt-test    # optional — run OEE bounds + nullability tests
.\run.ps1 docs        # optional — serve lineage graph at http://localhost:8081
```

---

### Step 4 — Set up the AI layer

```powershell
.\run.ps1 ai-setup
```

- Creates `AI_SCHEMA.FAILURE_PATTERNS` and `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` in Exasol
- Seeds ~241 failure pattern vectors (6D feature vectors built from historical downtime + OEE data)
- Pulls the Ollama LLM model `qwen2.5:7b` (~4 GB, first run only)

---

### Step 5 — Run the AI agent

```powershell
.\run.ps1 ai-agent
```

For each at-risk machine the agent:
1. Builds a 6D feature vector (machine type, temp/vibration/power Z-scores, OEE drop)
2. Runs cosine similarity SQL inside Exasol to retrieve the top-3 similar past failures
3. Calls Ollama (`qwen2.5:7b`, local) with the machine stats + similar failures
4. Inserts the LLM response into `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS`

After it runs, query the results:

```sql
SELECT machine_name, urgency_tier, estimated_hours_to_failure,
       root_cause, recommended_action, confidence
FROM MARTS.MART_AI_MAINTENANCE_QUEUE
ORDER BY
  CASE urgency_tier
    WHEN 'CRITICAL' THEN 1
    WHEN 'HIGH'     THEN 2
    WHEN 'MEDIUM'   THEN 3
    ELSE 4
  END;
```

---

### Step 6 — Start automated orchestration

```powershell
.\run.ps1 orchestrate
```

Opens the Dagster UI at **http://localhost:3000**.

To enable automatic triggering:
1. Click **Automation** in the left sidebar
2. Find `new_sensor_data_sensor`
3. Toggle it **on**

From this point the pipeline runs automatically whenever new rows arrive in `IOT_RAW.SENSOR_READINGS` — no manual intervention needed.

To simulate live sensor data flowing in:

```powershell
.\run.ps1 seed-uci-live    # streams one reading per machine every 5 minutes
```

---

## dbt Model Reference

### Staging (views — `STAGING` schema)

| Model | Source | Purpose |
|---|---|---|
| `stg_erp__machines` | PostgreSQL via Virtual Schema | Type casts, standardise machine type categories |
| `stg_erp__production_orders` | PostgreSQL via Virtual Schema | Derive `shift_date`, `shift_duration_hrs` |
| `stg_erp__defects` | PostgreSQL via Virtual Schema | Type casts |
| `stg_iot__sensor_readings` | Exasol native | Add `ts_hour`, `reading_date` time buckets |
| `stg_iot__downtime_events` | Exasol native | Calculate `duration_hrs` from timestamps |

### Intermediate (views — `INTERMEDIATE` schema)

| Model | What it calculates |
|---|---|
| `int_availability` | `(planned_time − downtime) / planned_time` per machine per day |
| `int_performance` | `actual_output / (theoretical_rate × available_time)` per machine per day |
| `int_quality` | `(total_units − defects) / total_units` per machine per day |

### Marts (tables — `MARTS` schema)

| Model | Grain | Use case |
|---|---|---|
| `mart_oee_daily` | machine × day | Primary OEE fact — `OEE = Availability × Performance × Quality` |
| `mart_machine_health` | machine × day | Sensor averages + anomaly flag |
| `mart_production_summary` | machine × week | Trend dashboards, management reports |
| `mart_ai_maintenance_queue` | machine (latest) | At-risk machines with AI recommendations |

---

## OEE Formula

**OEE = Availability × Performance × Quality**

```
Availability  =  (Planned Production Time − Downtime)  /  Planned Production Time
Performance   =  Actual Output  /  (Theoretical Rate × Available Time)
Quality       =  Good Units  /  Total Units Produced
```

World-class OEE ≥ 85%. A typical unoptimised plant runs 40–60%.

---

## AI System — How It Works

```
IoT Sensor Readings  ──►  mart_machine_health (anomaly_flag + sensor stats)
                                    │
                                    ▼
                          factory_ai_agent.py
                            │
                            ├─ 1. Find at-risk machines
                            │     (anomaly=TRUE or OEE below 7-day average)
                            │
                            ├─ 2. Build 6D feature vector per machine
                            │     (machine_type_code, temp_zscore, vib_zscore,
                            │      pwr_zscore, oee_drop, downtime_hrs)
                            │
                            ├─ 3. Cosine similarity SQL in Exasol
                            │     → top-3 most similar past failures
                            │     (Exasol acting as vector store — no Pinecone/Chroma)
                            │
                            ├─ 4. Call Ollama (qwen2.5:7b, local Docker)
                            │     → root_cause, recommended_action,
                            │       estimated_hours_to_failure, confidence
                            │
                            └─ 5. INSERT into AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS
                                            │
                                            ▼
                              mart_ai_maintenance_queue  (dbt mart)
                              OEE trend + sensor anomaly + AI recommendation
```

### Urgency tiers (computed in the mart)

| Tier | Hours to estimated failure |
|---|---|
| CRITICAL | ≤ 8 h |
| HIGH | ≤ 24 h |
| MEDIUM | ≤ 72 h |
| LOW | > 72 h |

### Why this is interesting

| Concept | Detail |
|---|---|
| **Exasol as vector store** | Failure patterns are 6D vectors stored in Exasol. Cosine similarity runs as pure SQL — no external vector database required |
| **100% local LLM** | `qwen2.5:7b` via Ollama in Docker — no cloud API, no API key, works in air-gapped factory environments |
| **AI output in dbt lineage** | `mart_ai_maintenance_queue` is a dbt model — AI recommendations are first-class in the lineage graph and dbt docs |
| **Closed feedback loop** | IoT data → Exasol analytics → AI agent → Exasol recommendation table → dbt mart → dashboards |

---

## Dagster Orchestration

The pipeline is orchestrated by Dagster with four assets executing in dependency order:

```
core_dbt_assets  (12 dbt models, excludes mart_ai_maintenance_queue)
        │
        ▼
   ai_tables  (re-seeds AI_SCHEMA.FAILURE_PATTERNS from latest mart data)
        │
        ▼
ai_recommendations  (runs factory_ai_agent.py — calls Ollama, writes to Exasol)
        │
        ▼
mart_ai_maintenance_queue_refresh  (rebuilds the final AI mart via dbt)
```

The `new_sensor_data_sensor` polls `IOT_RAW.SENSOR_READINGS` every 60 seconds. When the row count increases it triggers `manufacturing_refresh_job`, running all four assets automatically.

### Manual trigger

In the Dagster UI: **Jobs → manufacturing_refresh_job → Materialize all**

---

## Command Reference

Run all commands from the project root with `.\run.ps1 <command>`:

```
up              Start all Docker containers
seed            Seed Exasol + PostgreSQL with synthetic IoT and ERP data
dbt-run         Build all 12 dbt models
dbt-test        Run dbt tests (OEE bounds, not_null, unique)
docs            Generate and serve dbt docs at http://localhost:8081

seed-uci        Load real UCI AI4I 2020 Predictive Maintenance dataset (batch)
seed-uci-live   Stream UCI data in real time (one reading per machine per 5 min)

ai-setup        Create AI tables + seed failure vectors + pull Ollama model
ai-agent        Run the Factory AI Agent

orchestrate     Start Dagster UI at http://localhost:3000
demo            Full pipeline: up + seed + dbt-run + dbt-test + docs
clean           Stop containers and remove all volumes
```

---

## Verification Checklist

After `.\run.ps1 demo` then `.\run.ps1 ai-setup` then `.\run.ps1 ai-agent`:

- [ ] `docker ps` shows `mfg_exasol`, `mfg_postgres`, `mfg_ollama` all running
- [ ] `SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS` returns ~260 000
- [ ] `SELECT * FROM ERP_PG.MACHINES` returns 10 rows through the Virtual Schema
- [ ] `dbt run` completes: 12 models, 0 errors
- [ ] `dbt test` passes all OEE bounds and nullability checks
- [ ] `SELECT COUNT(*) FROM AI_SCHEMA.FAILURE_PATTERNS` returns ~241
- [ ] `SELECT COUNT(*) FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` returns > 0
- [ ] `SELECT * FROM MARTS.MART_AI_MAINTENANCE_QUEUE` shows at-risk machines with AI recommendations
- [ ] `.\run.ps1 orchestrate` → Dagster UI loads at http://localhost:3000 with 4 assets, 1 job, 1 sensor

---

## What's Next

| Extension | How |
|---|---|
| **Production scale** | Swap Exasol Nano for Exasol SaaS — only the `~/.dbt/profiles.yml` host changes |
| **Live dashboard** | Point Metabase or Grafana at `MARTS.MART_AI_MAINTENANCE_QUEUE` |
| **Better embeddings** | Replace the 6D feature vector with `nomic-embed-text` embeddings via Ollama |
| **Real IoT ingest** | Replace the seed script with a Kafka → Exasol connector |
| **Larger LLM** | Swap `qwen2.5:7b` for `qwen2.5-coder:14b` in Ollama for higher-quality reasoning |
