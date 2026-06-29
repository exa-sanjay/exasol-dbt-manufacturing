"""
Seed Exasol IOT_RAW tables from the UCI AI4I 2020 Predictive Maintenance Dataset.

Source:  https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset
Format:  10,000 rows — air temp [K], process temp [K], rotational speed [rpm],
         torque [Nm], tool wear [min], 5 labelled failure modes.

Column mapping to IOT_RAW.SENSOR_READINGS:
  temperature_c   ← process temperature (K → °C), Z-scaled to each machine's baseline
  vibration_mm_s  ← rotational speed (rpm), Z-scaled to each machine's baseline
  power_kw        ← torque × angular velocity (τ × ω = T × RPM × π/30 / 1000 kW),
                     Z-scaled to each machine's baseline

Usage:
  python scripts/seed_iot_from_uci.py            # load 90-day history and exit
  python scripts/seed_iot_from_uci.py --live      # stream one reading/machine every 5 min

Requirements: run AFTER 'make seed' so IOT_RAW tables already exist.
"""

import argparse
import csv
import io
import math
import os
import random
import statistics
import sys
import time
import zipfile
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pyexasol
import requests

# ── Config ────────────────────────────────────────────────────────────────────
EXA_DSN      = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER     = os.environ.get("EXASOL_USER", "sys")
EXA_PASSWORD = os.environ.get("EXASOL_PASSWORD", "exasol")

INTERVAL_MIN = 5    # minutes between sensor readings
DAYS_HISTORY = 90   # days of historical data to generate
BATCH_SIZE   = 5000

# Try old UCI direct URL first, then new zip URL as fallback
UCI_CSV_URL  = "https://archive.ics.uci.edu/ml/machine-learning-databases/00601/ai4i2020.csv"
UCI_ZIP_URL  = "https://archive.ics.uci.edu/static/public/601/ai4i+2020+predictive+maintenance+dataset.zip"
UCI_CACHE    = os.path.join(os.path.dirname(__file__), "uci_ai4i2020.csv")

# Machine baselines — must match seed_postgres.sql and setup_exasol.py
MACHINES = {
    1:  {"temp": 68.0, "vib": 1.20, "pwr": 18.5, "temp_std": 3.0, "vib_std": 0.15, "pwr_std": 1.5},
    2:  {"temp": 70.5, "vib": 1.35, "pwr": 19.2, "temp_std": 3.0, "vib_std": 0.15, "pwr_std": 1.5},
    3:  {"temp": 55.0, "vib": 0.85, "pwr": 12.0, "temp_std": 2.5, "vib_std": 0.10, "pwr_std": 1.0},
    4:  {"temp": 56.5, "vib": 0.90, "pwr": 12.8, "temp_std": 2.5, "vib_std": 0.10, "pwr_std": 1.0},
    5:  {"temp": 82.0, "vib": 2.10, "pwr": 35.0, "temp_std": 5.0, "vib_std": 0.25, "pwr_std": 3.0},
    6:  {"temp": 81.5, "vib": 2.05, "pwr": 34.5, "temp_std": 5.0, "vib_std": 0.25, "pwr_std": 3.0},
    7:  {"temp": 60.0, "vib": 1.00, "pwr": 22.0, "temp_std": 3.0, "vib_std": 0.12, "pwr_std": 2.0},
    8:  {"temp": 75.0, "vib": 0.60, "pwr": 45.0, "temp_std": 4.0, "vib_std": 0.08, "pwr_std": 4.0},
    9:  {"temp": 74.5, "vib": 0.65, "pwr": 44.8, "temp_std": 4.0, "vib_std": 0.08, "pwr_std": 4.0},
    10: {"temp": 40.0, "vib": 0.20, "pwr":  8.5, "temp_std": 2.0, "vib_std": 0.05, "pwr_std": 0.8},
}

FAILURE_REASON_MAP = {
    "TWF": "TOOLING_FAILURE",
    "HDF": "MATERIAL_JAM",        # heat dissipation → closest to material issue
    "PWF": "POWER_FLUCTUATION",
    "OSF": "OPERATOR_ERROR",      # overstrain → operator-induced
    "RNF": "UNPLANNED_BREAKDOWN",
}


# ── Dataset download ──────────────────────────────────────────────────────────

