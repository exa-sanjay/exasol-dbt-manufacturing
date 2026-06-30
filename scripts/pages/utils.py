"""Shared helpers for all Streamlit dashboard pages."""
import os
import pandas as pd
import pyexasol
import streamlit as st

EXA_DSN  = os.environ.get("EXASOL_HOST", "localhost") + ":" + os.environ.get("EXASOL_PORT", "8563")
EXA_USER = os.environ.get("EXASOL_USER", "sys")
EXA_PASS = os.environ.get("EXASOL_PASSWORD", "exasol")


@st.cache_resource
def get_con():
    return pyexasol.connect(dsn=EXA_DSN, user=EXA_USER, password=EXA_PASS,
                            websocket_sslopt={"cert_reqs": 0})


def q(sql: str) -> pd.DataFrame:
    try:
        stmt = get_con().execute(sql)
        cols = list(stmt.columns().keys())
        rows = stmt.fetchall()
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    except Exception as exc:
        # If the cached connection went stale (Exasol restarted), clear it and retry once
        if "closed" in str(exc).lower() or "connection" in str(exc).lower():
            st.cache_resource.clear()
            try:
                stmt = get_con().execute(sql)
                cols = list(stmt.columns().keys())
                rows = stmt.fetchall()
                return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
            except Exception as exc2:
                st.error(f"Query failed after reconnect: {exc2}")
                return pd.DataFrame()
        st.error(f"Query failed: {exc}")
        return pd.DataFrame()


def insight(text: str):
    st.markdown(
        f"<div style='background:#1e3a5f33;border-left:3px solid #3b82f6;"
        f"padding:9px 14px;border-radius:4px;font-size:0.875rem;margin:4px 0 16px 0'>"
        f"💡 {text}</div>",
        unsafe_allow_html=True,
    )
