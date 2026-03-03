import os
import streamlit as st

st.set_page_config(page_title="BetSafe", page_icon="🛡️", layout="wide")

st.title("🛡️ BetSafe")
st.caption("Automated downside protection for event markets.")

mode = "LIVE" if os.getenv("ARM_LIVE", "false").lower() in ("true","1","yes") and os.getenv("DRY_RUN","true").lower() in ("false","0","no") else "DRY RUN"
st.warning(f"Demo mode recommended: DRY_RUN={os.getenv('DRY_RUN','(not set)')}, ARM_LIVE={os.getenv('ARM_LIVE','(not set)')}")

st.subheader("Settings")
ticker = st.text_input("Ticker", value=os.getenv("TICKER", ""))
budget = st.number_input("Budget USD", min_value=0.01, value=float(os.getenv("BUDGET_USD","1.0")))
poll = st.number_input("Poll seconds", min_value=1, max_value=10, value=int(os.getenv("POLL_SECONDS","2")))

st.subheader("Current status")
c1, c2, c3 = st.columns(3)
c1.metric("Mode", mode)
c2.metric("Ticker (env)", os.getenv("TICKER","(not set)"))
c3.metric("Budget (env)", os.getenv("BUDGET_USD","(not set)"))

st.info(
    "This website is a dashboard. Your live trading bot should run as a separate Render Background Worker.\n\n"
    "To change live settings, update your Render Environment Variables (TICKER, BUDGET_USD, etc.) and redeploy the worker."
)
