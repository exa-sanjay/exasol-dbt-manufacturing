"""Self-Healing Factory AI Agent

Pipeline per run:
  1. Query Exasol for at-risk machines (anomaly flag OR declining 7-day OEE trend).
  2. Build a natural-language description of each machine's current sensor state.
  3. Call Ollama (nomic-embed-text) to embed that description into a 768-dimensional vector.
  4. Fetch all stored failure pattern embeddings from AI_SCHEMA.FAILURE_PATTERNS (Exasol),
     rank by cosine similarity in Python (numpy), return the top-3 most similar events.
  5. Call the local Ollama LLM (qwen2.5:7b) with machine context + similar failures
     and ask for a JSON root cause analysis + maintenance recommendation.
  6. INSERT the structured result into AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS.

Everything runs locally — no cloud API, no API key required.
"""

import json
import os
import sys
import time

# Force UTF-8 output on Windows so box-drawing / degree symbols don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pyexasol
import requests

from ai_constants import EMBED_MODEL

# ── Config ───────────────────────────────────────────────────────────────────
EXA_DSN      = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER     = os.environ.get("EXASOL_USER", "sys")
EXA_PASSWORD = os.environ.get("EXASOL_PASSWORD", "exasol")
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = "qwen2.5:7b"


# ── Step 1: Find at-risk machines ────────────────────────────────────────────

def get_at_risk_machines(con):
    """Return machines with an active anomaly OR declining 7-day OEE trend."""
    rows = con.execute("""
        WITH oee_max_date AS (
            SELECT machine_id, MAX(shift_date) AS max_date
            FROM MARTS.MART_OEE_DAILY
            GROUP BY machine_id
        ),
        oee_window AS (
            SELECT
                o.machine_id,
                AVG(CASE WHEN o.shift_date >= ADD_DAYS(d.max_date, -7)
                         THEN o.oee END)  AS oee_last_7d,
                AVG(CASE WHEN o.shift_date >= ADD_DAYS(d.max_date, -14)
                          AND o.shift_date <  ADD_DAYS(d.max_date, -7)
                         THEN o.oee END)  AS oee_prior_7d
            FROM MARTS.MART_OEE_DAILY o
            JOIN oee_max_date d ON o.machine_id = d.machine_id
            GROUP BY o.machine_id
        ),
        health_window AS (
            SELECT
                machine_id,
                MAX(reading_date)         AS reading_date,
                MAX(anomaly_flag)         AS anomaly_flag,
                AVG(avg_temp_c)           AS avg_temp_c,
                MAX(max_temp_c)           AS max_temp_c,
                AVG(avg_vibration_mm_s)   AS avg_vibration_mm_s,
                MAX(max_vibration_mm_s)   AS max_vibration_mm_s,
                AVG(avg_power_kw)         AS avg_power_kw,
                MAX(max_power_kw)         AS max_power_kw
            FROM MARTS.MART_MACHINE_HEALTH
            WHERE reading_date >= ADD_DAYS(
                (SELECT MAX(reading_date) FROM MARTS.MART_MACHINE_HEALTH), -7)
            GROUP BY machine_id
        )
        SELECT
            h.machine_id,
            m.machine_name,
            m.machine_type,
            m.production_line,
            h.reading_date,
            h.anomaly_flag,
            h.avg_temp_c,
            h.max_temp_c,
            h.avg_vibration_mm_s,
            h.max_vibration_mm_s,
            h.avg_power_kw,
            h.max_power_kw,
            COALESCE(w.oee_last_7d,  0) AS oee_last_7d,
            COALESCE(w.oee_prior_7d, 0) AS oee_prior_7d,
            GREATEST(
                CASE WHEN h.avg_temp_c > 0
                     THEN ABS(h.max_temp_c - h.avg_temp_c) / NULLIF(h.avg_temp_c, 0) ELSE 0 END,
                CASE WHEN h.avg_vibration_mm_s > 0
                     THEN ABS(h.max_vibration_mm_s - h.avg_vibration_mm_s) / NULLIF(h.avg_vibration_mm_s, 0) ELSE 0 END
            ) AS anomaly_score
        FROM health_window h
        JOIN ERP_PG.MACHINES m ON h.machine_id = m.machine_id
        LEFT JOIN oee_window w  ON h.machine_id = w.machine_id
        WHERE h.anomaly_flag = TRUE
           OR (w.oee_last_7d IS NOT NULL AND w.oee_prior_7d IS NOT NULL
               AND w.oee_last_7d < w.oee_prior_7d - 0.03)
        ORDER BY anomaly_score DESC
    """).fetchall()
    return [{k.lower(): v for k, v in r.items()} for r in rows]


