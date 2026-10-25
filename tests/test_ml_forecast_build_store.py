"""build_store: point-in-time contexts, skips, resume, store round trip. Stub model, no torch."""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ml_forecast import build_store as bs  # noqa: E402
from tools.ml_forecast.adapters.timesfm3 import TimesFM3Adapter, timesfm3_card  # noqa: E402
from ba2_common.core.ml_forecast import store  # noqa: E402
from ba2_common.core.ml_forecast.model_card import model_key  # noqa: E402

QS = np.array([0.99, 0.995, 0.998, 0.999, 1.0, 1.001, 1.002, 1.005, 1.01])
CARD = timesfm3_card(512)
KEY = model_key(CARD, ())


class Spy:
    """Stub model that records every context it was shown, and forecasts a function of the last close only."""
    def __init__(self):
        self.contexts = []
        self.calls = 0

    def __call__(self, ctxs, horizon):
        self.calls += 1
        self.contexts.extend(ctxs)
        return [float(c[-1]) * (1 + (QS[None, :] - 1) * np.arange(1, horizon + 1)[:, None]) for c in ctxs]


def make_bars(n, start="2022-01-03"):
    dates = pd.bdate_range(start, periods=n)
    return dates, 100.0 + np.arange(n, dtype=float)       # close value encodes the position


@pytest.fixture
def cache(tmp_path):
    store.reset_cache()
    d = tmp_path / bs.OHLCV_DIRNAME
    d.mkdir()
    for sym, n in (("AAA", 700), ("BBB", 700), ("NEW", 300)):    # NEW never reaches 512 bars before the window ends
        dates, close = make_bars(n, "2023-02-01" if sym == "NEW" else "2022-01-03")
        pd.DataFrame({"Date": dates, "Close": close}).to_parquet(d / f"{sym}_1d.parquet")
    yield tmp_path
    store.reset_cache()


def run(cache, spy, sessions=None, **kw):
    data, _ = bs.load_daily_closes(cache, ["AAA", "BBB", "NEW"])
    if sessions is None:
        sessions = bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 12), "all")
    ad = TimesFM3Adapter(CARD, spy)
    return bs.build(CARD, data, sessions, ad, cache, **kw), data, sessions


def test_point_in_time_and_round_trip(cache):
    spy = Spy()
    st, data, sessions = run(cache, spy, flush_sessions=3)
    assert len(sessions) == 9 and st.forecasts == 18
    # every context ends exactly at its session: the close value encodes the bar position
    d, c = data["AAA"]
    for ctx in spy.contexts:
        assert len(ctx) == 512
    for sess in sessions:
        row = store.read_row(KEY, "AAA", sess, 5, cache)
        assert row.session == sess
        idx = int(np.searchsorted(d, np.datetime64(sess, "D")))
        last = float(np.float32(c[idx]))
        assert row.decile(5, 90) == pytest.approx(0.05, abs=1e-5)       # (1.01-1)*5, scale free
        assert last == pytest.approx(100.0 + idx)
    # a decision cutoff the day before the first session sees nothing
    with pytest.raises(store.ForecastStoreMiss):
        store.read_row(KEY, "AAA", date(2024, 1, 1), 5, cache)
    # the scan cutoff = next session reads the row of session S
    assert store.read_row(KEY, "BBB", date(2024, 1, 3), 10, cache).session == date(2024, 1, 3)


def test_no_future_bar_is_ever_shown(cache):
    """Poison every bar after the session: the forecast must not change."""
    spy1 = Spy()
    data, _ = bs.load_daily_closes(cache, ["AAA"])
    d, c = data["AAA"]
    sess = date(2024, 1, 5)
    ctx1, _ = bs.context_for(d, c, sess, 512)
    c2 = c.copy()
    c2[np.searchsorted(d, np.datetime64(sess, "D"), side="right"):] = 1e9
    ctx2, _ = bs.context_for(d, c2, sess, 512)
    np.testing.assert_array_equal(ctx1, ctx2)
    assert ctx1[-1] == c[np.searchsorted(d, np.datetime64(sess, "D"))]


def test_short_history_and_no_bar_are_counted_not_forecast(cache):
    spy = Spy()
    st, _, sessions = run(cache, spy)
    assert st.skipped_short_history == len(sessions)          # NEW: < 512 bars
    assert st.skipped_no_bar == 0
    with pytest.raises(store.ForecastStoreMiss):
        store.read_row(KEY, "NEW", date(2024, 1, 5), 5, cache)
    # a session that is not a bar of the symbol
    d, c = bs.load_daily_closes(cache, ["AAA"])[0]["AAA"]
    assert bs.context_for(d, c, date(2024, 1, 6), 512) == (None, "no_bar")     # a Saturday


def test_resume_skips_stored_sessions(cache):
    spy = Spy()
    _, data, sessions = run(cache, spy, sessions=None)
    n_first = len(spy.contexts)
    spy2 = Spy()
    st2, _, _ = run(cache, spy2, resume=True)
    assert spy2.calls == 0 and st2.forecasts == 0 and st2.skipped_resume == 2 * len(sessions)
    # extend the window: only the new sessions are computed
    more = bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 19), "all")
    spy3 = Spy()
    st3, _, _ = run(cache, spy3, sessions=more, resume=True)
    assert st3.forecasts == 2 * (len(more) - len(sessions))
    assert store.read_row(KEY, "AAA", date(2024, 1, 19), 5, cache).session == date(2024, 1, 19)
    assert n_first == 2 * len(sessions)


