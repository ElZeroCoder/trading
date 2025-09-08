import streamlit as st
import pandas as pd
import sqlite3
import os

st.set_page_config(page_title='Trading Bot Dashboard')

st.title('Trading Bot Dashboard')

DB = os.path.join(os.path.dirname(__file__), 'data', 'bot.db')
if not os.path.exists(DB):
    st.warning('No database found. Run the bot first.')
else:
    conn = sqlite3.connect(DB)
    df = pd.read_sql_query('SELECT * FROM positions ORDER BY entry_ts DESC LIMIT 200', conn)
    st.subheader('Positions')
    st.dataframe(df)
    bal = pd.read_sql_query('SELECT * FROM balances ORDER BY ts DESC LIMIT 200', conn)
    st.subheader('Balances')
    if 'balance' in bal.columns:
        st.line_chart(bal['balance'])
    conn.close()
