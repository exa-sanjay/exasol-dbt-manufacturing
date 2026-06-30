"""Machine Health — anomaly heatmap + per-machine sensor readings."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import plotly.express as px
import streamlit as st

from utils import insight, q

st.set_page_config(page_title="Machine Health", page_icon="🌡️", layout="wide")


# ── Header ────────────────────────────────────────────────────────────────────
col_h, col_r = st.columns([6, 1])
col_h.markdown("## 🌡️ Machine Health")
col_h.caption("Daily sensor averages and anomaly detection from IOT_RAW.SENSOR_READINGS")
if col_r.button("↻ Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Data ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_health():
    return q("""
        SELECT h.machine_id,
               o.machine_name,
               o.machine_type,
               h.reading_date,
               h.avg_temp_c,
               h.max_temp_c,
               h.avg_vibration_mm_s,
               h.max_vibration_mm_s,
               h.avg_power_kw,
               h.max_power_kw,
               h.anomaly_flag
        FROM MARTS.MART_MACHINE_HEALTH h
        JOIN (
            SELECT DISTINCT machine_id, machine_name, machine_type
            FROM MARTS.MART_OEE_DAILY
        ) o ON h.machine_id = o.machine_id
        ORDER BY h.reading_date
    """)


df = load_health()

if df.empty:
    st.info("No health data yet. Go to **Pipeline Runner** and run Step 3 (dbt models).")
    st.stop()

latest    = df.sort_values("READING_DATE").groupby("MACHINE_NAME").last().reset_index()
n_anomaly = int(latest["ANOMALY_FLAG"].astype(bool).sum())

c1, c2 = st.columns(2)
c1.metric("Machines with active anomaly", f"{n_anomaly} / {len(latest)}")
c2.metric("Latest reading date", str(latest["READING_DATE"].max()))

st.divider()

# ── Anomaly heatmap ────────────────────────────────────────────────────────────
st.subheader("Anomaly flag by machine — all days")
df_hm = df.copy()
df_hm["ANOMALY_NUM"] = df_hm["ANOMALY_FLAG"].astype(int)
df_pivot = df_hm.pivot_table(
    index="MACHINE_NAME", columns="READING_DATE",
    values="ANOMALY_NUM", aggfunc="max",
)
fig_hm = px.imshow(
    df_pivot,
    color_continuous_scale=[[0, "#0f2942"], [1, "#ef4444"]],
    labels={"x": "Date", "y": "Machine", "color": "Anomaly"},
    aspect="auto",
)
fig_hm.update_layout(
    height=330, coloraxis_showscale=False,
    margin=dict(l=0, r=0, t=10, b=20),
)
st.plotly_chart(fig_hm, use_container_width=True)

anomaly_counts  = df_hm.groupby("MACHINE_NAME")["ANOMALY_NUM"].sum()
most_name       = anomaly_counts.idxmax()
most_days       = int(anomaly_counts.max())
total_days      = df_hm["READING_DATE"].nunique()
insight(
    f"**{most_name}** had the most anomaly days ({most_days} of {total_days} days). "
    f"{n_anomaly} machine{'s are' if n_anomaly != 1 else ' is'} currently showing an active anomaly — "
    f"select it below to see which sensor is spiking."
)

st.divider()

# ── Per-machine sensor detail ──────────────────────────────────────────────────
st.subheader("Sensor readings — per machine")
machine       = st.selectbox("Machine", sorted(df["MACHINE_NAME"].unique()))
df_m          = df[df["MACHINE_NAME"] == machine].copy()
anomaly_dates = df_m[df_m["ANOMALY_FLAG"].astype(bool)]["READING_DATE"].tolist()

c1, c2, c3 = st.columns(3)
for col, y_cols, title, unit in [
    (c1, ["AVG_TEMP_C",         "MAX_TEMP_C"],         "Temperature",  "°C"),
    (c2, ["AVG_VIBRATION_MM_S", "MAX_VIBRATION_MM_S"], "Vibration",    "mm/s"),
    (c3, ["AVG_POWER_KW",       "MAX_POWER_KW"],       "Power",        "kW"),
]:
    fig = px.line(df_m, x="READING_DATE", y=y_cols,
                  labels={"value": unit, "READING_DATE": "", "variable": ""},
                  title=f"{title} ({unit})")
    for d in anomaly_dates:
        fig.add_vrect(x0=d, x1=d, fillcolor="#ef4444", opacity=0.12, line_width=0)
    fig.update_layout(
        height=270, showlegend=False,
        margin=dict(l=0, r=0, t=30, b=0),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    col.plotly_chart(fig, use_container_width=True)

n_anomaly_days = len(anomaly_dates)
if n_anomaly_days > 0:
    temp_vals    = pd.to_numeric(df_m["MAX_TEMP_C"], errors="coerce")
    vib_vals     = pd.to_numeric(df_m["MAX_VIBRATION_MM_S"], errors="coerce")
    max_temp_row = df_m.loc[temp_vals.idxmax()]
    max_vib_row  = df_m.loc[vib_vals.idxmax()]
    peak_temp    = float(max_temp_row["MAX_TEMP_C"])
    peak_vib     = float(max_vib_row["MAX_VIBRATION_MM_S"])
    avg_temp     = float(df_m["AVG_TEMP_C"].astype(float).mean())
    avg_vib      = float(df_m["AVG_VIBRATION_MM_S"].astype(float).mean())
    primary = "temperature" if (peak_temp / avg_temp) > (peak_vib / avg_vib) else "vibration"
    insight(
        f"**{machine}** triggered {n_anomaly_days} anomaly day{'s' if n_anomaly_days != 1 else ''} "
        f"(red bands). The primary driver is **{primary}** — "
        f"peak was {peak_temp:.1f}°C vs avg {avg_temp:.1f}°C for temp, "
        f"{peak_vib:.3f} vs avg {avg_vib:.3f} mm/s for vibration."
    )
else:
    insight(f"**{machine}** shows no anomaly days in this period — sensor readings are within normal thresholds.")

# Anomaly days table
anomaly_df = df_m[df_m["ANOMALY_FLAG"].astype(bool)][
    ["READING_DATE", "MAX_TEMP_C", "MAX_VIBRATION_MM_S", "MAX_POWER_KW"]
].copy()
if not anomaly_df.empty:
    st.subheader(f"Anomaly days — {machine}")
    anomaly_df.columns = ["Date", "Max Temp (°C)", "Max Vibration (mm/s)", "Max Power (kW)"]
    st.dataframe(anomaly_df, use_container_width=True, hide_index=True)