def download_uci_dataset() -> str:
    """Download the UCI AI4I CSV. Caches to disk — only downloads once."""
    if os.path.exists(UCI_CACHE):
        print(f"    Using cached dataset: {UCI_CACHE}")
        with open(UCI_CACHE, "r", encoding="utf-8") as f:
            return f.read()

    # Try direct CSV URL first
    print(f"    Downloading from UCI ML Repository ...")
    for url, is_zip in [(UCI_CSV_URL, False), (UCI_ZIP_URL, True)]:
        try:
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()

            if is_zip:
                z = zipfile.ZipFile(io.BytesIO(resp.content))
                csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
                content = z.read(csv_name).decode("utf-8")
            else:
                content = resp.text

            # Sanity check — first few lines should include expected columns
            if "Rotational speed" not in content[:500]:
                continue

            with open(UCI_CACHE, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"    Cached to {UCI_CACHE}")
            return content

        except Exception:
            continue

    # Both URLs failed
    print("\nERROR: Could not download the UCI AI4I 2020 dataset automatically.")
    print("       Please download it manually:")
    print("       1. Go to: https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset")
    print("       2. Download the dataset ZIP, extract the CSV")
    print(f"       3. Save it as: {UCI_CACHE}")
    sys.exit(1)


# ── CSV parsing ───────────────────────────────────────────────────────────────

def parse_uci_csv(content: str):
    """
    Parse UCI AI4I CSV into a list of dicts and compute normalization statistics.

    Returns (rows, stats) where:
      rows  — list of dicts with keys: process_temp, rpm, power_kw, failure, failure_types
      stats — dict with mean/std for each raw signal (for Z-score normalization)
    """
    reader = csv.DictReader(io.StringIO(content))
    rows = []

    for row in reader:
        try:
            process_temp_k = float(row["Process temperature [K]"])
            rpm            = float(row["Rotational speed [rpm]"])
            torque         = float(row["Torque [Nm]"])
            # Physical formula: P(kW) = torque(Nm) × ω(rad/s) / 1000
            #                         = torque × RPM × π / 30 / 1000
            power_kw       = torque * rpm * math.pi / 30.0 / 1000.0
            failure        = int(row.get("Machine failure", 0)) == 1
            failure_types  = {
                k: int(row.get(k, 0)) == 1 for k in FAILURE_REASON_MAP
            }
            rows.append({
                "process_temp": process_temp_k - 273.15,  # K → °C
                "rpm":          rpm,
                "power_kw":     power_kw,
                "failure":      failure,
                "failure_types":failure_types,
            })
        except (ValueError, KeyError):
            continue

    if not rows:
        print("ERROR: No rows parsed from UCI CSV. Check the file format.")
        sys.exit(1)

    temps  = [r["process_temp"] for r in rows]
    rpms   = [r["rpm"]          for r in rows]
    powers = [r["power_kw"]     for r in rows]

    stats = {
        "temp_mean":  statistics.mean(temps),
        "temp_std":   statistics.stdev(temps),
        "rpm_mean":   statistics.mean(rpms),
        "rpm_std":    statistics.stdev(rpms),
        "pwr_mean":   statistics.mean(powers),
        "pwr_std":    statistics.stdev(powers),
    }

    failure_count = sum(1 for r in rows if r["failure"])
    print(f"    Parsed {len(rows):,} rows  |  {failure_count} failure events  ({failure_count/len(rows)*100:.1f}%)")
    print(f"    Temp:  {stats['temp_mean']:.1f}°C  ±{stats['temp_std']:.2f}")
    print(f"    RPM:   {stats['rpm_mean']:.0f}    ±{stats['rpm_std']:.0f}")
    print(f"    Power: {stats['pwr_mean']:.3f} kW ±{stats['pwr_std']:.3f}")

    return rows, stats


# ── Signal transformation ─────────────────────────────────────────────────────

