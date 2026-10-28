import json
from datetime import date, datetime

import pytest

from ba2_common.core.ml_forecast import store
from ba2_common.core.ml_forecast.forecast_row import SL_GRID_PCT, TP_GRID_PCT, ForecastRow
from ba2_common.core.ml_forecast.model_card import model_key
from .test_ml_forecast_model_card import make_card

HZ = (5, 10, 20)


def deciles(c=0.01):
    return tuple(c + 0.01 * (i - 4) for i in range(9))


def row(sym, d, c=0.01, paths=False):
    pp = None
    if paths:
        pp = {(x, h, tp, sl): 0.5 for x in ("long", "short") for h in HZ for tp in TP_GRID_PCT for sl in SL_GRID_PCT}
    return ForecastRow(sym, d, {h: deciles(c) for h in HZ}, pp)


@pytest.fixture(autouse=True)
def _reset():
    store.reset_cache()
    yield
    store.reset_cache()


@pytest.fixture
def key():
    return model_key(make_card(), ())


def seed(tmp_path, key, rows, card=None, sym_rows=None):
    store.write_rows(key, card or make_card(), {"build": "t"}, rows, covariate_set=(), cache_root=tmp_path)


def test_layout_and_meta(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    assert (tmp_path / "ml_forecast" / key / "AAPL.parquet").is_file()
    meta = json.loads((tmp_path / "ml_forecast" / key / "meta.json").read_text())
    assert meta["card"] == make_card().to_dict() and meta["build"] == {"build": "t"} and meta["model_key"] == key
    assert meta["context_length"] == 512 and meta["covariate_set"] == [] and meta["horizons"] == list(HZ)


def expect_h5(r):
    """What a horizon-5 read returns: that horizon's deciles (and its path cells) only."""
    pp = None if r.path_probs is None else {k: v for k, v in r.path_probs.items() if k[1] == 5}
    return ForecastRow(r.symbol, r.session, {5: r.deciles[5]}, pp)


def assert_rows_close(a, b):
    assert (a.symbol, a.session, a.horizons, a.has_paths) == (b.symbol, b.session, b.horizons, b.has_paths)
    for h in a.horizons:
        assert a.deciles[h] == pytest.approx(b.deciles[h], abs=1e-6)
    if a.has_paths:
        assert a.path_probs.keys() == b.path_probs.keys()
        for k in a.path_probs:
            assert a.path_probs[k] == pytest.approx(b.path_probs[k], abs=1e-6)


def test_round_trip(tmp_path, key):
    r = row("AAPL", date(2024, 3, 1))
    seed(tmp_path, key, [r])
    assert_rows_close(store.read_row(key, "aapl", date(2024, 3, 1), horizon=5, cache_root=tmp_path), expect_h5(r))
    h20 = store.read_row(key, "AAPL", date(2024, 3, 1), horizon=20, cache_root=tmp_path)
    assert h20.horizons == (20,)
    with pytest.raises(ValueError):
        store.read_row(key, "AAPL", date(2024, 3, 1), horizon=7, cache_root=tmp_path)    # not a card horizon


def test_round_trip_paths(tmp_path):
    card = make_card(output_type="paths")
    k = model_key(card, ())
    r = row("AAPL", date(2024, 3, 1), paths=True)
    store.write_rows(k, card, {}, [r], covariate_set=(), cache_root=tmp_path)
    assert_rows_close(store.read_row(k, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path), expect_h5(r))


def test_point_in_time_latest_not_after_cutoff(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.01), row("AAPL", date(2024, 3, 4), 0.02),
                         row("AAPL", date(2024, 3, 5), 0.03)])
    got = store.read_row(key, "AAPL", date(2024, 3, 4), horizon=5, cache_root=tmp_path)
    assert got.session == date(2024, 3, 4)
    got = store.read_row(key, "AAPL", date(2024, 3, 3), horizon=5, cache_root=tmp_path)   # weekend cutoff -> Friday row
    assert got.session == date(2024, 3, 1)