def test_without_resume_recomputes(cache):
    run(cache, Spy())
    spy = Spy()
    st, _, sessions = run(cache, spy, resume=False)
    assert st.forecasts == 2 * len(sessions)


def test_weekly_sessions_are_last_union_date_per_iso_week(cache):
    data, _ = bs.load_daily_closes(cache, ["AAA"])
    wk = bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 19), "all")
    wk = bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 19), "weekly")
    assert wk == [date(2024, 1, 5), date(2024, 1, 12), date(2024, 1, 19)]
    with pytest.raises(ValueError):
        bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 19), "monthly")


def test_parse_symbols():
    assert bs.parse_symbols("aapl, msft\n# c\nNVDA # x\nAAPL") == ["AAPL", "MSFT", "NVDA"]
    with pytest.raises(ValueError):
        bs.parse_symbols("# nothing")


def test_timesfm_is_deterministic_if_installed():
    pytest.importorskip("timesfm3")
    from tools.ml_forecast.adapters.timesfm3 import load_timesfm3_predict_fn
    predict = load_timesfm3_predict_fn(threads=6, batch_size=8)
    rng = np.random.default_rng(0)
    x = [np.exp(np.cumsum(rng.normal(0, 0.01, 512))).astype(np.float32) * 100 for _ in range(3)]
    a = predict(x, 20)
    b = predict(x, 20)
    for u, v in zip(a, b):
        np.testing.assert_array_equal(u, v)


# ---------------------------------------------------------------- review fixes
def test_rows_are_flushed_when_the_build_aborts(cache):
    """RAM guard / Ctrl-C / any exception: buffered rows reach the store, so --resume loses nothing."""
    data, _ = bs.load_daily_closes(cache, ["AAA", "BBB"])
    sessions = bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 12), "all")
    calls = {"n": 0}

    def guard():
        calls["n"] += 1
        if calls["n"] == 5:
            raise MemoryError("free RAM too low")
    with pytest.raises(MemoryError):
        bs.build(CARD, data, sessions, TimesFM3Adapter(CARD, Spy()), cache, flush_sessions=100, guard=guard)
    assert store.read_row(KEY, "AAA", date(2024, 1, 5), 5, cache).session == date(2024, 1, 5)
    st, _, _ = run(cache, Spy(), resume=True)       # resume completes the rest, recomputing only what is missing
    assert st.skipped_resume >= 4


def test_card_mismatch_is_refused_before_any_compute(cache):
    run(cache, Spy())
    meta_p = cache / store.STORE_DIRNAME / KEY / "meta.json"
    import json
    m = json.loads(meta_p.read_text())
    m["card"]["version"] = "9.9.9"
    meta_p.write_text(json.dumps(m))
    store.reset_cache()
    spy = Spy()
    with pytest.raises(store.ForecastStoreError):
        run(cache, spy, resume=True)
    assert spy.calls == 0


def test_only_input_data_errors_are_counted_model_errors_abort(cache):
    data, _ = bs.load_daily_closes(cache, ["AAA", "BBB"])
    d, c = data["BBB"]
    c = c.copy()
    c[np.searchsorted(d, np.datetime64("2024-01-02")) - 10] = -5.0      # inside every context of BBB
    data["BBB"] = (d, c)
    st = bs.build(CARD, data, [date(2024, 1, 2)], TimesFM3Adapter(CARD, Spy()), cache, batch_size=2)
    assert st.forecasts == 1 and st.invalid_forecasts == 1 and "BBB" in st.invalid[0]

    def crossed(ctxs, h):
        out = Spy()(ctxs, h)
        out[0][:, [3, 4]] = out[0][:, [4, 3]]
        return out
    data2, _ = bs.load_daily_closes(cache, ["AAA"])
    with pytest.raises(ValueError, match="monotone"):
        bs.build(CARD, data2, [date(2024, 1, 3)], TimesFM3Adapter(CARD, crossed), cache)

    def bad_shape(ctxs, h):
        return [np.ones((h, 5)) for _ in ctxs]
    with pytest.raises(ValueError, match="shape"):
        bs.build(CARD, data2, [date(2024, 1, 4)], TimesFM3Adapter(CARD, bad_shape), cache)


def test_exit_code_follows_max_invalid_share():
    st = bs.BuildStats(forecasts=99, invalid_forecasts=1)
    assert bs.exit_code(st, 0.0) == 1
    assert bs.exit_code(st, 0.02) == 0
    assert bs.exit_code(bs.BuildStats(forecasts=5), 0.0) == 0


def test_single_writer_lock(cache):
    kdir = cache / store.STORE_DIRNAME / KEY
    with bs.store_lock(cache, KEY):
        assert (kdir / ".lock").exists()
        with pytest.raises(bs.StoreLocked, match=r"pid \d+"):
            with bs.store_lock(cache, KEY):
                pass
        assert (kdir / ".lock").exists()          # the failed attempt did not steal or delete it
    assert not (kdir / ".lock").exists()
    # build() holds the lock and releases it on abort
    data, _ = bs.load_daily_closes(cache, ["AAA"])
    with pytest.raises(MemoryError):
        bs.build(CARD, data, [date(2024, 1, 2)], TimesFM3Adapter(CARD, Spy()), cache,
                 guard=lambda: (_ for _ in ()).throw(MemoryError("x")))
    assert not (kdir / ".lock").exists()
