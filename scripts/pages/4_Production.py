"""Production Summary — weekly OEE trend, downtime, defect rates."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import plotly.express as px
import streamlit as st

from utils import insight, q

st.set_page_config(page_title="Production Summary", page_icon="📦", layout="wide")

OEE_WORLD_CLASS = 0.85


# ── Header ────────────────────────────────────────────────────────────────────
col_h, col_r = st.columns([6, 1])
col_h.markdown("## 📦 Production Summary")
col_h.caption("Weekly production KPIs rolled up from mart_oee_daily")
if col_r.button("↻ Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Data ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_prod():
    df = q("""
        SELECT machine_name, machine_type, production_line,
               week_start, avg_oee,
               total_units_produced, total_good_units,
               total_defects, total_downtime_hrs
        FROM MARTS.MART_PRODUCTION_SUMMARY
        ORDER BY week_start, machine_name
    """)
    if df.empty:
        return df
    # Normalise types — Exasol returns DATE as datetime.date, numerics as Decimal
    df["WEEK_START"]           = pd.to_datetime(df["WEEK_START"])
    df["AVG_OEE"]              = df["AVG_OEE"].astype(float)
    df["TOTAL_UNITS_PRODUCED"] = pd.to_numeric(df["TOTAL_UNITS_PRODUCED"], errors="coerce").fillna(0).astype(int)
    df["TOTAL_GOOD_UNITS"]     = pd.to_numeric(df["TOTAL_GOOD_UNITS"],     errors="coerce").fillna(0).astype(int)
    df["TOTAL_DEFECTS"]        = pd.to_numeric(df["TOTAL_DEFECTS"],        errors="coerce").fillna(0).astype(int)
    df["TOTAL_DOWNTIME_HRS"]   = pd.to_numeric(df["TOTAL_DOWNTIME_HRS"],   errors="coerce").fillna(0).astype(float)
    return df


df = load_prod()

if df.empty:
    st.info("No production data yet. Go to **Pipeline Runner** and run Step 3 (dbt models).")
    st.stop()

latest_week = df["WEEK_START"].max()
df_latest   = df[df["WEEK_START"] == latest_week]

# ── KPI cards ─────────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
c1.metric("Units produced (latest week)", f"{df_latest['TOTAL_UNITS_PRODUCED'].sum():,}")
c2.metric("Good units",                   f"{df_latest['TOTAL_GOOD_UNITS'].sum():,}")
c3.metric("Defects",                      f"{df_latest['TOTAL_DEFECTS'].sum():,}")
c4.metric("Total downtime",               f"{df_latest['TOTAL_DOWNTIME_HRS'].sum():.1f} h")

st.divider()

# ── Weekly OEE trend ──────────────────────────────────────────────────────────
st.subheader("Weekly avg OEE — all machines")
fig_oee = px.line(
    df, x="WEEK_START", y="AVG_OEE", color="MACHINE_NAME",
    labels={"AVG_OEE": "OEE", "WEEK_START": "Week", "MACHINE_NAME": "Machine"},
)
fig_oee.add_hline(y=OEE_WORLD_CLASS, line_dash="dash", line_color="#94a3b8",
                  annotation_text="85% world-class")
fig_oee.update_layout(
    yaxis_tickformat=".0%", height=380,
    margin=dict(l=0, r=0, t=10, b=20),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
)
st.plotly_chart(fig_oee, use_container_width=True)

weeks        = df.groupby("WEEK_START")["AVG_OEE"].mean().sort_index()
if len(weeks) >= 2:
    latest_oee = float(weeks.iloc[-1])
    prior_oee  = float(weeks.iloc[-2])
    delta      = latest_oee - prior_oee
    direction  = "up" if delta >= 0 else "down"
    n_below    = int((df[df["WEEK_START"] == df["WEEK_START"].max()]["AVG_OEE"] < OEE_WORLD_CLASS).sum())
    insight(
        f"Fleet OEE is trending **{direction}** by {abs(delta):.1%} week-on-week "
        f"(latest week avg: {latest_oee:.1%}). "
        f"{n_below} machine{'s are' if n_below != 1 else ' is'} still below the 85% world-class target."
    )

# ── Weekly downtime ────────────────────────────────────────────────────────────
st.subheader("Weekly downtime by machine")
fig_dt = px.bar(
    df, x="WEEK_START", y="TOTAL_DOWNTIME_HRS", color="MACHINE_NAME",
    labels={"TOTAL_DOWNTIME_HRS": "Downtime (hrs)", "WEEK_START": "Week"},
)
fig_dt.update_layout(
    height=340, margin=dict(l=0, r=0, t=10, b=20),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
)
st.plotly_chart(fig_dt, use_container_width=True)

total_dt = df["TOTAL_DOWNTIME_HRS"].sum()
if total_dt > 0:
    per_machine = df.groupby("MACHINE_NAME")["TOTAL_DOWNTIME_HRS"].sum()
    worst_dt_name = per_machine.idxmax()
    worst_dt_hrs  = float(per_machine.max())
    pct           = worst_dt_hrs / total_dt
    insight(
        f"**{worst_dt_name}** accounts for {pct:.0%} of total fleet downtime "
        f"({worst_dt_hrs:.1f} of {total_dt:.1f} hrs across all weeks). "
        f"Reducing this machine's downtime has the highest impact on production output."
    )

# ── Latest week table ──────────────────────────────────────────────────────────
st.subheader(f"Latest week detail — {latest_week.strftime('%Y-%m-%d')}")

df_tbl = df_latest[["MACHINE_NAME", "MACHINE_TYPE",
                     "TOTAL_UNITS_PRODUCED", "TOTAL_GOOD_UNITS",
                     "TOTAL_DEFECTS", "TOTAL_DOWNTIME_HRS", "AVG_OEE"]].copy()

df_tbl["DEFECT_RATE"] = (
    df_tbl["TOTAL_DEFECTS"] / df_tbl["TOTAL_UNITS_PRODUCED"].replace(0, pd.NA)
).fillna(0)

df_tbl["AVG_OEE"]            = df_tbl["AVG_OEE"].apply(lambda v: f"{v:.1%}")
df_tbl["DEFECT_RATE"]        = df_tbl["DEFECT_RATE"].apply(lambda v: f"{v:.2%}")
df_tbl["TOTAL_DOWNTIME_HRS"] = df_tbl["TOTAL_DOWNTIME_HRS"].apply(lambda v: f"{v:.1f} h")

df_tbl.columns = ["Machine", "Type", "Units produced", "Good units",
                  "Defects", "Downtime", "OEE", "Defect rate"]
st.dataframe(df_tbl, use_container_width=True, hide_index=True)