def test_future_row_never_returned(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 5))])
    with pytest.raises(store.ForecastStoreMiss):
        store.read_row(key, "AAPL", date(2024, 3, 4), horizon=5, cache_root=tmp_path)


def test_misses_raise(tmp_path, key):
    with pytest.raises(store.ForecastStoreMiss):          # no store at all
        store.read_row(key, "AAPL", date(2024, 3, 4), horizon=5, cache_root=tmp_path)
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(store.ForecastStoreMiss):          # unknown symbol
        store.read_row(key, "MSFT", date(2024, 3, 4), horizon=5, cache_root=tmp_path)


def test_stale_row_raises(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(store.ForecastStoreMiss):
        store.read_row(key, "AAPL", date(2024, 4, 1), horizon=5, cache_root=tmp_path)
    assert store.read_row(key, "AAPL", date(2024, 4, 1), horizon=5, cache_root=tmp_path, max_age_days=40).session == date(2024, 3, 1)


def test_cutoff_must_be_plain_date(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(TypeError):
        store.read_row(key, "AAPL", datetime(2024, 3, 4, 10), horizon=5, cache_root=tmp_path)


def test_upsert_by_session_and_cache_invalidates(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.01)])
    assert store.read_row(key, "AAPL", date(2024, 3, 4), horizon=5, cache_root=tmp_path).decile(5, 50) == pytest.approx(0.01)
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.05), row("AAPL", date(2024, 3, 4), 0.07)])
    # the per-process cache must notice the rewrite without an explicit reset
    assert store.read_row(key, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path).decile(5, 50) == pytest.approx(0.05)
    assert store.read_row(key, "AAPL", date(2024, 3, 4), horizon=5, cache_root=tmp_path).decile(5, 50) == pytest.approx(0.07)


def test_cache_is_used_and_resettable(tmp_path, key, monkeypatch):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    store.read_row(key, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path)
    calls = []
    import pandas as pd
    orig = pd.read_parquet
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: calls.append(1) or orig(*a, **k))
    store.read_row(key, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path)
    assert calls == []
    store.reset_cache()
    store.read_row(key, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path)
    assert calls == [1]


def test_refuses_different_card_under_same_key(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    other = make_card(version="9.9")
    with pytest.raises(ValueError):                    # key no longer matches the card
        seed(tmp_path, key, [row("AAPL", date(2024, 3, 4))], card=other)
    mp = tmp_path / "ml_forecast" / key / "meta.json"
    meta = json.loads(mp.read_text()); meta["card"] = other.to_dict(); mp.write_text(json.dumps(meta))
    with pytest.raises(store.ForecastStoreCardMismatch):   # a different card already stored under this key
        seed(tmp_path, key, [row("AAPL", date(2024, 3, 4))])


def test_writer_checks_rows_against_card(tmp_path, key):
    bad = ForecastRow("AAPL", date(2024, 3, 1), {5: deciles()})    # horizons differ from the card
    with pytest.raises(ValueError):
        seed(tmp_path, key, [bad])
    with pytest.raises(ValueError):                                # paths row for a deciles card
        seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), paths=True)])
    with pytest.raises(ValueError):                                # empty
        seed(tmp_path, key, [])


def test_symbol_path_safety(tmp_path, key):
    with pytest.raises(ValueError):
        store.read_row(key, "../x", date(2024, 3, 1), horizon=5, cache_root=tmp_path)


def test_write_rejects_key_that_does_not_match_card_and_covariates(tmp_path, key):
    with pytest.raises(ValueError):
        store.write_rows("deadbeefdeadbeef", make_card(), {}, [row("AAPL", date(2024, 3, 1))],
                         covariate_set=(), cache_root=tmp_path)
    with pytest.raises(ValueError):                    # key was built for the empty covariate set
        store.write_rows(key, make_card(), {}, [row("AAPL", date(2024, 3, 1))],
                         covariate_set=("SPY",), cache_root=tmp_path)


