"""Dagster sensor: trigger the manufacturing refresh pipeline when new IoT rows arrive."""

import os

import pyexasol
from dagster import RunRequest, SensorEvaluationContext, SkipReason, sensor

EXA_DSN      = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER     = os.environ.get("EXASOL_USER", "sys")
EXA_PASSWORD = os.environ.get("EXASOL_PASSWORD", "exasol")


def _sensor_reading_count() -> int:
    con = pyexasol.connect(
        dsn=EXA_DSN,
        user=EXA_USER,
        password=EXA_PASSWORD,
        websocket_sslopt={"cert_reqs": 0},
    )
    count = con.execute("SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS").fetchval()
    con.close()
    return int(count)


def build_new_data_sensor(job):
    """Factory so definitions.py can pass the job without a circular import."""

    @sensor(
        job=job,
        minimum_interval_seconds=60,
        description=(
            "Polls IOT_RAW.SENSOR_READINGS every 60 s. "
            "Triggers a full pipeline run whenever the row count increases."
        ),
    )
    def new_sensor_data_sensor(context: SensorEvaluationContext):
        try:
            current_count = _sensor_reading_count()
        except Exception as exc:
            yield SkipReason(f"Cannot reach Exasol: {exc}")
            return

        last_count = int(context.cursor or 0)

        if current_count > last_count:
            new_rows = current_count - last_count
            context.update_cursor(str(current_count))
            context.log.info(f"New rows detected: {new_rows} (total {current_count}). Triggering pipeline.")
            yield RunRequest(run_key=str(current_count))
        else:
            yield SkipReason(f"No new rows (count={current_count}).")

    return new_sensor_data_sensor
