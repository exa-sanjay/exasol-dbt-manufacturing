"""Self-Healing Factory AI Agent

Pipeline per run:
  1. Query Exasol for at-risk machines (anomaly flag OR declining 7-day OEE trend).
  2. Build a 6D feature vector for each machine's current sensor state.
  3. Run cosine similarity SQL against AI_SCHEMA.FAILURE_PATTERNS (Exasol as vector store)
     to find the top-3 most similar historical failure incidents.
  4. Call the local Ollama LLM (qwen2.5:0.5b) with machine context + similar failures
     and ask for a JSON root cause analysis + maintenance recommendation.
  5. INSERT the structured result into AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS.

Everything runs locally — no cloud API, no API key required.
"""

import json
import math
import os
import sys
import time

# Force UTF-8 output on Windows so box-drawing / degree symbols don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pyexasol
import requests

# --Config ────────────────────────────────────────────────────────────────────
EXA_DSN      = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER     = os.environ.get("EXASOL_USER", "sys")
EXA_PASSWORD = os.environ.get("EXASOL_PASSWORD", "exasol")
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = "qwen2.5:7b"

MACHINE_TYPE_CODES = {
    "CNC_MILL": 1, "LATHE": 2, "WELDING_ROBOT": 3,
    "ASSEMBLY_BOT": 4, "INJECTION_MOLD": 5, "QUALITY_SCANNER": 6,
}


# --Step 1: Find at-risk machines ─────────────────────────────────────────────

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
            -- Aggregate over the 7 days up to max(reading_date) so live inserts
            -- don't push historical anomalies out of scope
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


# --Step 2: Build feature vector ──────────────────────────────────────────────

def compute_machine_baselines(con):
    rows = con.execute("""
        SELECT machine_id,
               AVG(temperature_c)     AS mean_temp,  STDDEV_POP(temperature_c)     AS std_temp,
               AVG(vibration_mm_s)    AS mean_vib,   STDDEV_POP(vibration_mm_s)    AS std_vib,
               AVG(power_kw)          AS mean_pwr,   STDDEV_POP(power_kw)          AS std_pwr
        FROM IOT_RAW.SENSOR_READINGS
        GROUP BY machine_id
    """).fetchall()
    rows = [{k.lower(): v for k, v in r.items()} for r in rows]
    return {r["machine_id"]: r for r in rows}


def safe_z(value, mean, std):
    if std is None or float(std) == 0:
        return 0.0
    return (float(value) - float(mean)) / float(std)


def build_query_vector(machine, baselines):
    mid  = machine["machine_id"]
    bl   = baselines.get(mid, {})
    mtype = (machine["machine_type"] or "").upper().strip()

    v1 = float(MACHINE_TYPE_CODES.get(mtype, 0))
    v2 = safe_z(machine["max_temp_c"],        bl.get("mean_temp"), bl.get("std_temp"))
    v3 = safe_z(machine["max_vibration_mm_s"],bl.get("mean_vib"),  bl.get("std_vib"))
    v4 = safe_z(machine["max_power_kw"],      bl.get("mean_pwr"),  bl.get("std_pwr"))
    v5 = max(0.0, 1.0 - float(machine["oee_last_7d"] or 0.7))
    v6 = 1.0  # placeholder — current event duration unknown, use neutral value

    norm = math.sqrt(v1**2 + v2**2 + v3**2 + v4**2 + v5**2 + v6**2)
    return (v1, v2, v3, v4, v5, v6, norm if norm > 0 else 1.0)


# --Step 3: Cosine similarity search in Exasol ────────────────────────────────

