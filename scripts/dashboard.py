"""
dashboard.py — Pipeline Runner (home page).
Multi-page Streamlit app for the Exasol + dbt Manufacturing OEE demo.

Run:  streamlit run scripts/dashboard.py
  or: .\\run.ps1 dashboard
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pyexasol
import requests
import streamlit as st

ROOT    = Path(__file__).parent.parent
DBT_DIR = ROOT / "dbt_project"
SCRIPTS = ROOT / "scripts"

st.set_page_config(page_title="Factory Demo", page_icon="🏭", layout="wide")

# ── Exasol helpers ────────────────────────────────────────────────────────────
EXA_DSN  = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER = os.environ.get("EXASOL_USER", "sys")
EXA_PASS = os.environ.get("EXASOL_PASSWORD", "exasol")


def _scalar(sql: str):
    try:
        con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASS,
                               websocket_sslopt={"cert_reqs": 0}, connection_timeout=3)
        val = con.execute(sql).fetchval()
        con.close()
        return val
    except Exception:
        return None


def find_dbt() -> str:
    if e := os.environ.get("DBT_CMD"):
        return e
    if f := shutil.which("dbt"):
        return f
    return "dbt"


# ── Service status ────────────────────────────────────────────────────────────
def service_status() -> dict:
    # Exasol
    try:
        con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASS,
                               websocket_sslopt={"cert_reqs": 0}, connection_timeout=3)
        con.close()
        exa = (True, "localhost:8563")
    except Exception:
        exa = (False, "Not reachable")

    # Ollama
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        models = [m["name"] for m in r.json().get("models", [])]
        oll = (True, f"{len(models)} model(s) loaded" if models else "Running — no models yet")
    except Exception:
        oll = (False, "Not reachable")

    # PostgreSQL (via Exasol Virtual Schema)
    if exa[0]:
        v = _scalar("SELECT COUNT(*) FROM ERP_PG.MACHINES")
        pg = (v is not None, f"Virtual Schema OK ({v} machines)" if v is not None else "Virtual Schema not ready")
    else:
        pg = (False, "Exasol not connected")

    return {"Exasol": exa, "PostgreSQL": pg, "Ollama": oll}


# ── Pipeline state (read from Exasol) ────────────────────────────────────────
def pipeline_state() -> dict:
    counts = {
        "seed":     _scalar("SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS"),
        "dbt":      _scalar("SELECT COUNT(*) FROM MARTS.MART_OEE_DAILY"),
        "ai_setup": _scalar("SELECT COUNT(*) FROM AI_SCHEMA.FAILURE_PATTERNS"),
        "ai_agent": _scalar("SELECT COUNT(*) FROM AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS"),
    }
    done = {k: (int(v) > 0 if v is not None else False) for k, v in counts.items()}
    done["counts"] = counts
    return done


# ── Subprocess streaming ──────────────────────────────────────────────────────
def run_step(cmd: list, cwd=None) -> tuple[int, str]:
    """Stream subprocess stdout into a live st.code block. Returns (rc, full_output)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=cwd, text=True, bufsize=1, env=os.environ.copy(),
    )
    lines: list[str] = []
    placeholder = st.empty()
    for line in proc.stdout:
        lines.append(line.rstrip())
        placeholder.code("\n".join(lines[-200:]), language="text")
    proc.wait()
    return proc.returncode, "\n".join(lines)


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("# 🏭 Factory Demo — Pipeline Runner")
st.caption(
    "Run each step in order. Once data is loaded, explore the analytics in the sidebar pages."
)
st.divider()

# ── Services ──────────────────────────────────────────────────────────────────
st.subheader("Services")

col_refresh = st.columns([6, 1])[1]
if col_refresh.button("↻ Refresh status", use_container_width=True):
    st.rerun()

