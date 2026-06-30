"""AI Maintenance Queue — urgency tiers, root causes, hours-to-failure chart."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from utils import insight, q

st.set_page_config(page_title="AI Maintenance Queue", page_icon="🤖", layout="wide")

TIER_COLOR = {"CRITICAL": "#ef4444", "HIGH": "#f97316", "MEDIUM": "#eab308", "LOW": "#22c55e"}


# ── Header ────────────────────────────────────────────────────────────────────
col_h, col_r = st.columns([6, 1])
col_h.markdown("## 🤖 AI Maintenance Queue")
col_h.caption(
    "At-risk machines with Ollama-generated root cause and maintenance recommendations  ·  "
    "Exasol cosine similarity → qwen2.5:7b (local, no cloud API)"
)
if col_r.button("↻ Refresh", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

st.divider()

# ── Data ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_queue():
    return q("""
        SELECT machine_name, machine_type, production_line,
               urgency_tier,
               estimated_hours_to_failure,
               confidence,
               oee_last_7d,
               oee_trend_delta,
               avg_temp_c,
               avg_vibration_mm_s,
               root_cause,
               recommended_action,
               ai_generated_at
        FROM MARTS.MART_AI_MAINTENANCE_QUEUE
        ORDER BY
            CASE urgency_tier
                WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                WHEN 'MEDIUM'   THEN 3 ELSE 4
            END,
            estimated_hours_to_failure ASC NULLS LAST
    """)


df = load_queue()

if df.empty:
    st.info(
        "No AI recommendations yet.  \n"
        "Go to **Pipeline Runner** → Step 4 (AI setup) → Step 5 (AI agent)."
    )
    st.stop()

# ── Urgency tier summary ──────────────────────────────────────────────────────
tiers = df["URGENCY_TIER"].value_counts().to_dict()
cols  = st.columns(4)
for i, tier in enumerate(["CRITICAL", "HIGH", "MEDIUM", "LOW"]):
    count = int(tiers.get(tier, 0))
    color = TIER_COLOR[tier]
    cols[i].markdown(
        f"<div style='background:{color}18;border-left:4px solid {color};"
        f"padding:14px 16px;border-radius:6px'>"
        f"<div style='font-size:2rem;font-weight:700;color:{color};line-height:1'>{count}</div>"
        f"<div style='font-size:0.8rem;color:#94a3b8;margin-top:4px'>{tier}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

st.divider()

# ── Insight after tier summary ────────────────────────────────────────────────
n_critical = int(tiers.get("CRITICAL", 0))
n_high     = int(tiers.get("HIGH", 0))
top_row    = df.iloc[0]
top_name   = top_row.get("MACHINE_NAME", "—")
top_hrs    = top_row.get("ESTIMATED_HOURS_TO_FAILURE")
top_tier   = top_row.get("URGENCY_TIER", "—")
hrs_str    = f"{float(top_hrs):.0f} h" if top_hrs is not None else "unknown hours"

if n_critical > 0:
    insight(
        f"**{n_critical} CRITICAL machine{'s' if n_critical != 1 else ''}** need immediate attention — "
        f"**{top_name}** is the most urgent at ~{hrs_str} to failure. "
        f"Expanding its card below shows the Ollama-generated root cause and recommended action."
    )
elif n_high > 0:
    insight(
        f"No CRITICAL machines right now, but **{n_high} HIGH**-priority machine{'s' if n_high != 1 else ''} "
        f"{'are' if n_high != 1 else 'is'} at risk — **{top_name}** leads with ~{hrs_str} to failure."
    )
else:
    insight("All machines are MEDIUM or LOW priority — no immediate failures predicted. Monitor trend direction.")

# ── Machine cards ─────────────────────────────────────────────────────────────
for _, row in df.iterrows():
    tier      = row.get("URGENCY_TIER") or "LOW"
    color     = TIER_COLOR.get(tier, "#6b7280")
    hrs       = row.get("ESTIMATED_HOURS_TO_FAILURE")
    hrs_str   = f"{float(hrs):.0f} h" if hrs is not None else "—"
    delta     = row.get("OEE_TREND_DELTA")
    delta_str = (
        f"▼ {abs(float(delta)):.1%}" if delta is not None and float(delta) < 0
        else f"▲ {float(delta):.1%}"  if delta is not None and float(delta) >= 0
        else "—"
    )

    with st.expander(
        f"**{row['MACHINE_NAME']}**  ·  {tier}  ·  {hrs_str} to failure  ·  OEE trend {delta_str}",
        expanded=(tier in ("CRITICAL", "HIGH")),
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Type", row.get("MACHINE_TYPE") or "—")
        c2.metric("OEE (7 d)",
                  f"{float(row['OEE_LAST_7D']):.1%}" if row.get("OEE_LAST_7D") is not None else "—")
        c3.metric("Avg temp",
                  f"{float(row['AVG_TEMP_C']):.1f} °C" if row.get("AVG_TEMP_C") is not None else "—")
        c4.metric("Avg vibration",
                  f"{float(row['AVG_VIBRATION_MM_S']):.3f} mm/s"
                  if row.get("AVG_VIBRATION_MM_S") is not None else "—")

        st.markdown(f"**Root cause:** {row.get('ROOT_CAUSE') or '—'}")
        st.markdown(f"**Recommended action:** {row.get('RECOMMENDED_ACTION') or '—'}")

        parts = []
        if row.get("CONFIDENCE"):     parts.append(f"Confidence: {row['CONFIDENCE']}")
        if row.get("AI_GENERATED_AT"): parts.append(f"Generated: {row['AI_GENERATED_AT']}")
        if parts:
            st.caption("  ·  ".join(parts))

# ── Hours-to-failure chart ────────────────────────────────────────────────────
df_hrs = df[df["ESTIMATED_HOURS_TO_FAILURE"].notna()].copy()
if not df_hrs.empty:
    st.divider()
    st.subheader("Estimated hours to failure")
    fig = go.Figure(go.Bar(
        x=df_hrs["MACHINE_NAME"],
        y=df_hrs["ESTIMATED_HOURS_TO_FAILURE"].astype(float),
        marker_color=df_hrs["URGENCY_TIER"].map(TIER_COLOR),
        text=df_hrs["ESTIMATED_HOURS_TO_FAILURE"].apply(lambda v: f"{float(v):.0f} h"),
        textposition="outside",
    ))
    for y, label, color in [
        (8,  "CRITICAL (8 h)",  "#ef4444"),
        (24, "HIGH (24 h)",     "#f97316"),
        (72, "MEDIUM (72 h)",   "#eab308"),
    ]:
        fig.add_hline(y=y, line_dash="dash", line_color=color, annotation_text=label)
    fig.update_layout(
        yaxis_title="Hours", height=400,
        margin=dict(l=0, r=0, t=20, b=20),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    shortest_hrs = df_hrs["ESTIMATED_HOURS_TO_FAILURE"].astype(float).min()
    hrs_numeric      = pd.to_numeric(df_hrs["ESTIMATED_HOURS_TO_FAILURE"], errors="coerce")
    shortest_machine = df_hrs.loc[hrs_numeric.idxmin(), "MACHINE_NAME"]
    n_under_24 = int((df_hrs["ESTIMATED_HOURS_TO_FAILURE"].astype(float) <= 24).sum())
    insight(
        f"**{shortest_machine}** has the shortest runway at {shortest_hrs:.0f} h. "
        f"{n_under_24} machine{'s are' if n_under_24 != 1 else ' is'} within the 24-hour HIGH threshold — "
        f"prioritise these for same-shift intervention."
    )
