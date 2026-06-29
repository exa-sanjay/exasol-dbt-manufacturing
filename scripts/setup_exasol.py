"""
setup_exasol.py
---------------
Sets up the Exasol side of the demo:
  1. Creates schemas: IOT_RAW, DBT_MFG
  2. Creates a JDBC connection to PostgreSQL
  3. Downloads the Exasol PostgreSQL Virtual Schema adapter and uploads to BucketFS
  4. Creates a Virtual Schema (ERP_PG) pointing to PostgreSQL
  5. Generates and loads synthetic IoT sensor data and downtime events

Connection settings default to the Docker Compose values. Override via env vars
(EXASOL_HOST, EXASOL_PORT, EXASOL_USER, EXASOL_PASSWORD, EXASOL_BUCKETFS_PORT,
POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD) or a local
scripts/settings.cfg file (see scripts/settings.cfg.template).
"""

import configparser
import math
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta

try:
    import pyexasol
    import requests
except ImportError:
    print("ERROR: Missing dependencies. Run: pip install pyexasol requests")
    sys.exit(1)

# ── Load settings.cfg ─────────────────────────────────────────────────────────

_cfg = configparser.ConfigParser()
_cfg_path = os.path.join(os.path.dirname(__file__), "settings.cfg")
if os.path.exists(_cfg_path):
    _cfg.read(_cfg_path)
    print(f"  Config loaded from {_cfg_path}")
else:
    print(f"  WARNING: {_cfg_path} not found — using defaults / env vars.")

def _get(section, key, env_var=None, default=""):
    """Read from env var first, then settings.cfg, then default."""
    if env_var and os.getenv(env_var):
        return os.getenv(env_var)
    return _cfg.get(section, key, fallback=default)

# ── Config ────────────────────────────────────────────────────────────────────

EXA_HOST = _get("exasol", "host",     "EXASOL_HOST",     "localhost")
EXA_PORT = _get("exasol", "port",     "EXASOL_PORT",     "8563")
EXA_DSN  = f"{EXA_HOST}:{EXA_PORT}"
EXA_USER = _get("exasol", "user",     "EXASOL_USER",     "sys")
EXA_PASS = _get("exasol", "password", "EXASOL_PASSWORD", "exasol")

BFS_HOST   = _get("bucketfs", "host",     "EXASOL_HOST",              "localhost")
BFS_PORT   = _get("bucketfs", "port",     "EXASOL_BUCKETFS_PORT",     "2581")
BFS_BUCKET = _get("bucketfs", "bucket",   "",                         "default")
BFS_USER   = _get("bucketfs", "username", "",                         "w")
BFS_PASS   = _get("bucketfs", "password", "EXASOL_BFS_WRITE_PASS",    "write")

_pg_host_raw = _get("postgres", "host", "POSTGRES_HOST", "_auto_detect_")
if _pg_host_raw == "_auto_detect_":
    import subprocess as _sp
    _r = _sp.run(
        ["docker", "inspect", "mfg_postgres",
         "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
        capture_output=True, text=True
    )
    _pg_host_raw = _r.stdout.strip() or "localhost"
    print(f"  Auto-detected PostgreSQL IP: {_pg_host_raw}")
PG_HOST = _pg_host_raw
PG_PORT = _get("postgres", "port",     "POSTGRES_PORT",     "5432")
PG_DB   = _get("postgres", "database", "POSTGRES_DB",       "manufacturing_erp")
PG_USER = _get("postgres", "user",     "POSTGRES_USER",     "erp_user")
PG_PASS = _get("postgres", "password", "POSTGRES_PASSWORD", "erp_password")

# ── JAR locations ─────────────────────────────────────────────────────────────
# https://github.com/exasol/postgresql-virtual-schema/releases/tag/4.0.0
ADAPTER_JAR = "virtual-schema-dist-14.0.2-postgresql-4.0.0.jar"
ADAPTER_URL = (
    "https://github.com/exasol/postgresql-virtual-schema/releases/download/"
    f"4.0.0/{ADAPTER_JAR}"
)
PG_JDBC_JAR = "postgresql-42.7.3.jar"
PG_JDBC_URL = f"https://jdbc.postgresql.org/download/{PG_JDBC_JAR}"

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"  {msg}", flush=True)