# ── Step 2 + 3: Build description and embed ──────────────────────────────────

def _f(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def build_machine_description(machine: dict) -> str:
    """Build a natural-language description of the current at-risk machine state."""
    mtype = (machine["machine_type"] or "UNKNOWN").upper().strip()
    return (
        f"Machine type: {mtype}\n"
        f"Current anomaly detected\n"
        f"Sensor readings (recent 7-day window):\n"
        f"  Temperature: max {_f(machine['max_temp_c']):.1f}°C, average {_f(machine['avg_temp_c']):.1f}°C\n"
        f"  Vibration: max {_f(machine['max_vibration_mm_s']):.3f} mm/s, average {_f(machine['avg_vibration_mm_s']):.3f} mm/s\n"
        f"  Power: max {_f(machine['max_power_kw']):.2f} kW, average {_f(machine['avg_power_kw']):.2f} kW\n"
        f"7-day OEE: {_f(machine['oee_last_7d']):.1%}"
    )


def get_embedding(text: str) -> list:
    """Call Ollama embedding API with nomic-embed-text, return list of floats."""
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]
    except requests.exceptions.ConnectionError:
        print(f"    WARNING: Cannot reach Ollama at {OLLAMA_URL} for embedding.")
        return []
    except Exception as e:
        print(f"    WARNING: Embedding call failed: {e}")
        return []


# ── Step 4: Cosine similarity search ────────────────────────────────────────

def find_similar_failures(con, query_embedding: list) -> list:
    """Fetch all stored embeddings from Exasol, rank by cosine similarity in Python.

    Embeddings live in Exasol; similarity math runs in numpy on the host.
    At ~241 patterns × 768 dims the fetch is <1 MB, so no in-database UDF needed.
    """
    rows = con.execute("""
        SELECT pattern_id, machine_type,
               CAST(event_date AS VARCHAR(20)) AS event_date,
               reason_code, downtime_hrs, embedding
        FROM AI_SCHEMA.FAILURE_PATTERNS
    """).fetchall()

    if not rows or not query_embedding:
        return []

    q      = np.array(query_embedding, dtype=float)
    q_norm = float(np.linalg.norm(q))

    scored = []
    for row in rows:
        d        = {k.lower(): v for k, v in row.items()}
        emb      = np.fromstring(d["embedding"], dtype=float, sep=",")
        emb_norm = float(np.linalg.norm(emb))
        sim      = float(np.dot(q, emb) / (q_norm * emb_norm)) if q_norm > 0 and emb_norm > 0 else 0.0
        scored.append({**d, "similarity": sim})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:3]


# ── Step 5: Call local Ollama LLM ────────────────────────────────────────────