def transform_to_machine(uci_row: dict, machine_id: int, stats: dict):
    """
    Map one UCI row to sensor values for a given machine using Z-score scaling.

    The UCI signal's Z-score is preserved, then rescaled to the machine's
    expected baseline ± std-dev. This keeps the UCI dataset's real variation
    patterns (trends, spikes, failure signatures) intact.
    """
    bl = MACHINES[machine_id]

    def z_scale(raw, raw_mean, raw_std, target_mean, target_std):
        z = (raw - raw_mean) / raw_std if raw_std > 0 else 0.0
        return target_mean + z * target_std

    temp = z_scale(uci_row["process_temp"], stats["temp_mean"], stats["temp_std"],
                   bl["temp"], bl["temp_std"])
    vib  = z_scale(uci_row["rpm"],          stats["rpm_mean"],  stats["rpm_std"],
                   bl["vib"],  bl["vib_std"])
    pwr  = z_scale(uci_row["power_kw"],     stats["pwr_mean"],  stats["pwr_std"],
                   bl["pwr"],  bl["pwr_std"])

    return (
        max(20.0, round(temp, 2)),
        max(0.0,  round(abs(vib), 3)),
        max(0.0,  round(abs(pwr), 3)),
    )


def pick_failure_reason(failure_types: dict) -> str:
    """Return the first active failure reason code, or UNPLANNED_BREAKDOWN."""
    for key, reason in FAILURE_REASON_MAP.items():
        if failure_types.get(key):
            return reason
    return "UNPLANNED_BREAKDOWN"


# ── Historical load ───────────────────────────────────────────────────────────

def seed_historical(con, uci_rows: list, stats: dict):
    """
    Generate DAYS_HISTORY days of sensor readings by cycling through UCI rows.

    90 days × 24h × 12 readings/h = 25,920 time steps × 10 machines = 259,200 rows.
    The 10,000-row UCI dataset cycles ~2.6× to cover the full history window.
    Each machine gets independently scaled values, preserving UCI variation patterns.
    """
    start_dt    = datetime.now() - timedelta(days=DAYS_HISTORY)
    total_steps = DAYS_HISTORY * 24 * 60 // INTERVAL_MIN   # 25,920

    print(f"\n==> Seeding {total_steps * 10:,} sensor readings ({DAYS_HISTORY} days × 10 machines) ...")

    sensor_batch   = []
    downtime_rows  = []
    last_failure_t = {}   # machine_id → last downtime timestamp (cooldown tracker)
    step           = 0
    t              = start_dt
    printed_pct    = -1

    while t < datetime.now():
        uci_row = uci_rows[step % len(uci_rows)]

        for mid in range(1, 11):
            temp, vib, pwr = transform_to_machine(uci_row, mid, stats)
            sensor_batch.append((mid, t, temp, vib, pwr))

            # Generate a downtime event when UCI flags a failure,
            # but enforce a 2-hour cooldown per machine to avoid cascading events.
            if uci_row["failure"]:
                last_t = last_failure_t.get(mid)
                if last_t is None or (t - last_t).total_seconds() > 7200:
                    reason   = pick_failure_reason(uci_row["failure_types"])
                    duration = random.randint(15, 180)
                    downtime_rows.append((mid, t, t + timedelta(minutes=duration), reason))
                    last_failure_t[mid] = t

        if len(sensor_batch) >= BATCH_SIZE:
            con.import_from_iterable(
                sensor_batch,
                table_name="IOT_RAW.SENSOR_READINGS",
                columns=["machine_id", "ts", "temperature_c", "vibration_mm_s", "power_kw"],
            )
            sensor_batch = []

        pct = int(step * 100 / total_steps)
        if pct != printed_pct and pct % 10 == 0:
            print(f"    {pct}% ...", end="\r", flush=True)
            printed_pct = pct

        t    += timedelta(minutes=INTERVAL_MIN)
        step += 1

    # Flush remainder
    if sensor_batch:
        con.import_from_iterable(
            sensor_batch,
            table_name="IOT_RAW.SENSOR_READINGS",
            columns=["machine_id", "ts", "temperature_c", "vibration_mm_s", "power_kw"],
        )

    total_readings = step * 10
    print(f"    {total_readings:,} sensor readings inserted.           ")

    if downtime_rows:
        con.import_from_iterable(
            downtime_rows,
            table_name="IOT_RAW.DOWNTIME_EVENTS",
            columns=["machine_id", "started_at", "ended_at", "reason_code"],
        )
        print(f"    {len(downtime_rows)} downtime events inserted "
              f"({len(downtime_rows)/total_readings*100:.2f}% of readings).")


# ── Live streaming mode ───────────────────────────────────────────────────────

