import os

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

st.set_page_config(page_title="Barça Hub", page_icon="⚽", layout="wide")
st.title("Barça Hub")

engine = create_engine(os.environ["DATABASE_URL"])

try:
    with engine.connect() as conn:
        tables = pd.read_sql(
            text("""
                SELECT table_schema AS schema, table_name AS "table"
                FROM information_schema.tables
                WHERE table_schema IN ('raw', 'dw', 'live')
                ORDER BY 1, 2
            """),
            conn,
        )
    st.success(f"Connected to the warehouse: {len(tables)} tables found")
    st.dataframe(tables, use_container_width=True, hide_index=True)
except Exception as e:
    st.error(f"Could not reach the warehouse: {e}")