def call_ollama(machine, similar_failures, anomaly_score):
    failures_text = ""
    for i, f in enumerate(similar_failures, 1):
        sim_pct = f["similarity"] * 100
        failures_text += (
            f"  {i}. {f['reason_code']} on {f['event_date']} "
            f"({f['machine_type']}, {_f(f['downtime_hrs']):.1f}h downtime, "
            f"{sim_pct:.0f}% semantic match)\n"
        )

    prompt = f"""You are a predictive maintenance AI for a smart factory. Analyze the following machine data and respond with a JSON object only — no markdown, no explanation, just the JSON.

Machine: {machine['machine_name']} ({machine['machine_type']}, {machine['production_line']})
Current sensor anomaly score: {_f(anomaly_score):.3f}
7-day OEE trend: {_f(machine['oee_last_7d']):.1%} (was {_f(machine['oee_prior_7d']):.1%})
Max temperature: {_f(machine['max_temp_c']):.1f}°C (daily avg: {_f(machine['avg_temp_c']):.1f}°C)
Max vibration: {_f(machine['max_vibration_mm_s']):.3f} mm/s (daily avg: {_f(machine['avg_vibration_mm_s']):.3f} mm/s)
Max power: {_f(machine['max_power_kw']):.2f} kW (daily avg: {_f(machine['avg_power_kw']):.2f} kW)

Top 3 most similar historical failures (ranked by nomic-embed-text semantic similarity):
{failures_text if failures_text.strip() else '  No similar historical failures found.'}

Respond with this exact JSON structure:
{{
  "root_cause": "one sentence explaining the most likely root cause based on sensor patterns",
  "recommended_action": "one specific maintenance action the shift supervisor should take",
  "estimated_hours_to_failure": 24.0,
  "confidence": "HIGH"
}}

Rules:
- estimated_hours_to_failure must be a number between 1 and 720
- confidence must be exactly HIGH, MEDIUM, or LOW
- base your estimate on the similarity to past failures and current anomaly severity
"""

    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "format":  "json",
        "options": {"temperature": 0.1, "num_predict": 200},
    }

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()
        body   = resp.json()
        raw    = body.get("response", "{}")
        result = json.loads(raw)

        if not all(k in result for k in ("root_cause", "recommended_action",
                                          "estimated_hours_to_failure", "confidence")):
            raise ValueError(f"Missing required keys in LLM response: {list(result.keys())}")

        result["estimated_hours_to_failure"] = max(1.0, min(720.0,
            float(result["estimated_hours_to_failure"])))
        if result["confidence"] not in ("HIGH", "MEDIUM", "LOW"):
            result["confidence"] = "MEDIUM"
        return result

    except requests.exceptions.ConnectionError:
        print(f"    WARNING: Cannot connect to Ollama at {OLLAMA_URL}. Is it running?")
        return _fallback_recommendation(machine, similar_failures)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        print(f"    WARNING: Ollama response parse/validation error ({e}). Using fallback.")
        return _fallback_recommendation(machine, similar_failures)


def _fallback_recommendation(machine, similar_failures):
    """Rule-based fallback when Ollama is unavailable — still useful output."""
    top    = similar_failures[0] if similar_failures else {}
    reason = top.get("reason_code", "UNKNOWN") if top else "UNKNOWN"
    return {
        "root_cause":                f"Pattern matches historical {reason.replace('_', ' ').lower()} events.",
        "recommended_action":        "Schedule immediate inspection of mechanical components.",
        "estimated_hours_to_failure": _f(top.get("downtime_hrs", 24)) * 2 if top else 24.0,
        "confidence":                "LOW",
    }


# ── Step 6: Persist recommendation ───────────────────────────────────────────

