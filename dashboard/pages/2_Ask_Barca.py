import os

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Ask Barça", page_icon="💬", layout="wide")
st.title("Ask Barça")
st.caption("Ask in plain English. A local language model writes a read-only SQL query over the warehouse, "
           "and the answer comes from the rows it returns. Expand 'SQL used' to check its work.")

ASSISTANT_URL = os.environ.get("ASSISTANT_URL", "http://assistant:8000")
START_HINT = "Start it from the repo folder:  `docker compose --profile assistant up -d`"
EXAMPLES = [
    "Who scored the most goals for Barcelona in 2020/2021?",
    "What was Barcelona's win-draw-loss record at home in 2020/2021?",
    "Top 5 Barcelona players by tackles per 90 in 2020/2021",
    "How many penalties did Messi take and score?",
]


def health():
    try:
        return requests.get(f"{ASSISTANT_URL}/health", timeout=2).json()
    except (requests.RequestException, ValueError):
        return None


with st.sidebar:
    st.subheader("Assistant status")
    h = health()
    if h is None:
        st.error("The assistant service is not running.")
        st.markdown(START_HINT)
    else:
        st.write(f"Database: **{h['database']}**")
        if h.get("views") == "missing":
            st.warning("The assistant's views do not exist yet. Create them once:")
            st.code("docker compose exec postgres psql -U barca -d barca_dw -v ON_ERROR_STOP=1 "
                    "-f /models/semantic_views.sql", language="bash")
        llm = {"ok": "ready", "model_missing": "model not downloaded", "unreachable": "Ollama not reachable"}.get(h["llm"], h["llm"])
        st.write(f"Model `{h['model']}`: **{llm}**")
        if h["llm"] == "model_missing":
            st.code(f"docker compose exec ollama ollama pull {h['model']}", language="bash")
    if st.button("Clear conversation"):
        st.session_state.pop("chat", None)
        st.rerun()

chat = st.session_state.setdefault("chat", [])


def render(entry):
    st.markdown(entry["answer"])
    if entry.get("rows"):
        st.dataframe(pd.DataFrame(entry["rows"], columns=entry["columns"]), use_container_width=True, hide_index=True)
        if entry.get("truncated"):
            st.caption("Only the first 200 rows are shown.")
    if entry.get("sql"):
        with st.expander("SQL used"):
            st.code(entry["sql"], language="sql")
    if entry.get("attempts"):
        st.caption(f"The first attempt failed, so the assistant retried ({len(entry['attempts'])} failed attempt(s)).")


def ask(question):
    history = [{"question": e["question"], "sql": e["sql"]} for e in chat if e.get("sql")][-2:]
    try:
        r = requests.post(f"{ASSISTANT_URL}/ask", json={"question": question, "history": history}, timeout=900)
    except requests.ConnectionError:
        return {"answer": f"I can't reach the assistant service. {START_HINT}"}
    except requests.Timeout:
        return {"answer": "That took too long. A bigger model or a CPU-only machine can be slow; try again, "
                          "since the first question after a restart also has to load the model."}
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        return {"answer": f"Something went wrong: {detail}"}
    return r.json()


for entry in chat:
    with st.chat_message("user"):
        st.write(entry["question"])
    with st.chat_message("assistant"):
        render(entry)

question = st.session_state.pop("pending", None) or st.chat_input("Ask about players, matches or shots...")

if not chat and not question:
    st.write("Try one of these:")
    cols = st.columns(len(EXAMPLES))
    for col, example in zip(cols, EXAMPLES):
        if col.button(example, use_container_width=True):
            st.session_state["pending"] = example
            st.rerun()

if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        with st.spinner("Thinking... (the first question can take a minute while the model loads)"):
            result = ask(question)
        entry = {"question": question, **result}
        render(entry)
    chat.append(entry)
