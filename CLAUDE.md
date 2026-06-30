# CLAUDE.md — Project context for AI coding assistants

## What this project is

A self-contained manufacturing OEE (Overall Equipment Effectiveness) demo that runs
entirely on a laptop via Docker. It combines:

- **Exasol** (columnar OLAP database) as the primary analytical store and AI vector store
- **PostgreSQL** as the ERP source (accessed from Exasol via Virtual Schema)
- **dbt** (data build tool) for the transformation layer
- **Dagster** for orchestration
- **Ollama** (local Docker service) running two models:
  - `nomic-embed-text` — 768-dimensional text embeddings
  - `qwen2.5:7b` — LLM for generating maintenance recommendations
- **Streamlit** for the dashboard (`scripts/dashboard.py`)

The AI layer is a RAG pipeline: historical failure events are embedded and stored in
Exasol, then retrieved via cosine similarity (computed in Python/numpy) to give the LLM
context for generating maintenance recommendations.

## How to run (fresh setup)

```bash
# 1. Start containers
docker compose up -d

# 2. Seed data into Exasol + PostgreSQL (~260k IoT rows)
python scripts/setup_exasol.py

# 3. Build dbt models (staging → marts)
cd dbt_project && dbt deps && dbt run

# 4. Pull Ollama models + embed failure patterns (~4.3 GB first run)
python scripts/pull_ollama_model.py
python scripts/setup_ai_tables.py

# 5. Run the AI agent
python scripts/factory_ai_agent.py

# 6. Launch the dashboard
streamlit run scripts/dashboard.py
```

Or use the Streamlit dashboard (step 6) and click through the 5 pipeline steps in order.

## Key files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Exasol, PostgreSQL, Ollama containers |
| `scripts/setup_exasol.py` | Creates schemas, uploads Virtual Schema JAR to BucketFS, seeds IoT + ERP data |
| `scripts/setup_ai_tables.py` | Creates `AI_SCHEMA` tables, embeds 241 failure events via nomic-embed-text |
| `scripts/factory_ai_agent.py` | RAG agent: detect at-risk machines → embed → cosine similarity → LLM → store recommendation |
| `scripts/pull_ollama_model.py` | Downloads both Ollama models with progress reporting |
| `scripts/ai_constants.py` | Single constant: `EMBED_MODEL = "nomic-embed-text"` |
| `scripts/dashboard.py` | Streamlit multi-page app (home + 4 analytics pages) |
| `scripts/pages/` | `1_OEE_Overview.py`, `2_Machine_Health.py`, `3_AI_Queue.py`, `4_Production.py` |
| `scripts/pages/utils.py` | Shared Exasol connection helper for all dashboard pages |
| `dbt_project/models/` | `staging/` → `intermediate/` → `marts/` (12 models total) |
| `dbt_project/models/marts/mart_ai_maintenance_queue.sql` | Final mart joining OEE + sensor health + AI recommendations |
| `orchestration/` | Dagster assets, sensors, definitions |
| `docs/ai-system-guide.md` | How the RAG pipeline works (embeddings, cosine similarity, retrieval flow) |
| `docs/query-reference.md` | SQL queries to verify each setup step |

## Exasol connection defaults

```
DSN:      localhost:8563
User:     sys
Password: exasol
```

BucketFS (for Virtual Schema JAR upload):
```
Port:     2580
User:     w
Password: auto-detected from Docker container at runtime
```

## Database schemas

| Schema | Contents |
|---|---|
| `IOT_RAW` | `SENSOR_READINGS` (~260k rows), `DOWNTIME_EVENTS` (~241 rows) |
| `ERP_PG` | Virtual Schema over PostgreSQL — `MACHINES`, `PRODUCTION_ORDERS`, `DEFECTS` |
| `STAGING` | dbt views over raw sources |
| `INTERMEDIATE` | dbt intermediate models |
| `MARTS` | `MART_OEE_DAILY`, `MART_MACHINE_HEALTH`, `MART_PRODUCTION_SUMMARY`, `MART_AI_MAINTENANCE_QUEUE` |
| `AI_SCHEMA` | `FAILURE_PATTERNS` (embeddings), `MAINTENANCE_RECOMMENDATIONS` (LLM output) |

## AI / embedding details

- Embedding model: `nomic-embed-text` via `POST http://localhost:11434/api/embed`
  - Request body: `{"model": "nomic-embed-text", "input": "<text>"}`
  - Response: `resp.json()["embeddings"][0]` — list of 768 floats
- Embeddings stored as comma-separated VARCHAR(20000) in `AI_SCHEMA.FAILURE_PATTERNS`
- Parsed back with: `np.fromstring(row["embedding"], dtype=float, sep=",")`
- Cosine similarity computed in Python (numpy), not in SQL
- LLM model: `qwen2.5:7b` via `POST http://localhost:11434/api/generate`

## dbt profiles

Copy `dbt_project/profiles.yml.template` to `~/.dbt/profiles.yml`. Default values
connect to the local Docker Exasol — no edits needed for the demo.

## Streamlit pages — sys.path note

All files under `scripts/pages/` must include this at the top (Streamlit does not add
the pages directory to sys.path automatically):

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
```

## What NOT to change without understanding the implications

- **BucketFS password detection** (`setup_exasol.py: _detect_bfs_password`): reads the
  base64-encoded password from the running Docker container and decodes it. Don't
  hardcode this — it changes on each fresh container start.
- **Embedding API endpoint**: use `/api/embed` (not `/api/embeddings` — that's the
  deprecated Ollama v1 endpoint). Request field is `input`, response key is
  `embeddings[0]`.
- **ai-setup order**: `pull_ollama_model.py` must run before `setup_ai_tables.py` —
  the embedding API will 404 if nomic-embed-text hasn't been pulled yet.
- **Streamlit column output scope**: don't put `if st.button():` logic inside a
  `with col:` block or all output renders in that narrow column. Use
  `clicked = col.button(...)` and handle `if clicked:` at the outer scope.
