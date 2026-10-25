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


def test_round_trip(tmp_path, key):
    r = row("AAPL", date(2024, 3, 1))
    seed(tmp_path, key, [r])
    assert store.read_row(key, "aapl", date(2024, 3, 1), cache_root=tmp_path) == r


def test_round_trip_paths(tmp_path):
    card = make_card(output_type="paths")
    k = model_key(card, ())
    r = row("AAPL", date(2024, 3, 1), paths=True)
    store.write_rows(k, card, {}, [r], covariate_set=(), cache_root=tmp_path)
    assert store.read_row(k, "AAPL", date(2024, 3, 1), cache_root=tmp_path) == r


def test_point_in_time_latest_not_after_cutoff(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.01), row("AAPL", date(2024, 3, 4), 0.02),
                         row("AAPL", date(2024, 3, 5), 0.03)])
    got = store.read_row(key, "AAPL", date(2024, 3, 4), cache_root=tmp_path)
    assert got.session == date(2024, 3, 4)
    got = store.read_row(key, "AAPL", date(2024, 3, 3), cache_root=tmp_path)   # weekend cutoff -> Friday row
    assert got.session == date(2024, 3, 1)


def test_future_row_never_returned(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 5))])
    with pytest.raises(store.ForecastStoreMiss):
        store.read_row(key, "AAPL", date(2024, 3, 4), cache_root=tmp_path)


def test_misses_raise(tmp_path, key):
    with pytest.raises(store.ForecastStoreMiss):          # no store at all
        store.read_row(key, "AAPL", date(2024, 3, 4), cache_root=tmp_path)
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(store.ForecastStoreMiss):          # unknown symbol
        store.read_row(key, "MSFT", date(2024, 3, 4), cache_root=tmp_path)


def test_stale_row_raises(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(store.ForecastStoreMiss):
        store.read_row(key, "AAPL", date(2024, 4, 1), cache_root=tmp_path)
    assert store.read_row(key, "AAPL", date(2024, 4, 1), cache_root=tmp_path, max_age_days=40).session == date(2024, 3, 1)


def test_cutoff_must_be_plain_date(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    with pytest.raises(TypeError):
        store.read_row(key, "AAPL", datetime(2024, 3, 4, 10), cache_root=tmp_path)


def test_upsert_by_session_and_cache_invalidates(tmp_path, key):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.01)])
    assert store.read_row(key, "AAPL", date(2024, 3, 4), cache_root=tmp_path).decile(5, 50) == pytest.approx(0.01)
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1), 0.05), row("AAPL", date(2024, 3, 4), 0.07)])
    # the per-process cache must notice the rewrite without an explicit reset
    assert store.read_row(key, "AAPL", date(2024, 3, 1), cache_root=tmp_path).decile(5, 50) == pytest.approx(0.05)
    assert store.read_row(key, "AAPL", date(2024, 3, 4), cache_root=tmp_path).decile(5, 50) == pytest.approx(0.07)


def test_cache_is_used_and_resettable(tmp_path, key, monkeypatch):
    seed(tmp_path, key, [row("AAPL", date(2024, 3, 1))])
    store.read_row(key, "AAPL", date(2024, 3, 1), cache_root=tmp_path)
    calls = []
    import pandas as pd
    orig = pd.read_parquet
    monkeypatch.setattr(pd, "read_parquet", lambda *a, **k: calls.append(1) or orig(*a, **k))
    store.read_row(key, "AAPL", date(2024, 3, 1), cache_root=tmp_path)
    assert calls == []
    store.reset_cache()
    store.read_row(key, "AAPL", date(2024, 3, 1), cache_root=tmp_path)
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
        store.read_row(key, "../x", date(2024, 3, 1), cache_root=tmp_path)


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
        store.read_row(key, "AAPL", date(2024, 3, 1), cache_root=tmp_path)
