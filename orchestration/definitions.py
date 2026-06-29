"""Dagster Definitions — wires together assets, job, sensor, and resources."""

from dagster import AssetSelection, Definitions, define_asset_job, load_assets_from_modules
from dagster_dbt import DbtCliResource

from . import assets
from .assets import DBT_CMD, DBT_PROFILES_DIR, DBT_PROJECT_DIR
from .sensors import build_new_data_sensor

# ── collect all assets ────────────────────────────────────────────────────────

all_assets = load_assets_from_modules([assets])

# ── job: run the full pipeline whenever triggered ────────────────────────────

manufacturing_refresh_job = define_asset_job(
    name="manufacturing_refresh_job",
    selection=AssetSelection.all(),
    description=(
        "Full pipeline: dbt (11 models) → AI vector re-seed → "
        "AI agent (Ollama) → mart_ai_maintenance_queue rebuild."
    ),
    tags={"dagster/max_concurrent_runs": "1"},
)

# ── sensor: triggers the job when new sensor rows arrive ─────────────────────

new_sensor_data_sensor = build_new_data_sensor(manufacturing_refresh_job)

# ── definitions ───────────────────────────────────────────────────────────────

defs = Definitions(
    assets=all_assets,
    jobs=[manufacturing_refresh_job],
    sensors=[new_sensor_data_sensor],
    resources={
        "dbt": DbtCliResource(
            project_dir=str(DBT_PROJECT_DIR),
            profiles_dir=str(DBT_PROFILES_DIR),
            dbt_executable=DBT_CMD,
        ),
    },
)
