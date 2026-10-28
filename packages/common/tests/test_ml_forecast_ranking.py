"""The compact ranking panel against the reference path (store.read_session + mapping.bullish_score)."""
import random
from datetime import date, timedelta
from types import MappingProxyType

import pytest

from ba2_common.core.ml_forecast import mapping, ranking, store
from ba2_common.core.ml_forecast.forecast_row import ForecastRow
from ba2_common.core.ml_forecast.model_card import model_key
from .test_ml_forecast_model_card import make_card

SYMS = [f"S{i:02d}" for i in range(14)]


@pytest.fixture(autouse=True)
def _reset():
    store.reset_cache()
    ranking.reset_panels()
    yield
    store.reset_cache()
    ranking.reset_panels()


def slow_ranks(key, cutoff, horizon, universe, root, max_age=store.MAX_STALE_CALENDAR_DAYS):
    """The reference: read the rows, score them, order by (-P(up), -second, symbol)."""
    rows = store.read_session(key, cutoff, horizon, universe, root, max_age)
    scored = sorted(((mapping.bullish_score(r, horizon), s) for s, r in rows.items()),
                    key=lambda t: (-t[0][0], -t[0][1], t[1]))
    return {s: i + 1 for i, (_, s) in enumerate(scored)}, len(scored)


def _sessions(rng, start, n):
    """Business days with random gaps so some symbols go stale at some cutoffs."""
    out, d = [], start
    for _ in range(n):
        d += timedelta(days=rng.choice([1, 1, 1, 2, 3, 9]))
        out.append(d)
    return out


def decile_rows(rng, sym, sessions, horizons=(5, 10)):
    rows = []
    for d in sessions:
        c = rng.choice([-0.05, -0.02, 0.0, 0.01, 0.02, 0.03, 0.06, 0.09])   # repeated levels -> exact ties
        w = rng.choice([0.01, 0.02])
        rows.append(ForecastRow(sym, d, {h: tuple(c + w * (i - 4) for i in range(9)) for h in horizons}))
    return rows


def class_rows(rng, sym, sessions):
    rows = []
    for d in sessions:
        pu = rng.choice([0.2, 0.3, 0.5, 0.5, 0.6])
        pd_ = rng.choice([0.1, 0.2, 0.3])
        cf = ((pd_, -0.03), (1 - pu - pd_, 0.001), (pu, 0.03 + rng.choice([0, 0.01])))
        rows.append(ForecastRow(sym, d, {}, class_probs={3: cf}))
    return rows


def build(tmp_path, kind):
    rng = random.Random(7)
    if kind == "deciles":
        card, make = make_card(horizons=(5, 10)), decile_rows
    else:
        card = make_card(horizons=(3,), output_type="class_probs")
        make = lambda r, s, ss: class_rows(r, s, ss)          # noqa: E731
    key = model_key(card, ())
    rows = []
    for s in SYMS:
        rows += make(rng, s, _sessions(rng, date(2024, 1, 1), 40))
    store.write_rows(key, card, {"card_id": kind}, rows, (), tmp_path)
    return key, (5 if kind == "deciles" else 3), rng


@pytest.mark.parametrize("kind", ["deciles", "classes"])
def test_panel_ranks_equal_the_reference_on_random_cutoffs_and_universes(tmp_path, kind):
    key, h, rng = build(tmp_path, kind)
    full = SYMS
    last = date(2024, 1, 1) + timedelta(days=200)
    for _ in range(60):
        cutoff = date(2023, 12, 20) + timedelta(days=rng.randrange(0, (last - date(2023, 12, 20)).days))
        universe = full if rng.random() < 0.4 else rng.sample(full, rng.randrange(1, len(full) + 1))
        max_age = rng.choice([store.MAX_STALE_CALENDAR_DAYS, 3, 30])
        ranks, n = ranking.session_ranks(key, cutoff, h, universe, tmp_path, max_age)
        ref, ref_n = slow_ranks(key, cutoff, h, universe, tmp_path, max_age)
        assert (dict(ranks), n) == (ref, ref_n), (cutoff, max_age)