def insert_recommendation(con, machine, anomaly_score, result, similar_failures):
    pattern_ids    = [str(f["pattern_id"]) for f in similar_failures]
    similar_ids_json = "[" + ",".join(pattern_ids) + "]"

    con.execute(
        "DELETE FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS "
        "WHERE machine_id = {machine_id} AND CAST(generated_at AS DATE) = CURRENT_DATE",
        {"machine_id": int(machine["machine_id"])},
    )

    con.execute(
        """
        INSERT INTO AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS
            (machine_id, machine_name, anomaly_score, root_cause,
             recommended_action, estimated_hours_to_failure, confidence, similar_pattern_ids)
        VALUES ({machine_id}, {machine_name}, {anomaly_score}, {root_cause},
                {recommended_action}, {estimated_hours_to_failure}, {confidence}, {similar_pattern_ids})
        """,
        {
            "machine_id":                 int(machine["machine_id"]),
            "machine_name":               str(machine["machine_name"] or "")[:100],
            "anomaly_score":              round(_f(anomaly_score), 4),
            "root_cause":                 str(result.get("root_cause", ""))[:1000],
            "recommended_action":         str(result.get("recommended_action", ""))[:1000],
            "estimated_hours_to_failure": float(result.get("estimated_hours_to_failure", 48.0)),
            "confidence":                 str(result.get("confidence", "MEDIUM")),
            "similar_pattern_ids":        similar_ids_json,
        },
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n==> Factory AI Agent starting ...")
    print(f"    Exasol:     {EXA_DSN}")
    print(f"    Ollama:     {OLLAMA_URL}")
    print(f"    Embed model: {EMBED_MODEL}")
    print(f"    LLM model:   {OLLAMA_MODEL}")

    try:
        con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASSWORD,
                               websocket_sslopt={"cert_reqs": 0}, fetch_dict=True)
    except Exception as e:
        print(f"\nERROR: Cannot connect to Exasol: {e}")
        sys.exit(1)

    recs_written = 0
    try:
        print("\n-- Step 1: Scanning for at-risk machines ...")
        try:
            machines = get_at_risk_machines(con)
        except Exception as e:
            print(f"ERROR: {e}")
            print("       Ensure 'make dbt-run' and 'make ai-setup' have both completed.")
            sys.exit(1)

        if not machines:
            print("    No at-risk machines found — all systems nominal.")
            return

        print(f"    Found {len(machines)} at-risk machine(s):")
        for m in machines:
            flag = "ANOMALY" if m["anomaly_flag"] else "DECLINING OEE"
            print(f"      - {m['machine_name']} ({flag}, OEE 7d: {_f(m['oee_last_7d']):.1%})")

        for machine in machines:
            name = machine["machine_name"]
            print(f"\n-- Processing: {name} " + "-" * 40)

            print(f"    Embedding current state via {EMBED_MODEL} ...")
            query_emb = get_embedding(build_machine_description(machine))
            if not query_emb:
                print(f"    ERROR: Could not get embedding for {name} — skipping.")
                continue

            print("    Searching failure patterns by cosine similarity ...")
            similar = find_similar_failures(con, query_emb)
            if similar:
                top = similar[0]
                print(f"    Top match: {top['reason_code']} "
                      f"({top['similarity']*100:.0f}% semantic similarity)")
            else:
                print("    No similar patterns found in vector store.")

            print(f"    Calling Ollama ({OLLAMA_MODEL}) for root cause analysis ...")
            t0 = time.time()
            try:
                result = call_ollama(machine, similar, machine["anomaly_score"])
            except Exception as e:
                print(f"    WARNING: Ollama failed for {name}: {e}")
                print("    Skipping this machine.")
                continue
            elapsed = time.time() - t0
            print(f"    Ollama response in {elapsed:.1f}s:")
            print(f"      Root cause:  {result['root_cause']}")
            print(f"      Action:      {result['recommended_action']}")
            print(f"      Est. TTF:    {result['estimated_hours_to_failure']:.0f}h  [{result['confidence']}]")

            insert_recommendation(con, machine, machine["anomaly_score"], result, similar)
            recs_written += 1

    finally:
        con.close()

    print(f"\n==> AI Agent complete — {recs_written} recommendation(s) written to "
          "AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS")
    print("\n    Query the results:")
    print("    SELECT machine_name, estimated_hours_to_failure, recommended_action, confidence")
    print("    FROM MARTS.MART_AI_MAINTENANCE_QUEUE")
    print("    ORDER BY estimated_hours_to_failure ASC;\n")


if __name__ == "__main__":
    main()
