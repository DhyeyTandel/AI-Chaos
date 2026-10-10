from faultline.store import Store


def test_round_trip_and_window(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.insert_sample(10.0, 1.0, "home", True, 200, 12.5)
    s.insert_sample(20.0, 2.0, "home", False, None, 5000.0, "ReadTimeout")
    s.insert_sample(20.0, 2.0, "cart", True, 200, 30.0)
    assert len(s.query()) == 3
    assert len(s.query(journey="home")) == 2
    rows = s.query(journey="home", since=15.0)
    assert len(rows) == 1 and rows[0]["ok"] == 0 and rows[0]["error"] == "ReadTimeout"
    assert rows[0]["status"] is None
    assert s.query(journey="home", until=12.0)[0]["latency_ms"] == 12.5
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