def live_mode(con, uci_rows: list, stats: dict):
    """
    Stream one sensor reading per machine every INTERVAL_MIN minutes into Exasol.

    Position in the UCI dataset is derived from the current reading count so
    restarts continue from where they left off.
    """
    print(f"\n==> Live mode — inserting 10 readings every {INTERVAL_MIN} min  (Ctrl+C to stop)\n")

    # Resume position from existing row count
    existing = con.execute(
        "SELECT COUNT(*) FROM IOT_RAW.SENSOR_READINGS"
    ).fetchval()
    position = int(existing // 10) % len(uci_rows)
    print(f"    Resuming at UCI row {position} (based on {existing:,} existing readings)\n")

    last_failure_t = {}

    while True:
        now     = datetime.now()
        uci_row = uci_rows[position % len(uci_rows)]
        batch   = []

        for mid in range(1, 11):
            temp, vib, pwr = transform_to_machine(uci_row, mid, stats)
            batch.append((mid, now, temp, vib, pwr))

        con.import_from_iterable(batch, ("IOT_RAW", "SENSOR_READINGS"))

        # Downtime event handling with cooldown
        if uci_row["failure"]:
            reason = pick_failure_reason(uci_row["failure_types"])
            next_id = int(con.execute("SELECT COALESCE(MAX(event_id), 0) + 1 FROM IOT_RAW.DOWNTIME_EVENTS").fetchval())
            for mid in range(1, 11):
                last_t = last_failure_t.get(mid)
                if last_t is None or (now - last_t).total_seconds() > 7200:
                    duration = random.randint(15, 60)
                    end_ts = now + timedelta(minutes=duration)
                    con.execute(
                        f"INSERT INTO IOT_RAW.DOWNTIME_EVENTS VALUES"
                        f"({next_id}, {mid}, TIMESTAMP '{now.strftime('%Y-%m-%d %H:%M:%S')}',"
                        f" TIMESTAMP '{end_ts.strftime('%Y-%m-%d %H:%M:%S')}', '{reason}')"
                    )
                    next_id += 1
                    last_failure_t[mid] = now
            print(f"  [{now.strftime('%H:%M:%S')}] [!] Failure event: {reason}")
        else:
            print(f"  [{now.strftime('%H:%M:%S')}] Inserted 10 readings  (UCI row {position})")

        position += 1
        time.sleep(INTERVAL_MIN * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Seed Exasol IOT_RAW from UCI AI4I 2020 Predictive Maintenance Dataset"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=f"Stream one reading per machine every {INTERVAL_MIN} minutes (real-time simulation)",
    )
    args = parser.parse_args()

    print("\n==> UCI AI4I 2020 -> Exasol IOT_RAW seeder")
    print(f"    Mode: {'LIVE (streaming)' if args.live else 'HISTORICAL (batch)'}\n")

    # ── Download + parse ──────────────────────────────────────────────────────
    print("── Step 1: Fetching UCI dataset ...")
    content  = download_uci_dataset()
    uci_rows, stats = parse_uci_csv(content)

    # ── Connect ───────────────────────────────────────────────────────────────
    print("\n── Step 2: Connecting to Exasol ...")
    try:
        con = pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASSWORD,
                               websocket_sslopt={"cert_reqs": 0})
        print(f"    Connected to {EXA_DSN}")
    except Exception as e:
        print(f"\nERROR: Cannot connect to Exasol at {EXA_DSN}: {e}")
        print("       Make sure 'make up' and 'make seed' have completed first.")
        sys.exit(1)

    # ── Clear existing IoT data (historical mode only) ────────────────────────
    if not args.live:
        print("\n── Step 3: Clearing existing IoT data ...")
        con.execute("TRUNCATE TABLE IOT_RAW.SENSOR_READINGS")
        con.execute("TRUNCATE TABLE IOT_RAW.DOWNTIME_EVENTS")
        print("    IOT_RAW tables cleared.")

    # ── Seed ──────────────────────────────────────────────────────────────────
    try:
        if args.live:
            live_mode(con, uci_rows, stats)
        else:
            seed_historical(con, uci_rows, stats)
    except KeyboardInterrupt:
        print("\n\n    Stopped by user.")
    finally:
        con.close()

    if not args.live:
        print("\n==> Done.")
        print("    Next step: run 'make dbt-run' to rebuild analytics models with the new data.\n")


if __name__ == "__main__":
    main()