def test_panel_skips_stale_and_not_yet_started_symbols_and_is_read_only(tmp_path):
    key, h, _ = build(tmp_path, "deciles")
    ranks, n = ranking.session_ranks(key, date(2023, 12, 1), h, SYMS, tmp_path)     # before every session
    assert dict(ranks) == {} and n == 0
    ranks, n = ranking.session_ranks(key, date(2024, 12, 1), h, SYMS, tmp_path)     # long after: all stale
    assert n == 0
    ranks, n = ranking.session_ranks(key, date(2024, 2, 15), h, SYMS, tmp_path)
    assert isinstance(ranks, MappingProxyType) and n > 0
    assert sorted(ranks.values()) == list(range(1, n + 1))
    with pytest.raises(TypeError):
        ranks["S00"] = 1


def test_panel_ignores_extra_files_and_refuses_a_missing_one(tmp_path):
    key, h, _ = build(tmp_path, "deciles")
    cut = date(2024, 2, 15)
    sub = SYMS[:6]
    base = ranking.session_ranks(key, cut, h, sub, tmp_path)
    # the other eight files of the store do not matter, and neither does a new locked-holdout symbol
    card = make_card(horizons=(5, 10))
    store.write_rows(key, card, {"card_id": "deciles"},
                     [ForecastRow("LOCKED", date(2024, 2, 14), {x: tuple(0.2 + 0.01 * i for i in range(9)) for x in (5, 10)})],
                     (), tmp_path)
    assert (tmp_path / "ml_forecast" / key / "LOCKED.parquet").is_file()
    assert ranking.session_ranks(key, cut, h, sub, tmp_path) == base
    with pytest.raises(store.ForecastStoreError, match="MISSING"):
        ranking.session_ranks(key, cut, h, sub + ["MISSING"], tmp_path)
    with pytest.raises(ValueError):
        ranking.session_ranks(key, cut, h, [], tmp_path)


def test_panel_is_cached_per_universe_and_bounded(tmp_path, monkeypatch):
    key, h, _ = build(tmp_path, "deciles")
    builds = []
    real = ranking.RankingPanel.build.__func__
    monkeypatch.setattr(ranking.RankingPanel, "build",
                        classmethod(lambda cls, *a, **k: (builds.append(a), real(cls, *a, **k))[1]))
    cut = date(2024, 2, 15)
    for _ in range(3):
        ranking.session_ranks(key, cut, h, SYMS[:5], tmp_path)
        ranking.session_ranks(key, cut + timedelta(days=1), h, list(reversed(SYMS[:5])), tmp_path)   # same set
    assert len(builds) == 1
    for k in range(2, 2 + ranking.PANEL_CACHE_SIZE + 1):      # more distinct universes than the cache holds
        ranking.session_ranks(key, cut, h, SYMS[:k], tmp_path)
    assert len(ranking._panels) == ranking.PANEL_CACHE_SIZE


def test_panel_loads_no_path_cells_for_a_paths_store(tmp_path, monkeypatch):
    card = make_card(horizons=(5,), output_type="paths")
    key = model_key(card, ())
    from ba2_common.core.ml_forecast.forecast_row import SL_GRID_PCT, TP_GRID_PCT
    pp = {(x, 5, tp, sl): 0.5 for x in ("long", "short") for tp in TP_GRID_PCT for sl in SL_GRID_PCT}
    store.write_rows(key, card, {"card_id": "p"},
                     [ForecastRow(s, date(2024, 3, 1), {5: tuple(0.01 * (i - 3) for i in range(9))}, pp)
                      for s in ("AAA", "BBB")], (), tmp_path)
    import pandas as pd
    real = pd.read_parquet
    seen = []
    monkeypatch.setattr(pd, "read_parquet", lambda p, columns=None, **kw: (seen.append(columns), real(p, columns=columns, **kw))[1])
    ranks, n = ranking.session_ranks(key, date(2024, 3, 1), 5, ["AAA", "BBB"], tmp_path)
    assert n == 2 and seen and all(len(c) == 10 for c in seen)
    # and a later read_row on the same store still gets its path cells (the panel did not poison the frame LRU)
    assert store.read_row(key, "AAA", date(2024, 3, 1), 5, tmp_path).has_paths
