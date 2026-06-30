# AI System — How It Works

The AI layer is a **RAG pipeline** (Retrieval-Augmented Generation) that runs entirely on
your laptop — no cloud API, no API key. Two local Ollama models do all the work:

| Model | Role | Size |
|---|---|---|
| `nomic-embed-text` | Converts text descriptions into 768-dimensional vectors | ~274 MB |
| `qwen2.5:7b` | Reads machine context + similar past failures, writes the recommendation | ~4 GB |

---

## What is an embedding and why does it matter?

An **embedding** is a list of numbers (a vector) that encodes the *meaning* of a piece of
text, not just the words. The key property is that texts with similar meaning end up with
similar numbers — so you can measure "how similar are these two situations?" by comparing
their vectors.

For this demo, every historical failure event is described in plain language and then embedded:

```
Machine type: CNC_MILL
Failure type: BEARING_FAILURE
Sensor readings on failure day:
  Temperature: max 94.3°C, average 78.2°C
  Vibration: max 2.847 mm/s, average 1.234 mm/s
  Power: max 45.2 kW, average 38.7 kW
OEE: 67.3%
Downtime: 4.5 hours
```

`nomic-embed-text` turns that description into 768 numbers. When a machine starts showing
anomalies today, the agent builds the same kind of description for its *current* state,
embeds it identically, then asks: *which stored failure descriptions are most numerically
similar to this one?*

### Cosine similarity

That similarity score — **cosine similarity** — measures the angle between two vectors.

- Score **1.0** → vectors point in the same direction (same meaning)
- Score **0.0** → vectors are perpendicular (unrelated)
- Score **−1.0** → opposite meaning

The agent ranks all ~241 stored failure embeddings against the query vector and returns
the top-3 closest matches. These are the historical failures most likely to resemble what
is happening right now.

### Why embeddings instead of hand-picked features?

An earlier approach would manually choose a few numbers to compare — temperature z-score,
vibration z-score, OEE drop, and so on. That works, but it bakes in assumptions about
which signals matter most and how they relate to each other.

A 768-dimensional embedding trained on a large text corpus understands context that numbers
alone cannot encode. For example, it understands that:

- "bearing failure with high vibration and moderate temperature" is semantically closer to
  "shaft wear with elevated vibration" than to "power supply fault" — even if the raw
  sensor numbers happen to look similar
- "ASSEMBLY_BOT" and "WELDING_ROBOT" have different failure modes than "CNC_MILL", and the
  embedding captures that from the machine type string alone

You don't have to engineer those relationships by hand. The model has already learned them.

---

## The full pipeline

### Setup (`make ai-setup`)

`setup_ai_tables.py` runs once to build the knowledge base:

1. Creates `AI_SCHEMA.FAILURE_PATTERNS` and `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` in Exasol
2. Fetches all historical downtime events from `IOT_RAW.DOWNTIME_EVENTS`, joined with sensor
   stats from `MARTS.MART_MACHINE_HEALTH` and OEE from `MARTS.MART_OEE_DAILY`
3. For each event, builds a plain-text description (machine type, reason code, sensor readings,
   OEE, downtime hours)
4. Calls `POST http://localhost:11434/api/embeddings` with that description — `nomic-embed-text`
   returns a list of 768 floats
5. Stores the description and the embedding as a comma-separated VARCHAR in
   `AI_SCHEMA.FAILURE_PATTERNS`

After this step, Exasol holds the full vector knowledge base — ~241 rows, each with a
768-dimensional representation of a real failure event.

### Agent run (`make ai-agent`)

`factory_ai_agent.py` runs on demand (or automatically via Dagster) to generate
recommendations for today's at-risk machines:

```
Step 1  Scan for at-risk machines
        ─────────────────────────
        Query mart_machine_health + mart_oee_daily.
        A machine is "at-risk" if:
          anomaly_flag = TRUE  (sensor peak > daily average by configured threshold)
          OR 7-day OEE fell more than 3 percentage points vs the prior 7 days

Step 2  Describe the machine's current state
        ─────────────────────────────────────
        Build a plain-text description of the current sensor readings
        (same format as the stored failure descriptions):

          Machine type: LATHE
          Current anomaly detected
          Sensor readings (recent 7-day window):
            Temperature: max 91.2°C, average 76.4°C
            Vibration: max 3.102 mm/s, average 1.456 mm/s
            Power: max 41.8 kW, average 37.2 kW
          7-day OEE: 71.4%

Step 3  Embed the description
        ───────────────────────
        POST /api/embeddings → nomic-embed-text (Ollama)
        Result: [0.01234567, -0.02345678, ... ]  ← 768 floats

Step 4  Retrieve the most similar past failures
        ─────────────────────────────────────────
        Fetch all rows from AI_SCHEMA.FAILURE_PATTERNS (description + embedding + metadata).
        For each stored embedding, compute cosine similarity against the query vector in
        Python (numpy). Return the top-3 highest-scoring matches.

        Example result:
          1. BEARING_FAILURE  on 2024-03-12  (CNC_MILL,  4.5h downtime, 94% match)
          2. SHAFT_MISALIGN   on 2024-01-28  (LATHE,     2.1h downtime, 87% match)
          3. LUBRICATION_FAIL on 2024-02-05  (LATHE,     3.8h downtime, 82% match)

Step 5  Call the LLM
        ─────────────
        POST /api/generate → qwen2.5:7b (Ollama) with a structured prompt:
          - current sensor readings for this machine
          - the 3 similar past failures (type, date, downtime, similarity %)
          - instruction to respond with JSON only

        LLM response (validated before storing):
          {
            "root_cause": "Bearing wear indicated by sustained high vibration...",
            "recommended_action": "Inspect and replace main spindle bearings...",
            "estimated_hours_to_failure": 18.0,
            "confidence": "HIGH"
          }

Step 6  Persist the recommendation
        ───────────────────────────
        DELETE any existing recommendation for this machine from today (idempotent).
        INSERT the new recommendation into AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS.
```

