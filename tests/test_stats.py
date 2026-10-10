from faultline.stats import journey_stats, percentile
from faultline.store import Store


def test_percentile_empty():
    assert percentile([], 95) is None


def test_percentile_single():
    assert percentile([7.0], 50) == 7.0
    assert percentile([7.0], 95) == 7.0


def test_percentile_nearest_rank():
    v = list(range(1, 101))
    assert percentile(v, 50) == 50
    assert percentile(v, 95) == 95
    assert percentile([1, 2, 3, 4], 50) == 2


def test_empty_window_returns_none():
    s = Store(":memory:")
    st = journey_stats(s, "home")
    assert st.count == 0 and st.success_rate is None and st.p50 is None and st.p95 is None


def test_all_failures():
    s = Store(":memory:")
    for i in range(5):
        s.insert_sample(100 + i, i, "home", False, None, 5000.0 + i, "timeout")
    st = journey_stats(s, "home")
    assert st.count == 5 and st.success_rate == 0.0 and st.p95 == 5004.0