def test_read_recomputes_key_from_meta(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    mp = tmp_path / "ml_forecast" / key / "meta.json"
    meta = json.loads(mp.read_text()); meta["covariate_set"] = ["SPY"]; mp.write_text(json.dumps(meta))
    store.reset_cache()
    with pytest.raises(store.ForecastStoreError):
        store.read_row(key, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path)


def test_reads_only_needed_columns_as_float32(tmp_path, key, monkeypatch):
    import numpy as np
    import pandas as pd
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    seen = []
    orig = pd.read_parquet
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: seen.append(k.get("columns")) or orig(*a, **k))
    store.read_row(key, "AAPL", date(2024, 3, 1), horizon=10, cache_root=tmp_path)
    assert seen == [["session"] + [f"h10_q{q}" for q in range(10, 100, 10)]]
    entry = next(iter(next(iter(store._frames.values())).values()))
    assert entry.dec.dtype == np.float32 and entry.sessions.dtype == np.dtype("datetime64[D]")


def test_paths_columns_limited_to_the_horizon(tmp_path):
    import pandas as pd
    card = make_card(output_type="paths")
    k = model_key(card, ())
    store.write_rows(k, card, {}, [row("AAPL", date(2024, 3, 1), paths=True)], covariate_set=(), cache_root=tmp_path)
    seen = []
    orig = pd.read_parquet
    import ba2_common.core.ml_forecast.store as st
    pd.read_parquet = lambda *a, **kw: seen.append(kw["columns"]) or orig(*a, **kw)
    try:
        st.read_row(k, "AAPL", date(2024, 3, 1), horizon=10, cache_root=tmp_path)
    finally:
        pd.read_parquet = orig
    assert all(c == "session" or c.startswith("h10_") or "_h10_" in c for c in seen[0])
    assert any(c.startswith("p_short_h10_") for c in seen[0])


def test_lru_bound_evicts_least_recently_used(tmp_path, key, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(store, "MAX_CACHED_FRAMES_PER_KEY_HORIZON", 2)
    seed(tmp_path, key, [row(s, date(2024, 3, 1)) for s in ("AAA", "BBB", "CCC")])
    store.reset_cache()
    calls = []
    orig = pd.read_parquet
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: calls.append(1) or orig(*a, **k))
    rd = lambda s: store.read_row(key, s, date(2024, 3, 1), horizon=5, cache_root=tmp_path)
    rd("AAA"); rd("BBB"); rd("AAA"); rd("CCC")          # BBB is the least recently used -> evicted
    assert len(calls) == 3
    rd("AAA"); rd("CCC")
    assert len(calls) == 3
    rd("BBB")
    assert len(calls) == 4


def test_max_age_days_validated(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            store.read_row(key, "AAPL", date(2024, 3, 1), horizon=5, cache_root=tmp_path, max_age_days=bad)


def test_meta_written_after_parquet(tmp_path, key, monkeypatch):
    import pandas as pd

    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    with pytest.raises(OSError):
        seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    assert not (tmp_path / "ml_forecast" / key / "meta.json").exists()    # a crash never leaves meta without data


def test_read_session_is_the_latest_fresh_row_of_each_universe_symbol_at_the_cutoff(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.01), row("AAPL", date(2024, 3, 5), 0.02),
                         row("AAPL", date(2024, 3, 20), 0.09),            # after the cutoff: never returned
                         row("MSFT", date(2024, 3, 4), 0.03),
                         row("OLD", date(2024, 2, 20)),                   # 17 days before the cutoff: stale
                         row("NVDA", date(2024, 3, 25)),                  # first session after the cutoff: absent
                         row("LOCKED", date(2024, 3, 6), 0.05)])          # in the store, NOT in the universe
    universe = ["NVDA", "MSFT", "AAPL", "OLD"]
    out = store.read_session(key, date(2024, 3, 8), 5, universe, tmp_path)
    assert list(out) == ["AAPL", "MSFT"]                                  # sorted universe order, no LOCKED
    assert out["AAPL"].session == date(2024, 3, 5) and out["MSFT"].session == date(2024, 3, 4)
    assert_rows_close(out["AAPL"], expect_h5(row("AAPL", date(2024, 3, 5), 0.02)))
    assert all(r.session <= date(2024, 3, 8) for r in out.values())
    # agrees with read_row symbol by symbol, staleness included
    for sym in ("AAPL", "MSFT"):
        assert out[sym] == store.read_row(key, sym, date(2024, 3, 8), 5, tmp_path)
    for sym in ("OLD", "NVDA"):
        with pytest.raises(store.ForecastStoreMiss):
            store.read_row(key, sym, date(2024, 3, 8), 5, tmp_path)
    # the staleness window is the caller's
    assert list(store.read_session(key, date(2024, 3, 8), 5, universe, tmp_path, max_age_days=30)) == \
        ["AAPL", "MSFT", "OLD"]
    assert store.read_session(key, date(2024, 2, 1), 5, universe, tmp_path) == {}