### After the agent (`dbt run --select mart_ai_maintenance_queue`)

The final dbt model joins the recommendation with OEE trends and sensor health, adds the
`urgency_tier` column, and materialises the result as a table. This is what the dashboard
and the Dagster pipeline both read.

---

## How retrieval works (step by step)

When `factory_ai_agent.py` runs for an at-risk machine, the retrieval process is:

**1. Build a query description**

The agent describes the machine's current sensor state in the same plain-text format used
during setup:

```
Machine type: LATHE
Current anomaly detected
Sensor readings (recent 7-day window):
  Temperature: max 91.2°C, average 76.4°C
  Vibration: max 3.102 mm/s, average 1.456 mm/s
  Power: max 41.8 kW, average 37.2 kW
7-day OEE: 71.4%
```

**2. Embed the query**

```python
resp = requests.post("http://localhost:11434/api/embed",
                     json={"model": "nomic-embed-text", "input": description})
query_vector = resp.json()["embeddings"][0]   # list of 768 floats
```

**3. Fetch all stored embeddings from Exasol**

```sql
SELECT pattern_id, machine_type, event_date, reason_code, downtime_hrs, embedding
FROM AI_SCHEMA.FAILURE_PATTERNS
```

~241 rows come back. Each `embedding` column is a comma-separated string of 768 floats
(~10 KB per row, ~2.4 MB total). This is fast — Exasol is the store, Python does the math.

**4. Compute cosine similarity in Python (numpy)**

```python
import numpy as np

q      = np.array(query_vector)              # 768D query vector
q_norm = np.linalg.norm(q)

scored = []
for row in all_patterns:
    emb      = np.fromstring(row["embedding"], sep=",")   # parse VARCHAR → array
    emb_norm = np.linalg.norm(emb)
    sim      = np.dot(q, emb) / (q_norm * emb_norm)      # cosine similarity
    scored.append({**row, "similarity": sim})

top3 = sorted(scored, key=lambda x: x["similarity"], reverse=True)[:3]
```

**Cosine similarity** measures the angle between two vectors. A score of `1.0` means the
vectors point in the same direction — identical meaning. `0.0` means unrelated.

**5. Return the top-3 matches**

```
1. BEARING_FAILURE  2024-03-12  LATHE       4.5h downtime  similarity: 0.94
2. SHAFT_MISALIGN   2024-01-28  LATHE       2.1h downtime  similarity: 0.87
3. LUBRICATION_FAIL 2024-02-05  CNC_MILL    3.8h downtime  similarity: 0.82
```

These three are passed directly into the LLM prompt as context (Step 5 of the pipeline).

---

### Why fetch everything instead of filtering in SQL?

At ~241 patterns the full fetch is ~2.4 MB — negligible. There is no SQL function that
can compute cosine similarity against a comma-separated VARCHAR in Exasol without a UDF.
Pulling the data to Python and using numpy is faster to implement, easier to read, and
performs fine at this scale. If the pattern library grew to tens of thousands of rows, the
right move would be to store embeddings in a proper vector column and push the similarity
computation back into Exasol or a dedicated vector index.

---

## Where the data lives

| Table | What's in it | Written by |
|---|---|---|
| `AI_SCHEMA.FAILURE_PATTERNS` | One row per historical downtime event. Stores the plain-text `description` and the 768-float `embedding` (comma-separated VARCHAR), plus `machine_type`, `reason_code`, `downtime_hrs`. | `setup_ai_tables.py` |
| `AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS` | One row per at-risk machine per day. `root_cause`, `recommended_action`, `estimated_hours_to_failure`, `confidence`, `similar_pattern_ids`. Today's row is replaced on each re-run. | `factory_ai_agent.py` |
| `MARTS.MART_AI_MAINTENANCE_QUEUE` | Joins the above with OEE trends and sensor health. Adds `urgency_tier`. | dbt (`mart_ai_maintenance_queue.sql`) |

---

## Urgency tiers

The mart computes urgency from `estimated_hours_to_failure`:

| Tier | Condition |
|---|---|
| CRITICAL | ≤ 8 hours |
| HIGH | ≤ 24 hours |
| MEDIUM | ≤ 72 hours |
| LOW | > 72 hours |

---

## Why this architecture is interesting

**Exasol as a vector store.**
The embeddings live in the same database as all the operational data. When a new failure
event is stored, the embedding goes in alongside it. No separate vector database. No sync
job. No extra infrastructure. The SQL to inspect stored embeddings is:

```sql
SELECT pattern_id, machine_type, reason_code, downtime_hrs, description
FROM AI_SCHEMA.FAILURE_PATTERNS
ORDER BY pattern_id;
```

**100% local AI.**
Both `nomic-embed-text` and `qwen2.5:7b` run in Docker via Ollama. No data leaves the
machine. This matters in manufacturing where factory data is often security-sensitive, and
where production networks may not have internet access.

**AI output in dbt lineage.**
`mart_ai_maintenance_queue` is a real dbt model — not a view created by the agent, not a
separate reporting table. It appears in the dbt lineage graph alongside `mart_oee_daily`
and `mart_machine_health`. AI recommendations are first-class in the data pipeline.

**Closed feedback loop.**
Every step is observable and re-runnable:

```
IoT data (Exasol)
    → dbt builds OEE metrics
    → AI agent reads metrics, embeds, retrieves, reasons
    → AI agent writes recommendations back to Exasol
    → dbt mart joins everything into a single queue
    → dashboard reads the mart
```