def find_similar_failures(con, query_vector):
    """Run cosine similarity SQL inside Exasol against FAILURE_PATTERNS.

    The entire vector math runs inside Exasol — no data leaves the database.
    This is the 'Exasol as vector store' demo moment.
    """
    q1, q2, q3, q4, q5, q6, qnorm = [float(v) for v in query_vector]

    sql = f"""
        SELECT
            pattern_id,
            machine_type,
            CAST(event_date AS VARCHAR(20)) AS event_date,
            reason_code,
            v_downtime_hrs,
            CAST(
                ({q1} * v_machine_type +
                 {q2} * v_temp_zscore  +
                 {q3} * v_vib_zscore   +
                 {q4} * v_pwr_zscore   +
                 {q5} * v_oee_drop     +
                 {q6} * v_downtime_hrs)
                / NULLIF({qnorm} * vector_norm, 0)
            AS DECIMAL(8,6)) AS similarity
        FROM AI_SCHEMA.FAILURE_PATTERNS
        ORDER BY similarity DESC
        LIMIT 3
    """
    rows = con.execute(sql).fetchall()
    return [{k.lower(): v for k, v in r.items()} for r in rows]


# --Step 4: Call local Ollama LLM ─────────────────────────────────────────────

def call_ollama(machine, similar_failures, anomaly_score):
    failures_text = ""
    for i, f in enumerate(similar_failures, 1):
        sim_pct = float(f["similarity"] or 0) * 100
        failures_text += (
            f"  {i}. {f['reason_code']} on {f['event_date']} "
            f"({f['machine_type']}, {float(f['v_downtime_hrs']):.1f}h downtime, "
            f"{sim_pct:.0f}% pattern match)\n"
        )

    prompt = f"""You are a predictive maintenance AI for a smart factory. Analyze the following machine data and respond with a JSON object only — no markdown, no explanation, just the JSON.

Machine: {machine['machine_name']} ({machine['machine_type']}, {machine['production_line']})
Current sensor anomaly score: {float(anomaly_score):.3f}
7-day OEE trend: {float(machine['oee_last_7d']):.1%} (was {float(machine['oee_prior_7d']):.1%})
Max temperature: {float(machine['max_temp_c']):.1f}°C (daily avg: {float(machine['avg_temp_c']):.1f}°C)
Max vibration: {float(machine['max_vibration_mm_s']):.3f} mm/s (daily avg: {float(machine['avg_vibration_mm_s']):.3f} mm/s)
Max power: {float(machine['max_power_kw']):.2f} kW (daily avg: {float(machine['avg_power_kw']):.2f} kW)

Top 3 most similar historical failures (from Exasol vector similarity search):
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
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 200},
    }

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "{}")
        result = json.loads(raw)

        # Validate and clamp
        result["estimated_hours_to_failure"] = max(1.0, min(720.0,
            float(result.get("estimated_hours_to_failure", 48))))
        if result.get("confidence") not in ("HIGH", "MEDIUM", "LOW"):
            result["confidence"] = "MEDIUM"
        return result

    except requests.exceptions.ConnectionError:
        print(f"    WARNING: Cannot connect to Ollama at {OLLAMA_URL}. Is it running?")
        return _fallback_recommendation(machine, similar_failures)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"    WARNING: Ollama response parse error ({e}). Using fallback.")
        return _fallback_recommendation(machine, similar_failures)


def _fallback_recommendation(machine, similar_failures):
    """Rule-based fallback when Ollama is unavailable — still useful output."""
    top = similar_failures[0] if similar_failures else {}
    reason = top.get("reason_code", "UNKNOWN") if top else "UNKNOWN"
    return {
        "root_cause": f"Pattern matches historical {reason.replace('_', ' ').lower()} events.",
        "recommended_action": "Schedule immediate inspection of mechanical components.",
        "estimated_hours_to_failure": float(top.get("v_downtime_hrs", 24)) * 2 if top else 24.0,
        "confidence": "LOW",
    }


# --Step 5: Persist recommendation ────────────────────────────────────────────

def _sql_str(s):
    return "'" + str(s or "").replace("'", "''") + "'"


def insert_recommendation(con, machine, anomaly_score, result, similar_failures):
    pattern_ids = [str(f["pattern_id"]) for f in similar_failures]
    similar_ids_json = "[" + ",".join(pattern_ids) + "]"

    con.execute(f"""
        INSERT INTO AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS
            (machine_id, machine_name, anomaly_score, root_cause,
             recommended_action, estimated_hours_to_failure, confidence, similar_pattern_ids)
        VALUES (
            {int(machine["machine_id"])},
            {_sql_str(machine["machine_name"])},
            {round(float(anomaly_score), 4)},
            {_sql_str(result.get("root_cause", "")[:1000])},
            {_sql_str(result.get("recommended_action", "")[:1000])},
            {float(result.get("estimated_hours_to_failure", 48.0))},
            {_sql_str(result.get("confidence", "MEDIUM"))},
            {_sql_str(similar_ids_json)}
        )
    """)


# --Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n==> Factory AI Agent starting ...")
    print(f"    Exasol:  {EXA_DSN}")
    print(f"    Ollama:  {OLLAMA_URL}  (model: {OLLAMA_MODEL})")

    try:
        con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASSWORD,
                               websocket_sslopt={"cert_reqs": 0}, fetch_dict=True)
    except Exception as e:
        print(f"\nERROR: Cannot connect to Exasol: {e}")
        sys.exit(1)

    # Step 1
    print("\n--Step 1: Scanning for at-risk machines ...")
    try:
        machines = get_at_risk_machines(con)
    except Exception as e:
        print(f"ERROR: {e}")
        print("       Ensure 'make dbt-run' and 'make ai-setup' have both completed.")
        con.close()
        sys.exit(1)

    if not machines:
        print("    No at-risk machines found — all systems nominal.")
        con.close()
        return

    print(f"    Found {len(machines)} at-risk machine(s):")
    for m in machines:
        flag = "ANOMALY" if m["anomaly_flag"] else "DECLINING OEE"
        print(f"      - {m['machine_name']} ({flag}, OEE 7d: {float(m['oee_last_7d']):.1%})")

    # Compute baselines once
    print("\n--Step 2: Computing sensor baselines ...")
    baselines = compute_machine_baselines(con)

    # Process each machine
    recs_written = 0
    for machine in machines:
        name = machine["machine_name"]
        print(f"\n-- Processing: {name} " + "-" * 40)

        # Step 2: build vector
        qvec = build_query_vector(machine, baselines)
        print(f"    Feature vector: [{', '.join(f'{v:.3f}' for v in qvec[:6])}]  norm={qvec[6]:.3f}")

        # Step 3: similarity search inside Exasol
        print("    Running cosine similarity search in Exasol ...")
        similar = find_similar_failures(con, qvec)
        if similar:
            print(f"    Top match: {similar[0]['reason_code']} "
                  f"({float(similar[0]['similarity'] or 0)*100:.0f}% similar)")
        else:
            print("    No similar patterns found in vector store.")

        # Step 4: call Ollama
        print(f"    Calling Ollama ({OLLAMA_MODEL}) for root cause analysis ...")
        t0 = time.time()
        try:
            result = call_ollama(machine, similar, machine["anomaly_score"])
        except Exception as e:
            print(f"    WARNING: Ollama failed for {machine['machine_name']}: {e}")
            print("    Skipping this machine.")
            continue
        elapsed = time.time() - t0
        print(f"    Ollama response in {elapsed:.1f}s:")
        print(f"      Root cause:   {result['root_cause']}")
        print(f"      Action:       {result['recommended_action']}")
        print(f"      Est. TTF:     {result['estimated_hours_to_failure']:.0f}h  [{result['confidence']}]")

        # Step 5: persist
        insert_recommendation(con, machine, machine["anomaly_score"], result, similar)
        recs_written += 1

    con.close()

    print(f"\n==> AI Agent complete — {recs_written} recommendation(s) written to "
          "AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS")
    print("\n    Query the results:")
    print("    SELECT machine_name, estimated_hours_to_failure, recommended_action, confidence")
    print("    FROM MARTS.MART_AI_MAINTENANCE_QUEUE")
    print("    ORDER BY estimated_hours_to_failure ASC;\n")


if __name__ == "__main__":
    main()
