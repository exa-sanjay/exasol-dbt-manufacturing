"""OEE Overview — avg OEE by machine, trend over time, component breakdown."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from utils import insight, q

st.set_page_config(page_title="OEE Overview", page_icon="📊", layout="wide")

OEE_WORLD_CLASS = 0.85


def oee_color(val):
    if val is None:  return "#6b7280"
    if val >= 0.85:  return "#22c55e"
    if val >= 0.60:  return "#eab308"
    return "#ef4444"


# ── Header ────────────────────────────────────────────────────────────────────
col_h, col_r = st.columns([6, 1])
col_h.markdown("## 📊 OEE Overview")
col_h.caption("Overall Equipment Effectiveness  ·  OEE = Availability × Performance × Quality")
if col_r.button("↻ Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Data ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_summary():
    return q("""
        SELECT machine_name, machine_type, production_line,
               AVG(oee)               AS avg_oee,
               AVG(availability_rate) AS avg_availability,
               AVG(performance_rate)  AS avg_performance,
               AVG(quality_rate)      AS avg_quality
        FROM MARTS.MART_OEE_DAILY
        WHERE shift_date >= ADD_DAYS((SELECT MAX(shift_date) FROM MARTS.MART_OEE_DAILY), -7)
        GROUP BY machine_name, machine_type, production_line
        ORDER BY avg_oee DESC
    """)


@st.cache_data(ttl=60)
def load_trend():
    return q("""
        SELECT machine_name, shift_date, oee,
               availability_rate, performance_rate, quality_rate
        FROM MARTS.MART_OEE_DAILY
        ORDER BY shift_date
    """)


df_sum   = load_summary()
df_trend = load_trend()

if df_sum.empty:
    st.info("No OEE data yet. Go to **Pipeline Runner** and run Step 3 (dbt models).")
    st.stop()

# ── KPI cards ─────────────────────────────────────────────────────────────────
avg_oee       = float(df_sum["AVG_OEE"].mean())
n_world_class = int((df_sum["AVG_OEE"].astype(float) >= OEE_WORLD_CLASS).sum())
n_at_risk     = int((df_sum["AVG_OEE"].astype(float) < 0.60).sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("Fleet avg OEE (7 d)",  f"{avg_oee:.1%}")
c2.metric("World-class (≥ 85%)",  f"{n_world_class} machines")
c3.metric("At-risk (< 60%)",      f"{n_at_risk} machines")
c4.metric("Total machines",        len(df_sum))

st.divider()

# ── OEE bar chart ──────────────────────────────────────────────────────────────
df_bar = df_sum.sort_values("AVG_OEE")
fig_bar = go.Figure(go.Bar(
    x=df_bar["AVG_OEE"].astype(float),
    y=df_bar["MACHINE_NAME"],
    orientation="h",
    marker_color=df_bar["AVG_OEE"].astype(float).apply(oee_color),
    text=df_bar["AVG_OEE"].astype(float).apply(lambda v: f"{v:.1%}"),
    textposition="outside",
))
fig_bar.add_vline(x=OEE_WORLD_CLASS, line_dash="dash", line_color="#94a3b8",
                  annotation_text="World-class 85%", annotation_position="top right")
fig_bar.update_layout(
    title="OEE by machine — last 7 days avg",
    xaxis_tickformat=".0%", xaxis_range=[0, 1.1],
    height=420, margin=dict(l=0, r=80, t=40, b=20),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
)
st.plotly_chart(fig_bar, use_container_width=True)

oee_vals  = pd.to_numeric(df_sum["AVG_OEE"], errors="coerce")
worst_idx = oee_vals.idxmin()
if pd.notna(worst_idx):
    worst   = df_sum.loc[worst_idx]
    n_below = int((oee_vals < OEE_WORLD_CLASS).sum())
    gap     = OEE_WORLD_CLASS - float(worst["AVG_OEE"])
    insight(
        f"**{worst['MACHINE_NAME']}** is the lowest performer at {float(worst['AVG_OEE']):.1%} — "
        f"{gap:.0%} below world-class. {n_below} of {len(df_sum)} machines are under the 85% target."
    )

# ── OEE trend line ─────────────────────────────────────────────────────────────
st.subheader("OEE trend over time")
machines = sorted(df_trend["MACHINE_NAME"].unique())
selected = st.multiselect("Machines", machines, default=list(machines[:4]))
df_sel   = df_trend[df_trend["MACHINE_NAME"].isin(selected)] if selected else df_trend

fig_line = px.line(df_sel, x="SHIFT_DATE", y="OEE", color="MACHINE_NAME",
                   labels={"OEE": "OEE", "SHIFT_DATE": "Date", "MACHINE_NAME": "Machine"})
fig_line.add_hline(y=OEE_WORLD_CLASS, line_dash="dash", line_color="#94a3b8",
                   annotation_text="85%")
fig_line.update_layout(
    yaxis_tickformat=".0%", height=360,
    margin=dict(l=0, r=0, t=10, b=20),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
)
st.plotly_chart(fig_line, use_container_width=True)

trend_oee = df_trend["OEE"].astype(float)
dates     = pd.to_datetime(df_trend["SHIFT_DATE"])
max_date  = dates.max()
recent    = trend_oee[dates >= max_date - pd.Timedelta(days=7)].mean()
prior     = trend_oee[(dates >= max_date - pd.Timedelta(days=14)) & (dates < max_date - pd.Timedelta(days=7))].mean()
if pd.notna(recent) and pd.notna(prior) and prior > 0:
    direction = "improved" if recent >= prior else "declined"
    delta     = abs(recent - prior)
    insight(f"Fleet OEE has **{direction}** by {delta:.1%} over the last 7 days vs the prior week.")
else:
    insight(f"Fleet OEE over the selected period averages {recent:.1%}." if pd.notna(recent) else "Select machines above to see the trend.")

# ── OEE components ─────────────────────────────────────────────────────────────
st.subheader("OEE components — last 7-day avg")
df_comp = df_sum[["MACHINE_NAME", "AVG_AVAILABILITY", "AVG_PERFORMANCE", "AVG_QUALITY"]].copy()
df_comp = df_comp.melt("MACHINE_NAME", var_name="Component", value_name="Rate")
df_comp["Component"] = df_comp["Component"].str.replace("AVG_", "").str.title()
df_comp["Rate"] = df_comp["Rate"].astype(float)

fig_comp = px.bar(
    df_comp, x="MACHINE_NAME", y="Rate", color="Component", barmode="group",
    labels={"Rate": "Rate", "MACHINE_NAME": "Machine"},
    color_discrete_map={"Availability": "#3b82f6", "Performance": "#8b5cf6", "Quality": "#06b6d4"},
)
fig_comp.add_hline(y=OEE_WORLD_CLASS, line_dash="dash", line_color="#94a3b8")
fig_comp.update_layout(
    yaxis_tickformat=".0%", height=360,
    margin=dict(l=0, r=0, t=10, b=20),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
)
st.plotly_chart(fig_comp, use_container_width=True)

avg_a = float(df_sum["AVG_AVAILABILITY"].astype(float).mean())
avg_p = float(df_sum["AVG_PERFORMANCE"].astype(float).mean())
avg_q = float(df_sum["AVG_QUALITY"].astype(float).mean())
weakest_name, weakest_val = min(
    [("Availability", avg_a), ("Performance", avg_p), ("Quality", avg_q)], key=lambda x: x[1]
)
insight(
    f"**{weakest_name}** is the weakest OEE component fleet-wide at {weakest_val:.1%}. "
    f"Targeting this alone would have the highest leverage on overall OEE."
)