def download_jar(url: str, dest: str):
    log(f"Downloading {url.split('/')[-1]} ...")
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            f.write(chunk)
    log(f"  -> saved to {dest}")


def _detect_bfs_password() -> str:
    """Try to read the BucketFS write password from the running Docker container.
    Falls back to prompting the user if auto-detection fails."""
    import subprocess, re
    try:
        r = subprocess.run(
            ["docker", "exec", "mfg_exasol", "grep", "-m1", "WritePasswd", "/exa/etc/EXAConf"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            m = re.search(r"WritePasswd\s*=\s*(\S+)", r.stdout)
            if m:
                log("Auto-detected BucketFS write password from container.")
                return m.group(1)
    except Exception:
        pass

    if BFS_PASS != "write":
        return BFS_PASS

    print("\n[!] Could not auto-detect BucketFS password.")
    print("    Find it manually:  docker exec mfg_exasol grep WritePasswd /exa/etc/EXAConf")
    return input("    Enter BucketFS write password: ").strip()


def upload_to_bucketfs(local_path: str, remote_name: str, password: str):
    """Upload a file to BucketFS via HTTPS PUT (no external tools required)."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = f"https://{BFS_HOST}:{BFS_PORT}/{BFS_BUCKET}/{remote_name}"
    log(f"Uploading {remote_name} ...")
    with open(local_path, "rb") as fh:
        resp = requests.put(url, data=fh, auth=(BFS_USER, password), verify=False, timeout=120)
    if not resp.ok:
        raise RuntimeError(
            f"BucketFS upload failed ({resp.status_code}): {resp.text[:300]}\n"
            f"  URL: {url}\n"
            f"  Tip: run  docker exec mfg_exasol grep WritePasswd /exa/etc/EXAConf\n"
            f"       then set env var EXASOL_BFS_WRITE_PASS=<password> and retry."
        )
    log(f"  -> OK")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n==> Connecting to Exasol ...")
    con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASS,
                           websocket_sslopt={"cert_reqs": 0})
    print("    Connected.\n")

    # ── 1. Schemas ────────────────────────────────────────────────────────────
    print("==> Creating schemas ...")
    for schema in ("IOT_RAW", "DBT_MFG", "ADAPTER_SCRIPTS"):
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        log(f"Schema {schema} ready.")

    # ── 2. Download & upload adapter + JDBC driver ────────────────────────────
    print("\n==> Preparing Virtual Schema adapter ...")
    with tempfile.TemporaryDirectory() as tmpdir:
        adapter_path = os.path.join(tmpdir, ADAPTER_JAR)
        pg_jdbc_path = os.path.join(tmpdir, PG_JDBC_JAR)

        download_jar(ADAPTER_URL, adapter_path)
        download_jar(PG_JDBC_URL, pg_jdbc_path)

        # Write JDBC driver settings.cfg for ExaLoader registration
        cfg_path = os.path.join(tmpdir, "settings.cfg")
        with open(cfg_path, "w") as f:
            f.write(f"DRIVERNAME=POSTGRES_JDBC_DRIVER\n")
            f.write(f"JAR={PG_JDBC_JAR}\n")
            f.write(f"DRIVERMAIN=org.postgresql.Driver\n")
            f.write(f"PREFIX=jdbc:postgresql:\n")
            f.write(f"FETCHSIZE=100000\n")
            f.write(f"INSERTSIZE=-1\n")

        bfs_pw = _detect_bfs_password()
        upload_to_bucketfs(adapter_path, ADAPTER_JAR, bfs_pw)
        upload_to_bucketfs(pg_jdbc_path, f"drivers/jdbc/{PG_JDBC_JAR}", bfs_pw)
        upload_to_bucketfs(cfg_path, "drivers/jdbc/settings.cfg", bfs_pw)

    # ── 3. Create adapter script ───────────────────────────────────────────────
    print("\n==> Creating adapter script ...")
    con.execute("""
        CREATE OR REPLACE JAVA ADAPTER SCRIPT ADAPTER_SCRIPTS.JDBC_ADAPTER AS
            %scriptclass com.exasol.adapter.RequestDispatcher;
            %jar /buckets/bfsdefault/default/{adapter_jar};
            %jar /buckets/bfsdefault/default/drivers/jdbc/{pg_jdbc_jar};
/""".format(adapter_jar=ADAPTER_JAR, pg_jdbc_jar=PG_JDBC_JAR))
    log("Adapter script created.")

    # ── 4. PostgreSQL connection ───────────────────────────────────────────────
    print("\n==> Creating JDBC connection to PostgreSQL ...")
    con.execute(f"""
        CREATE OR REPLACE CONNECTION POSTGRES_CONN
        TO 'jdbc:postgresql://{PG_HOST}:{PG_PORT}/{PG_DB}'
        USER '{PG_USER}'
        IDENTIFIED BY '{PG_PASS}'
    """)
    log(f"Connection POSTGRES_CONN -> {PG_HOST}:{PG_PORT}/{PG_DB}")

    # ── 5. Virtual Schema ─────────────────────────────────────────────────────
    print("\n==> Creating Virtual Schema ERP_PG ...")
    con.execute("DROP VIRTUAL SCHEMA IF EXISTS ERP_PG CASCADE")
    con.execute("""
        CREATE VIRTUAL SCHEMA ERP_PG
        USING ADAPTER_SCRIPTS.JDBC_ADAPTER
        WITH
          CONNECTION_NAME = 'POSTGRES_CONN'
          CATALOG_NAME    = 'manufacturing_erp'
          SCHEMA_NAME     = 'public'
    """)
    # Smoke-test
    rows = con.execute("SELECT COUNT(*) FROM ERP_PG.MACHINES").fetchval()
    log(f"Virtual Schema ready — ERP_PG.MACHINES has {rows} rows.")

    # ── 6. IOT tables ─────────────────────────────────────────────────────────
    print("\n==> Creating IOT_RAW tables ...")
    con.execute("""
        CREATE OR REPLACE TABLE IOT_RAW.SENSOR_READINGS (
            machine_id       INT,
            ts               TIMESTAMP,
            temperature_c    DECIMAL(5,2),
            vibration_mm_s   DECIMAL(6,3),
            power_kw         DECIMAL(7,3)
        )
    """)
    con.execute("""
        CREATE OR REPLACE TABLE IOT_RAW.DOWNTIME_EVENTS (
            event_id    INT PRIMARY KEY,
            machine_id  INT,
            started_at  TIMESTAMP,
            ended_at    TIMESTAMP,
            reason_code VARCHAR(50)
        )
    """)
    log("IOT_RAW tables created.")

    # ── 7. Create AI_SCHEMA stub ──────────────────────────────────────────────
    # mart_ai_maintenance_queue.sql joins this table at dbt-run time.
    # It's empty until `ai-setup` + `ai-agent` run — that's fine.
    print("\n==> Creating AI_SCHEMA stub ...")
    con.execute("CREATE SCHEMA IF NOT EXISTS AI_SCHEMA")
    con.execute("""
        CREATE TABLE IF NOT EXISTS AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS (
            rec_id                     INT,
            machine_id                 INT,
            machine_name               VARCHAR(100),
            generated_at               TIMESTAMP,
            anomaly_score              DECIMAL(6,3),
            root_cause                 VARCHAR(500),
            recommended_action         VARCHAR(500),
            estimated_hours_to_failure DECIMAL(6,1),
            confidence                 VARCHAR(20),
            similar_pattern_ids        VARCHAR(200)
        )
    """)
    log("AI_SCHEMA.MAINTENANCE_RECOMMENDATIONS ready (empty until ai-setup + ai-agent).")

    # ── 9. Seed sensor readings ───────────────────────────────────────────────
    # 10 machines × 90 days × 288 readings/day (5-min intervals) ≈ 259 200 rows
    print("\n==> Seeding sensor readings (~260k rows) ...")

    machine_baselines = {
        1:  {"temp": 68.0, "vib": 1.20, "pwr": 18.5},
        2:  {"temp": 70.5, "vib": 1.35, "pwr": 19.2},
        3:  {"temp": 55.0, "vib": 0.85, "pwr": 12.0},
        4:  {"temp": 56.5, "vib": 0.90, "pwr": 12.8},
        5:  {"temp": 82.0, "vib": 2.10, "pwr": 35.0},
        6:  {"temp": 81.5, "vib": 2.05, "pwr": 34.5},
        7:  {"temp": 60.0, "vib": 1.00, "pwr": 22.0},
        8:  {"temp": 75.0, "vib": 0.60, "pwr": 45.0},
        9:  {"temp": 74.5, "vib": 0.65, "pwr": 44.8},
        10: {"temp": 40.0, "vib": 0.20, "pwr": 8.5},
    }
    reason_codes = [
        "UNPLANNED_BREAKDOWN", "MATERIAL_JAM", "TOOLING_FAILURE",
        "POWER_FLUCTUATION", "OPERATOR_ERROR"
    ]

    start_dt = datetime.now() - timedelta(days=90)
    interval = timedelta(minutes=5)
    batch: list = []
    downtime_rows: list = []
    rng = random.Random(42)

    total_steps = 10 * 90 * 24 * 12  # machines × days × hours × 5-min slots
    printed_pct = -1

    t = start_dt
    step = 0
    while t < datetime.now():
        for mid in range(1, 11):
            b = machine_baselines[mid]
            hour = t.hour
            # simulate load curve: higher during day shifts
            load_factor = 0.6 + 0.4 * math.sin(math.pi * (hour - 6) / 16) if 6 <= hour <= 22 else 0.5

            temp = round(b["temp"] * load_factor + rng.gauss(0, 0.8), 2)
            vib  = round(b["vib"]  * load_factor + rng.gauss(0, 0.05), 3)
            pwr  = round(b["pwr"]  * load_factor + rng.gauss(0, 0.3),  3)

            batch.append((mid, t, max(20.0, temp), max(0.0, vib), max(0.0, pwr)))

            # Occasional downtime event (≈0.1% of readings = ~260 events)
            if rng.random() < 0.001:
                duration_mins = rng.randint(15, 180)
                ended = t + timedelta(minutes=duration_mins)
                downtime_rows.append((mid, t, ended, rng.choice(reason_codes)))

        if len(batch) >= 5000:
            con.import_from_iterable(batch, ("IOT_RAW", "SENSOR_READINGS"))
            batch = []

        step += 1
        pct = int(step * 100 / (90 * 24 * 12))
        if pct != printed_pct and pct % 10 == 0:
            log(f"  {pct}% loaded ...")
            printed_pct = pct

        t += interval

    if batch:
        con.import_from_iterable(batch, ("IOT_RAW", "SENSOR_READINGS"))

    sensor_count = con.execute("SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS").fetchval()
    log(f"Sensor readings loaded: {sensor_count:,}")

    # ── 10. Seed downtime events ──────────────────────────────────────────────
    print("\n==> Seeding downtime events ...")
    if downtime_rows:
        downtime_rows_with_id = [
            (i + 1, mid, started, ended, code)
            for i, (mid, started, ended, code) in enumerate(downtime_rows)
        ]
        con.import_from_iterable(downtime_rows_with_id, ("IOT_RAW", "DOWNTIME_EVENTS"))
    downtime_count = con.execute("SELECT COUNT(*) FROM IOT_RAW.DOWNTIME_EVENTS").fetchval()
    log(f"Downtime events loaded: {downtime_count:,}")

    con.close()
    print("\n==> Exasol setup complete.\n")


if __name__ == "__main__":
    main()
