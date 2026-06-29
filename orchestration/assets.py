"""Dagster asset definitions for the manufacturing OEE + AI pipeline.

Asset execution order (enforced by deps):
  core_dbt_assets (11 models)
    └── ai_tables           (re-seeds failure-pattern vectors)
          └── ai_recommendations   (factory AI agent → writes to AI_SCHEMA)
                └── mart_ai_maintenance_queue_refresh  (final dbt mart)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dagster import AssetExecutionContext, AssetKey, asset
from dagster_dbt import DbtCliResource, dbt_assets

PROJECT_ROOT    = Path(__file__).parent.parent
DBT_PROJECT_DIR = PROJECT_ROOT / "dbt_project"
DBT_PROFILES_DIR = Path.home() / ".dbt"
DBT_MANIFEST    = DBT_PROJECT_DIR / "target" / "manifest.json"


# ── dbt executable resolution ──────────────────────────────────────────────────

def _find_dbt() -> str:
    if cmd := os.environ.get("DBT_CMD"):
        return cmd
    # Prefer pip-installed dbt-core (has exasol adapter) over dbt-fusion on PATH
    win_pip_path = (
        Path.home()
        / "AppData/Local/Packages"
        / "PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"
        / "LocalCache/local-packages/Python312/Scripts/dbt.exe"
    )
    if win_pip_path.exists():
        return str(win_pip_path)
    if found := shutil.which("dbt"):
        return found
    return "dbt"


DBT_CMD = _find_dbt()


def _ensure_manifest() -> None:
    """Generate dbt manifest.json if it does not exist yet."""
    if DBT_MANIFEST.exists():
        return
    base = [
        DBT_CMD,
        "--project-dir", str(DBT_PROJECT_DIR),
        "--profiles-dir", str(DBT_PROFILES_DIR),
    ]
    subprocess.run([*base, "deps"],  check=True)
    subprocess.run([*base, "parse"], check=True)


_ensure_manifest()


# ── dbt assets (11 core models, mart_ai_maintenance_queue excluded) ────────────

@dbt_assets(
    manifest=DBT_MANIFEST,
    exclude="mart_ai_maintenance_queue",
)
def core_dbt_assets(context: AssetExecutionContext, dbt: DbtCliResource):
    """Run all dbt models except mart_ai_maintenance_queue.
    That mart is rebuilt after the AI agent writes its recommendations."""
    yield from dbt.cli(["run"], context=context).stream()


# ── AI layer assets ───────────────────────────────────────────────────────────

@asset(
    deps=[AssetKey("mart_machine_health"), AssetKey("mart_oee_daily")],
    group_name="ai_layer",
    description=(
        "Re-seed AI_SCHEMA.FAILURE_PATTERNS with 6-dimensional feature vectors "
        "built from the latest mart data. Exasol acts as the vector store."
    ),
)
def ai_tables(context: AssetExecutionContext) -> None:
    _run_script(context, PROJECT_ROOT / "scripts" / "setup_ai_tables.py")


@asset(
    deps=[AssetKey("ai_tables")],
    group_name="ai_layer",
    description=(
        "Factory AI Agent: detect at-risk machines → cosine similarity search "
        "inside Exasol → Ollama LLM root-cause analysis → write recommendations."
    ),
)
def ai_recommendations(context: AssetExecutionContext) -> None:
    _run_script(context, PROJECT_ROOT / "scripts" / "factory_ai_agent.py")


@asset(
    deps=[AssetKey("ai_recommendations")],
    group_name="ai_layer",
    description=(
        "Rebuild mart_ai_maintenance_queue so it reflects the latest AI "
        "recommendations written by the factory AI agent."
    ),
)
def mart_ai_maintenance_queue_refresh(context: AssetExecutionContext) -> None:
    result = subprocess.run(
        [
            DBT_CMD, "run",
            "--select",       "mart_ai_maintenance_queue",
            "--profiles-dir", str(DBT_PROFILES_DIR),
            "--project-dir",  str(DBT_PROJECT_DIR),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    context.log.info(result.stdout)
    if result.returncode != 0:
        raise Exception(result.stderr)


# ── shared helper ─────────────────────────────────────────────────────────────

def _run_script(context: AssetExecutionContext, script: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    context.log.info(result.stdout)
    if result.returncode != 0:
        raise Exception(f"{script.name} failed:\n{result.stderr}")