svc   = service_status()
cols  = st.columns(3)
for i, (name, (ok, msg)) in enumerate(svc.items()):
    border = "#22c55e" if ok else "#ef4444"
    icon   = "🟢" if ok else "🔴"
    cols[i].markdown(
        f"<div style='border:1px solid {border}44;border-radius:8px;padding:12px 16px'>"
        f"<div style='font-weight:600'>{icon}&nbsp; {name}</div>"
        f"<div style='font-size:0.8rem;color:#94a3b8;margin-top:4px'>{msg}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

st.divider()

# ── Pipeline steps ────────────────────────────────────────────────────────────
st.subheader("Pipeline Steps")

state  = pipeline_state()
counts = state["counts"]

STEPS = [
    {
        "key":        "containers",
        "label":      "Step 1 — Start containers",
        "desc":       "Starts Exasol, PostgreSQL, and Ollama via Docker Compose",
        "done":       svc["Exasol"][0],
        "done_label": "All services running",
        "run":        lambda: run_step(["docker", "compose", "up", "-d"], cwd=str(ROOT)),
    },
    {
        "key":        "seed",
        "label":      "Step 2 — Seed data",
        "desc":       "Creates schemas, uploads Virtual Schema adapter, loads ~260 k IoT rows + ERP data",
        "done":       state["seed"],
        "done_label": f"{int(counts['seed']):,} sensor rows" if counts["seed"] else "Not run",
        "run":        lambda: run_step([sys.executable, str(SCRIPTS / "setup_exasol.py")], cwd=str(ROOT)),
    },
    {
        "key":        "dbt",
        "label":      "Step 3 — Run dbt models",
        "desc":       "Builds all 12 models: staging → intermediate → marts  (~25 sec)",
        "done":       state["dbt"],
        "done_label": f"{int(counts['dbt']):,} OEE rows" if counts["dbt"] else "Not run",
        "run":        None,  # handled specially below
    },
    {
        "key":        "ai_setup",
        "label":      "Step 4 — Set up AI layer",
        "desc":       "Pulls both Ollama models (~4.3 GB, first run only) then embeds 241 failure events via nomic-embed-text",
        "done":       state["ai_setup"],
        "done_label": f"{int(counts['ai_setup']):,} failure patterns" if counts["ai_setup"] else "Not run",
        "run":        None,  # handled specially below
    },
    {
        "key":        "ai_agent",
        "label":      "Step 5 — Run AI agent",
        "desc":       "Finds at-risk machines → nomic-embed-text similarity search → qwen2.5:7b → recommendations  (~5–15 min)",
        "done":       state["ai_agent"],
        "done_label": f"{int(counts['ai_agent'])} recommendations" if counts["ai_agent"] else "Not run",
        "run":        lambda: run_step([sys.executable, str(SCRIPTS / "factory_ai_agent.py")], cwd=str(ROOT)),
    },
]

if "last_output" not in st.session_state:
    st.session_state.last_output = {}

for step in STEPS:
    key  = step["key"]
    done = step["done"]
    badge = f"✅ {step['done_label']}" if done else "⚪ Not run"

    col_info, col_btn = st.columns([5, 1])
    with col_info:
        st.markdown(f"**{step['label']}**")
        st.caption(f"{step['desc']}  ·  {badge}")
    clicked = col_btn.button("▶ Run" if not done else "↺ Re-run", key=f"btn_{key}",
                             use_container_width=True)

    # Run logic and output are outside the column context → full page width
    if clicked:
        st.markdown(f"**▶ {step['label']}**")

        rc = 0
        if key == "dbt":
            dbt = find_dbt()
            rc, out1 = run_step([dbt, "deps"], cwd=str(DBT_DIR))
            if rc == 0:
                rc, out2 = run_step([dbt, "run"], cwd=str(DBT_DIR))
                out = out1 + "\n" + out2
            else:
                out = out1
        elif key == "ai_setup":
            rc, out1 = run_step(
                [sys.executable, str(SCRIPTS / "pull_ollama_model.py")], cwd=str(ROOT))
            if rc == 0:
                rc, out2 = run_step(
                    [sys.executable, str(SCRIPTS / "setup_ai_tables.py")], cwd=str(ROOT))
                out = out1 + "\n" + out2
            else:
                out = out1
        else:
            rc, out = step["run"]()

        st.session_state.last_output[key] = (rc, out)

        if rc == 0:
            st.success(f"✅ {step['label']} completed.")
        else:
            st.error(f"❌ {step['label']} failed (exit code {rc}).")

    # Show previous output if available
    if key in st.session_state.last_output:
        rc_prev, out_prev = st.session_state.last_output[key]
        label = f"{'✅' if rc_prev == 0 else '❌'} Last run output ({len(out_prev.splitlines())} lines)"
        with st.expander(label, expanded=False):
            st.code(out_prev[-6000:] if len(out_prev) > 6000 else out_prev, language="text")

    st.divider()

st.caption(
    "📊 OEE Overview  ·  🌡️ Machine Health  ·  🤖 AI Queue  ·  📦 Production  — navigate via the sidebar"
)
