import threading
from queue import Queue, Empty
import streamlit as st

import bot  # your bot.py file

st.set_page_config(page_title="Risk Guard", layout="wide")

st.title("Risk Guard (Demo)")

# Safety banner
st.warning("Demo mode recommended: DRY_RUN=True, ARM_LIVE=False")

# Controls
col1, col2, col3 = st.columns(3)
with col1:
    ticker = st.text_input("Ticker", value=bot.TICKER)
with col2:
    poll = st.number_input("Poll seconds", min_value=1, max_value=10, value=int(bot.POLL_SECONDS))
with col3:
    st.write("")

# Apply quick config changes (no refactor needed)
bot.TICKER = ticker
bot.POLL_SECONDS = int(poll)

if "stop_event" not in st.session_state:
    st.session_state.stop_event = threading.Event()
if "log_q" not in st.session_state:
    st.session_state.log_q = Queue()
if "thread" not in st.session_state:
    st.session_state.thread = None

c1, c2 = st.columns(2)
with c1:
    if st.button("▶️ Start"):
        if st.session_state.thread is None or not st.session_state.thread.is_alive():
            st.session_state.stop_event.clear()
            st.session_state.thread = threading.Thread(
                target=bot.run_bot,
                args=(st.session_state.stop_event, st.session_state.log_q),
                daemon=True,
            )
            st.session_state.thread.start()

with c2:
    if st.button("⏹ Stop"):
        st.session_state.stop_event.set()

st.subheader("Live Logs")
log_box = st.empty()

# Pull logs
logs = []
try:
    while True:
        logs.append(st.session_state.log_q.get_nowait())
except Empty:
    pass

if "all_logs" not in st.session_state:
    st.session_state.all_logs = []

st.session_state.all_logs.extend(logs)
st.session_state.all_logs = st.session_state.all_logs[-300:]  # keep last 300 lines

log_box.code("\n".join(st.session_state.all_logs) if st.session_state.all_logs else "No logs yet...")

st.caption("If the page looks frozen, press Stop, refresh, then Start again.")