def test_read_session_ignores_extra_files_and_refuses_a_missing_one(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1)), row("MSFT", date(2024, 3, 1)), row("EXTRA", date(2024, 3, 1))])
    assert list(store.read_session(key, date(2024, 3, 1), 5, ["MSFT", "AAPL"], tmp_path)) == ["AAPL", "MSFT"]
    with pytest.raises(store.ForecastStoreError, match="NOFILE") as ei:
        store.read_session(key, date(2024, 3, 1), 5, ["AAPL", "NOFILE"], tmp_path)
    assert not isinstance(ei.value, store.ForecastStoreMiss)        # an incomplete store, not a quiet absence
    for bad in ([], (), "AAPL"):
        with pytest.raises((ValueError, TypeError)):
            store.read_session(key, date(2024, 3, 1), 5, bad, tmp_path)


def test_read_session_validates_like_read_row(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(TypeError):
        store.read_session(key, datetime(2024, 3, 1), 5, ["AAPL"], tmp_path)
    with pytest.raises(ValueError):
        store.read_session(key, date(2024, 3, 1), 7, ["AAPL"], tmp_path)
    with pytest.raises(ValueError):
        store.read_session(key, date(2024, 3, 1), 5, ["AAPL"], tmp_path, max_age_days=0)
    with pytest.raises(ValueError, match="unsafe"):
        store.read_session(key, date(2024, 3, 1), 5, ["../x"], tmp_path)


def test_read_session_reuses_the_symbol_cache(tmp_path, key, monkeypatch):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1)), row("MSFT", date(2024, 3, 1))])
    store.read_session(key, date(2024, 3, 1), 5, ["AAPL", "MSFT"], tmp_path)
    import pandas as pd
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: pytest.fail("parquet re-read despite the LRU"))
    assert sorted(store.read_session(key, date(2024, 3, 1), 5, ["AAPL", "MSFT"], tmp_path)) == ["AAPL", "MSFT"]


def test_load_score_entries_reads_only_the_score_columns_never_the_path_cells(tmp_path, monkeypatch):
    k = model_key(make_card(output_type="paths"), ())
    seed(tmp_path, k, [row("AAPL", date(2024, 3, 1), paths=True)], card=make_card(output_type="paths"))
    import pandas as pd
    real = pd.read_parquet
    seen = []

    def spy(p, columns=None, **kw):
        seen.append(list(columns))
        return real(p, columns=columns, **kw)

    monkeypatch.setattr(pd, "read_parquet", spy)
    with_classes, ent = store.load_score_entries(k, 5, ["AAPL"], tmp_path)
    assert not with_classes and ent["AAPL"][1].shape == (1, 9) and ent["AAPL"][1].dtype.name == "float32"
    assert len(seen) == 1 and len(seen[0]) == 10 and not any(c.startswith("p_") for c in seen[0])
    with pytest.raises(store.ForecastStoreError, match="NOFILE"):
        store.load_score_entries(k, 5, ["NOFILE"], tmp_path)


def test_root_is_public_and_store_stamp_changes_with_the_meta_file(tmp_path, key):
    import os
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    assert store.root(tmp_path) == tmp_path / "ml_forecast"
    a = store.store_stamp(key, tmp_path)
    p = tmp_path / "ml_forecast" / key / "meta.json"
    os.utime(p, ns=(a, a + 1_000_000))
    assert store.store_stamp(key, tmp_path) != a
