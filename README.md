<div align="center">

# 🏭 Exasol + dbt Manufacturing OEE Demo

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![Docker](https://img.shields.io/badge/Docker-required-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com)
[![dbt](https://img.shields.io/badge/dbt-1.x-FF694B?style=flat-square&logo=dbt&logoColor=white)](https://www.getdbt.com)
[![Ollama](https://img.shields.io/badge/Ollama-local%20AI-black?style=flat-square)](https://ollama.ai)
[![Exasol](https://img.shields.io/badge/Exasol-columnar%20DB-003580?style=flat-square)](https://www.exasol.com)

**Exasol as the AI analytical engine. dbt for transformations. A local RAG AI agent that detects failing machines and generates maintenance recommendations — no cloud, no API key.**

</div>

---

### What's inside

| | |
|---|---|
| **10 machines** · **~260 k IoT readings** · **90 days** of sensor history | High-volume time-series data seeded into Exasol |
| **12 dbt models** across staging → intermediate → marts | OEE, machine health, production summary, AI queue |
| **241 failure patterns** embedded via `nomic-embed-text` (768-dim) | Exasol acting as a vector store — no Pinecone, no Chroma |
| **`qwen2.5:7b`** generating maintenance recommendations locally | Full RAG pipeline, 100% on-laptop via Ollama |
| **Dagster sensor** polling Exasol for new IoT data | Fully automated pipeline — no manual re-runs |

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
| Docker Desktop | 4.0+ | Runs Exasol, PostgreSQL, and Ollama |
| Python | 3.10+ | Use a virtual environment (recommended) |
| Git | any | To clone the repo |

**System resources:** 16 GB RAM recommended. Exasol uses ~4 GB and the AI LLM (`qwen2.5:7b`) uses ~8 GB during inference. Reserve ~15 GB disk space (Docker images + two Ollama model downloads: `nomic-embed-text` ~274 MB + `qwen2.5:7b` ~4 GB).

**Clone the repo:**

```bash
git clone https://github.com/exa-sanjay/exasol-dbt-manufacturing.git
cd exasol-dbt-manufacturing
```

**Install Python dependencies** (includes dbt-core, dbt-exasol, dagster):

```powershell
# Recommended: use a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

> **Windows users:** if `.\run.ps1` is blocked by execution policy, run once:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

> **macOS / Linux users:** use `make <command>` instead of `.\run.ps1 <command>` throughout this guide.

---

## Using an AI assistant to set up this project

If you have access to an AI coding assistant, you can use it to guide the setup interactively — especially useful if you hit errors or are unfamiliar with one of the tools.

### Claude Code (best experience)

This repo includes a `CLAUDE.md` that gives Claude full project context automatically. In the project directory, start a session and ask:

```
claude
```
> "Help me set up this project from scratch on [Windows / macOS / Linux]. Walk me through each step and help me fix any errors."

Claude Code reads `CLAUDE.md`, all source files, and your terminal output — it can diagnose errors in context.

### Cursor / GitHub Copilot / Windsurf

Open the project folder in your IDE — the AI already sees `CLAUDE.md` and all source files. Ask in the chat panel:

> "Help me set up this project step by step. I'm on [Windows / macOS / Linux]."

---

## Repository Layout

```
exasol-dbt-manufacturing/
├── docker-compose.yml          # Exasol, PostgreSQL, Ollama
├── run.ps1                     # Windows command runner
├── Makefile                    # macOS / Linux command runner
├── requirements.txt
├── pyproject.toml              # Dagster code location config
│
├── scripts/
│   ├── setup_exasol.py         # Schema setup + IoT data seed
│   ├── seed_iot_from_uci.py    # Real-world UCI AI4I dataset loader
│   ├── setup_ai_tables.py      # AI vector store seed (failure patterns)
│   ├── factory_ai_agent.py     # AI agent: detect → retrieve → reason → recommend
│   ├── pull_ollama_model.py    # Pull Ollama models: nomic-embed-text + qwen2.5:7b (first run only)
│   ├── dashboard.py            # Streamlit multi-page dashboard (pipeline runner + analytics)
│   └── pages/                  # Streamlit pages: OEE, Machine Health, AI Queue, Production
│       └── utils.py            # Shared DB connection + query helpers for dashboard pages
│
├── dbt_project/
│   ├── profiles.yml.template   # Copy to ~/.dbt/profiles.yml (Step 1b)
│   ├── models/
│   │   ├── staging/            # 5 views — type-cast raw sources
│   │   ├── intermediate/       # 3 views — OEE component calculations
│   │   └── marts/              # 4 tables — final analytics + AI queue
│
├── orchestration/              # Dagster pipeline
│   ├── __init__.py
│   ├── assets.py               # 4 Dagster assets
│   ├── sensors.py              # Row-count sensor on IOT_RAW.SENSOR_READINGS
│   └── definitions.py          # Definitions, job, sensor wiring
│
└── docs/
    ├── ai-system-guide.md      # Deep-dive: embeddings, RAG pipeline, why it works
    ├── dbt-dagster-guide.md    # Deep-dive: dbt layers, Dagster assets, glossary
    └── query-reference.md      # SQL queries to verify each setup step
```

---

## Quickstart — Full End-to-End

### Step 1 — Start the containers

```powershell
.\run.ps1 up
```

Starts three Docker services and waits until all are ready:
- `exasol` — Exasol on ports `8563` (SQL) / `2581` (BucketFS)
- `postgres` — PostgreSQL ERP data
- `ollama` — Local LLM server on port `11434`

**First start takes ~2 minutes** while Exasol initialises its internal storage.

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

The default values connect to the local Docker Exasol — no edits needed.

---

### Step 2 — Seed the data

```powershell
.\run.ps1 seed
```

Downloads two JDBC adapter JARs (~20 MB, internet required), uploads them to Exasol's BucketFS, creates all schemas and the Virtual Schema pointing at PostgreSQL, then loads:

| Table | Rows | Description |
|---|---|---|
| `ERP_PG.MACHINES` (via Virtual Schema) | 10 | Machine master data |
| `ERP_PG.PRODUCTION_ORDERS` | ~2 700 | 10 machines × 90 days × 3 shifts |
| `ERP_PG.DEFECTS` | ~130 | 4–6% of production orders |
| `IOT_RAW.SENSOR_READINGS` | ~260 000 | 5-min sensor readings, 90 days |
| `IOT_RAW.DOWNTIME_EVENTS` | ~260 | Derived from sensor anomalies |

**Takes ~5–10 minutes** on first run.

---

### Step 3 — Run the dbt pipeline

```powershell
.\run.ps1 dbt-run
```

Downloads `dbt_utils` package (~1 MB, internet required on first run), then builds all 12 models in dependency order. **Takes ~25 seconds.**

```powershell
.\run.ps1 dbt-test    # optional — run OEE bounds + nullability tests
.\run.ps1 docs        # optional — serve lineage graph at http://localhost:8082
```

---

### Step 4 — Set up the AI layer

> **Requires:** Step 2 (seed) and Step 3 (dbt-run) must have completed successfully.
> `ai-setup` reads from `MARTS.MART_MACHINE_HEALTH` and `MARTS.MART_OEE_DAILY` to seed the vector store.

```powershell
.\run.ps1 ai-setup
```

- Creates `AI_SCHEMA.FAILURE_PATTERNS` and seeds ~241 historical failure event embeddings (calls Ollama's `nomic-embed-text` once per event — takes ~60 seconds)
- Pulls two Ollama models on first run: `nomic-embed-text` (**~274 MB**) and `qwen2.5:7b` (**~4 GB**). Subsequent runs are instant — models are cached in the `ollama_data` Docker volume
- Safe to re-run: drops and recreates `AI_SCHEMA` tables each time (`.\run.ps1 clean-ai` does the same without a full teardown)

---

### Step 5 — Run the AI agent

> **Requires:** Step 4 (ai-setup) must have completed successfully.
> The agent reads from the failure pattern vectors seeded in Step 4.

```powershell
.\run.ps1 ai-agent
```

Processes each at-risk machine in sequence:
1. Builds a natural-language description of the machine's current sensor state
2. Embeds that description via `nomic-embed-text` (Ollama) → 768-dimensional vector
3. Fetches all stored failure pattern embeddings from Exasol, ranks by cosine similarity (Python/numpy) → top-3 most similar past failures
4. Calls Ollama (`qwen2.5:7b`, local) with the machine stats + similar failures
5. Inserts the LLM response into `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS`

**Takes ~5–15 minutes** (roughly 60–90 seconds per at-risk machine).

After it finishes, query the results (see [Connecting to Exasol](#connecting-to-exasol) or the full **[Query Reference](docs/query-reference.md)**):

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

## Connecting to Exasol

The recommended SQL client for exploring the data is **DbVisualizer** — it has a built-in Exasol driver and lets you browse schemas, run queries, and inspect table contents without any setup.

### DbVisualizer (recommended)

1. Download and install DbVisualizer Free from [dbvis.com](https://www.dbvis.com)
2. Open DbVisualizer → **Database → Create Database Connection**
3. Select **Exasol** from the driver list (no manual driver download needed)
4. Fill in the connection details:

| Field | Value |
|---|---|
| **Host** | `localhost` |
| **Port** | `8563` |
| **User** | `sys` |
| **Password** | `exasol` |
| **Auto Commit** | On |

5. Click **Connect** — you should see the schemas `IOT_RAW`, `ERP_PG`, `MARTS`, `AI_SCHEMA` in the left panel

**Useful queries to run after connecting** (see also [docs/query-reference.md](docs/query-reference.md) for per-step verification queries):

```sql
-- Verify row counts
SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS;          -- ~260 000
SELECT COUNT(*) FROM ERP_PG.MACHINES;                  -- 10 (Virtual Schema working)
SELECT COUNT(*) FROM AI_SCHEMA.FAILURE_PATTERNS;       -- ~241

-- See the AI maintenance queue
SELECT machine_name, urgency_tier, estimated_hours_to_failure,
       root_cause, recommended_action, confidence
FROM MARTS.MART_AI_MAINTENANCE_QUEUE
ORDER BY
    CASE urgency_tier
        WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
        WHEN 'MEDIUM'   THEN 3 ELSE 4
    END;
```

> **SSL note:** Exasol in Docker uses a self-signed certificate. If DbVisualizer shows an SSL warning, go to **Connection → Advanced → SSL** and set **Trust Server Certificate** to `true`.

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

The AI layer is a **RAG pipeline** (Retrieval-Augmented Generation) running entirely locally — no cloud API, no API key. Two Ollama models handle the work:

| Model | Role | Size |
|---|---|---|
| `nomic-embed-text` | Embeds failure event descriptions into 768-dimensional vectors | ~274 MB |
| `qwen2.5:7b` | Reads machine context + similar past failures, writes the recommendation | ~4 GB |

**Short version of the pipeline:**
1. Find machines where `anomaly_flag = TRUE` or OEE is declining week-on-week
2. Describe each machine's current sensor state in plain text
3. Embed that description via `nomic-embed-text` → 768-dimensional vector
4. Fetch all stored failure embeddings from `AI_SCHEMA.FAILURE_PATTERNS` (Exasol), rank by cosine similarity (numpy) → top-3 most similar past failures
5. Send machine stats + those 3 failures to `qwen2.5:7b` → JSON recommendation
6. Insert result into `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` → read by the dbt mart

> **Full explanation** — what embeddings are, why they're better than hand-crafted features, how cosine similarity works, and why the architecture is interesting: see **[docs/ai-system-guide.md](docs/ai-system-guide.md)**.

### Urgency tiers (computed in the mart)

| Tier | Condition |
|---|---|
| CRITICAL | `estimated_hours_to_failure` ≤ 8 h |
| HIGH | ≤ 24 h |
| MEDIUM | ≤ 72 h |
| LOW | > 72 h |

---

## Dagster Orchestration

The pipeline is orchestrated by Dagster with four assets executing in dependency order:

```
core_dbt_assets  (11 dbt models, excludes mart_ai_maintenance_queue)
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

> **Note:** the sensor resets to *paused* every time Dagster restarts. Re-enable it in the UI after each `.\run.ps1 orchestrate`.

### Manual trigger

In the Dagster UI: **Jobs → manufacturing_refresh_job → Materialize all**

---

## Command Reference

```
.\run.ps1 <command>      # Windows
make <command>           # macOS / Linux

up              Start all Docker containers (waits until ready)
seed            Seed Exasol + PostgreSQL with synthetic IoT and ERP data
dbt-run         Build all 12 dbt models
dbt-test        Run dbt tests (OEE bounds, not_null, unique)
docs            Generate and serve dbt docs at http://localhost:8082

seed-uci        Replace synthetic IoT data with real UCI AI4I 2020 dataset (batch, then dbt-run)
seed-uci-live   Stream UCI data in real time (one reading per machine per 5 min, Ctrl+C to stop)

ai-setup        Create AI tables + seed failure embeddings + pull nomic-embed-text (~274 MB) and qwen2.5:7b (~4 GB, first run only)
ai-agent        Run the Factory AI Agent (~5-15 min, requires ai-setup)

dashboard       Open Streamlit analytics dashboard at http://localhost:8501
orchestrate     Start Dagster automation UI at http://localhost:3000
demo            Full pipeline: up + seed + dbt-run + dbt-test + docs
clean-ai        Drop AI_SCHEMA tables only (containers keep running — re-run ai-setup to rebuild)
clean           Stop containers and remove all volumes
```

### Which seed command to use?

| Command | When to use |
|---|---|
| `seed` | First run, or to reset back to clean synthetic data |
| `seed-uci` | Replace synthetic data with the real UCI AI4I 2020 dataset (more realistic anomalies) |
| `seed-uci-live` | Simulate a live factory: new readings arrive every 5 min. Run alongside `orchestrate` to see the full automated loop |

### Dashboard vs. Dagster

| Tool | Purpose | Start with |
|---|---|---|
| **Streamlit dashboard** (`dashboard`) | Explore OEE, sensor health, AI queue, and production KPIs interactively. No pipeline control. | `.\run.ps1 dashboard` |
| **Dagster UI** (`orchestrate`) | Trigger and monitor the full pipeline. Automate re-runs on new sensor data. | `.\run.ps1 orchestrate` |

Both can run at the same time — Dagster updates the data, Streamlit shows it.

---

## Verification Checklist

After completing Steps 1–5, verify everything is working. Use the **[Query Reference](docs/query-reference.md)** for the full set of per-step SQL queries, or run the quick health check below in DbVisualizer:

- [ ] `docker ps` shows `mfg_exasol`, `mfg_postgres`, `mfg_ollama` all running
- [ ] `SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS` returns ~260 000
- [ ] `SELECT COUNT(*) FROM ERP_PG.MACHINES` returns 10 (Virtual Schema working)
- [ ] dbt run completed: 12 models, 0 errors
- [ ] dbt test passed all OEE bounds and nullability checks
- [ ] `SELECT COUNT(*) FROM AI_SCHEMA.FAILURE_PATTERNS` returns ~241
- [ ] `SELECT COUNT(*) FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` returns > 0
- [ ] `SELECT * FROM MARTS.MART_AI_MAINTENANCE_QUEUE` shows at-risk machines with recommendations
- [ ] `.\run.ps1 orchestrate` → Dagster UI loads at http://localhost:3000 with 4 assets, 1 job, 1 sensor

---

## Troubleshooting

**`.\run.ps1 up` hangs waiting for Exasol**
Exasol takes ~90 seconds to initialise its internal storage on first start. If it hasn't become ready after 3 minutes, run `docker logs mfg_exasol --tail 50` to check for errors. A common cause is insufficient memory — ensure Docker Desktop has at least 8 GB RAM allocated.

**`dbt run` fails with "object ERP_PG.MACHINES not found"**
The Virtual Schema wasn't created yet. Run `.\run.ps1 seed` first.

**`dbt run` fails with "schema STAGING does not exist"**
This resolves itself on first run — dbt creates the schema automatically. If it persists, check that `~/.dbt/profiles.yml` exists (Step 1b) and that `user: sys` has `CREATE SCHEMA` privileges.

**`.\run.ps1 ai-setup` fails with "table MARTS.MART_MACHINE_HEALTH not found"**
Step 3 (`dbt-run`) must complete successfully before Step 4. Run `.\run.ps1 dbt-run` first.

**Ollama model download stalls or times out**
`ai-setup` pulls two models: `nomic-embed-text` (~274 MB) and `qwen2.5:7b` (~4 GB). On a slow connection `qwen2.5:7b` can take 30+ minutes. The script retries up to 3 times with backoff. To pull manually: `docker exec mfg_ollama ollama pull nomic-embed-text` and `docker exec mfg_ollama ollama pull qwen2.5:7b`.

**`.\run.ps1 ai-agent` produces no recommendations**
Check that at-risk machines exist: `SELECT * FROM MARTS.MART_MACHINE_HEALTH WHERE ANOMALY_FLAG = TRUE`. If the table is empty, the seed data may not have generated anomalies — run `.\run.ps1 seed` again to regenerate.

**Dagster sensor shows "No new rows" indefinitely**
The sensor compares `COUNT(*)` to its last-seen cursor. After a `clean` + `seed` cycle the row count resets, but the cursor still holds the old value. Fix: in the Dagster UI, go to **Automation → new_sensor_data_sensor → Reset cursor**, then re-enable.

---

## What's Next

| Extension | How |
|---|---|
| **Production scale** | Swap `exasol/docker-db` (community Docker image) for Exasol SaaS — only the `~/.dbt/profiles.yml` host changes |
| **Live dashboard** | Point Metabase or Grafana at `MARTS.MART_AI_MAINTENANCE_QUEUE` |
| **Real IoT ingest** | Replace the seed script with a Kafka → Exasol connector |
| **Larger LLM** | Swap `qwen2.5:7b` for `qwen2.5-coder:14b` in Ollama for higher-quality reasoning |
