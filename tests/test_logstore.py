import logging

from backend.logstore import LogStore, categorize


def _rec(name, level, msg):
    return logging.LogRecord(name, level, "f.py", 1, msg, None, None)


def test_categorize():
    assert categorize("bot_engine", "group 3 opened long_residual mode=paper") == "trade"
    assert categorize("bot_engine", "group 3 trade #7 closed mode=paper") == "trade"
    assert categorize("bot_engine", "entry scan: 4 group(s)") == "engine"
    assert categorize("xt", "x") == "exchange"
    assert categorize("telegram.cmd", "x") == "telegram"
    assert categorize("whatever", "x") == "other"


def test_query_filters_and_pagination(tmp_path):
    st = LogStore(tmp_path / "l.jsonl", size=100)
    st.emit(_rec("bot_engine", logging.INFO, "group 1 opened long_residual"))
    st.emit(_rec("bot_engine", logging.WARNING, "group 2: window too small"))
    st.emit(_rec("xt", logging.ERROR, "rate limit hit"))
    st.emit(_rec("uvicorn.access", logging.INFO, "GET /api/logs"))  # skipped
    assert st.query()["total"] == 3
    assert [r["cat"] for r in st.query(category="trade")["items"]] == ["trade"]
    assert st.query(level="WARNING")["matched"] == 2
    assert st.query(q="RATE limit")["matched"] == 1
    assert st.query(q="window nothere")["matched"] == 0
    assert st.query(group_id=2)["items"][0]["group_id"] == 2
    page = st.query(limit=2)
    assert page["has_more"] and [r["id"] for r in page["items"]] == [3, 2]
    assert st.query(before_id=2)["items"][0]["id"] == 1
    assert st.query(after_id=2)["items"][0]["id"] == 3


def test_persists_across_restart_and_rotates(tmp_path):
    p = tmp_path / "l.jsonl"
    st = LogStore(p, size=100)
    st.emit(_rec("bot_engine", logging.INFO, "group 1 opened x"))
    st.close()
    st2 = LogStore(p, size=100)
    assert st2.query()["total"] == 1
    st2.emit(_rec("bot_engine", logging.INFO, "next"))
    assert st2.query()["items"][0]["id"] == 2  # ids keep increasing
    small = LogStore(tmp_path / "r.jsonl", size=100, max_bytes=300)
    for i in range(20):
        small.emit(_rec("bot_engine", logging.INFO, f"line {i} " + "x" * 40))
    assert (tmp_path / "r.jsonl.1").exists()
    assert small.query()["total"] == 20
