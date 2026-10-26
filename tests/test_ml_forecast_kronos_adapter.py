"""Kronos adapter maths with an injected fake sampler (no torch): deciles, the P(TP before SL) grid, seeds, validation,
and the store builder end to end on OHLCV."""
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ml_forecast import build_store as bs  # noqa: E402
from tools.ml_forecast.adapters import kronos as K  # noqa: E402
from tools.ml_forecast.adapters.timesfm3 import InputDataError  # noqa: E402
from ba2_common.core.ml_forecast import store  # noqa: E402
from ba2_common.core.ml_forecast.forecast_row import DECILE_LEVELS, SL_GRID_PCT, TP_GRID_PCT  # noqa: E402
from ba2_common.core.ml_forecast.model_card import model_key  # noqa: E402

T = 20          # max horizon
SESSION = date(2024, 1, 5)    # a Friday


def ohlcv(n=600, last=100.0):
    """(n, 5) OHLCV whose LAST close is ``last``."""
    close = np.linspace(last * 0.8, last, n)
    return np.column_stack([close * 0.999, close * 1.005, close * 0.995, close, np.full(n, 1e6)])


def flat_paths(p, c0=100.0):
    return np.full((p, T, 4), c0)


def fixed_fn(paths):
    """A fake sampler returning the same hand-made paths for every item (and recording its calls)."""
    calls = []

    def fn(ctxs, xd, yd, n_paths, seeds):
        calls.append((list(ctxs), list(xd), list(yd), n_paths, list(seeds)))
        return [np.array(paths, dtype=float) for _ in ctxs]
    fn.calls = calls
    return fn


def adapter_for(paths, size="small"):
    n = len(paths)
    return K.KronosAdapter(K.kronos_card(size, n), fixed_fn(paths), n)


# ------------------------------------------------------------------ card
def test_cards_distinct_keys_and_fields():
    a, b = K.kronos_card("small"), K.kronos_card("base")
    assert (a.family, a.output_type, a.horizons, a.timeframe, a.context_length) == ("kronos", "paths", (5, 10, 20), "1d", 512)
    assert a.input_fields == ("Open", "High", "Low", "Close", "Volume")
    assert a.licence == "MIT" and a.licence_commercial_ok is True and a.pretraining_cutoff is None
    assert a.model_id == "NeoQuasar/Kronos-small" and b.model_id == "NeoQuasar/Kronos-base"
    assert model_key(a, ()) != model_key(b, ())
    assert model_key(K.kronos_card("small", 64), ()) != model_key(K.kronos_card("small", 32), ())   # n_paths is identity
    assert set(K.CARD_IDS) == {"kronos-small", "kronos-base"}
    assert K.card_n_paths(a) == K.DEFAULT_N_PATHS == 64
    for bad in ("large", "mini", ""):
        with pytest.raises(ValueError):
            K.kronos_card(bad)
    for bad in (1, 0, -3, True, 64.0):
        with pytest.raises(ValueError):
            K.kronos_card("small", bad)


def test_docstring_states_the_contamination():
    assert "mid-2025" in K.__doc__ and "2020-2024" in K.__doc__ and "almost certainly" in K.__doc__


def test_adapter_refuses_a_mismatched_card():
    with pytest.raises(ValueError, match="does not match"):
        K.KronosAdapter(K.kronos_card("small", 64), fixed_fn(flat_paths(32)), 32)
    from tools.ml_forecast.adapters.timesfm3 import timesfm3_card
    with pytest.raises(ValueError):
        K.KronosAdapter(timesfm3_card(512), fixed_fn(flat_paths(64)), 64)
    with pytest.raises(NotImplementedError):
        K.KronosAdapter(K.kronos_card("small", 4), fixed_fn(flat_paths(4)), 4, covariate_set=["volume"])


# ------------------------------------------------------------------ seeds
def test_seed_is_a_pure_function_of_the_row_identity():
    k = model_key(K.kronos_card("small"), ())
    s = K.derive_seed(k, "AAPL", SESSION)
    assert s == K.derive_seed(k, "AAPL", SESSION) == K.derive_seed(k, " aapl ", SESSION)
    assert 0 <= s < 2 ** 64
    others = {K.derive_seed(k, "MSFT", SESSION), K.derive_seed(k, "AAPL", date(2024, 1, 8)),
              K.derive_seed(model_key(K.kronos_card("base"), ()), "AAPL", SESSION)}
    assert s not in others and len(others) == 3
    # pinned: a change of the derivation changes every stored row, so it must be a deliberate act
    import hashlib
    assert s == int.from_bytes(hashlib.sha256(f"kronos-seed|{k}|AAPL|2024-01-05".encode()).digest()[:8], "big")


def seeded_fn(ctxs, xd, yd, n_paths, seeds):
    """Fake sampler whose paths depend ONLY on the item's seed and last close (like the real one's noise)."""
    out = []
    for c, s in zip(ctxs, seeds):
        rng = np.random.default_rng(s)
        steps = rng.normal(0, 0.01, (n_paths, T))
        out.append(float(c[-1, 3]) * np.exp(np.cumsum(steps, axis=1))[:, :, None].repeat(4, axis=2))
    return out


def test_rows_are_reproducible_and_independent_of_batch_composition():
    ad = K.KronosAdapter(K.kronos_card("small", 32), seeded_fn, 32)
    items = [(s, SESSION, ohlcv(600, 100 + i)) for i, s in enumerate(["AAA", "BBB", "CCC"])]
    a = ad.forecast_batch(items)
    b = ad.forecast_batch(items)
    c = ad.forecast_batch(list(reversed(items)))[::-1]
    d = [ad.forecast_batch([it])[0] for it in items]
    for x in (b, c, d):
        assert [r.to_record() for r in x] == [r.to_record() for r in a]
    other_day = ad.forecast_batch([("AAA", date(2024, 1, 8), ohlcv(600, 100))])[0]
    assert other_day.deciles != a[0].deciles


# ------------------------------------------------------------------ deciles
def test_deciles_are_empirical_percentiles_of_the_horizon_return():
    n = 11
    k = (np.arange(n) - 5) / 1000.0                                  # per-step drift of path i
    close = 100.0 * (1 + k[:, None] * np.arange(1, T + 1)[None, :])     # close[i, t] = c0 * (1 + k_i * (t+1))
    paths = np.repeat(close[:, :, None], 4, axis=2)
    row = adapter_for(paths).forecast_batch([("aapl", SESSION, ohlcv(600, 100.0))])[0]
    assert row.symbol == "AAPL" and row.session == SESSION and row.horizons == (5, 10, 20) and row.has_paths
    for h in (5, 10, 20):
        expect = np.percentile(k * h, DECILE_LEVELS)
        np.testing.assert_allclose(row.deciles[h], expect, atol=1e-12)
        assert row.decile(h, 50) == pytest.approx(0.0, abs=1e-12)          # the middle path is flat
        assert all(b >= a for a, b in zip(row.deciles[h], row.deciles[h][1:]))
    assert row.decile(10, 10) == pytest.approx(np.percentile(k * 10, 10))
    assert row.decile(20, 90) == pytest.approx(np.percentile(k * 20, 90))
    assert row.decile(5, 90) == pytest.approx(np.percentile(k * 5, 90))


def test_return_is_relative_to_the_close_of_session_s_not_the_sampled_open():
    paths = flat_paths(4, 50.0)           # every sampled price 50 ...
    row = adapter_for(paths).forecast_batch([("X", SESSION, ohlcv(600, 100.0))])[0]     # ... but close_S = 100
    assert row.decile(5, 50) == pytest.approx(-0.5)


# ------------------------------------------------------------------ the path-probability grid
def hand_paths():
    """c0 = 100, 5 paths, all prices 100 except the listed touches (indices are 0-based steps after S)."""
    p = flat_paths(5)
    # A: high 104 at step 2 (long TP3 reached, never a low)
    p[0, 2, 1] = 104
    # B: both a 103.5 high and a 96.5 low at step 0 (same-step tie)
    p[1, 0, 1], p[1, 0, 2] = 103.5, 96.5
    # C: nothing happens
    # D: low 98.5 at step 2 (touches SL1 but not SL2), high 104 at step 6
    p[3, 2, 2] = 98.5
    p[3, 6, 1] = 104
    # E: low 96 at step 4 (short TP3 reached), never a high
    p[4, 4, 2] = 96
    return p


def test_path_prob_grid_on_hand_made_paths():
    row = adapter_for(hand_paths()).forecast_batch([("X", SESSION, ohlcv(600, 100.0))])[0]
    g = row.path_prob
    # long, TP3 / SL3: A wins at step 2 (< 5); D's TP is at step 6 so it counts only for h >= 10; B is a tie = stop
    assert g("long", 5, 3, 3) == pytest.approx(0.2)
    assert g("long", 10, 3, 3) == pytest.approx(0.4)
    assert g("long", 20, 3, 3) == pytest.approx(0.4)
    # tighter stop SL1 (level 99): D's 98.5 low at step 2 comes BEFORE its TP at step 6 -> loss; SL2 (98) it survives
    assert g("long", 10, 3, 1) == pytest.approx(0.2)
    assert g("long", 10, 3, 2) == pytest.approx(0.4)
    # TP1 / SL1: A wins; B tie = stop (high 103.5 >= 101 AND low 96.5 <= 99 in the same step); D stopped first
    assert g("long", 20, 1, 1) == pytest.approx(0.2)
    # TP5 / TP8 / TP12 were never reached (A and D top out at 104)
    for tp in (5, 8, 12):
        for sl in SL_GRID_PCT:
            assert g("long", 20, tp, sl) == 0.0
    # short, TP3 (96.97 -> low <= 97) / SL3 (high >= 103): E wins at step 4 (< 5); A's step-2 high 104 is a stop only
    assert g("short", 5, 3, 3) == pytest.approx(0.2)
    assert g("short", 20, 3, 3) == pytest.approx(0.2)
    # B: both touched in the same step = stop, also for the short side ...
    # ... but with SL5 (105) B's 103.5 high is no longer a stop, so B's 96.5 low is a short win next to E's
    assert g("short", 20, 3, 5) == pytest.approx(0.4)
    # SL8 (level 108) is never touched, so B (tie at SL3) is a clean short win too: B and E within 5 steps
    assert g("short", 5, 3, 8) == pytest.approx(0.4)
    assert g("short", 20, 8, 3) == 0.0


def test_grid_is_complete_and_probabilities_are_monotone_in_the_horizon():
    rng = np.random.default_rng(1)
    c0 = 100.0
    close = c0 * np.exp(np.cumsum(rng.normal(0, 0.02, (200, T)), axis=1))
    paths = np.stack([close, close * 1.01, close * 0.99, close], axis=2)       # O, H, L, C
    probs = K.path_probabilities(paths, c0, (5, 10, 20))
    assert len(probs) == 2 * 3 * len(TP_GRID_PCT) * len(SL_GRID_PCT)
    for d in ("long", "short"):
        for tp in TP_GRID_PCT:
            for sl in SL_GRID_PCT:
                p5, p10, p20 = (probs[(d, h, tp, sl)] for h in (5, 10, 20))
                assert 0.0 <= p5 <= p10 <= p20 <= 1.0
    # a wider TP can only be reached later or never
    for sl in SL_GRID_PCT:
        v = [probs[("long", 20, tp, sl)] for tp in TP_GRID_PCT]
        assert all(b <= a + 1e-12 for a, b in zip(v, v[1:]))


def test_touch_is_inclusive_and_effective_high_low_cover_open_and_close():
    c0 = 100.0
    p = flat_paths(2, c0)
    p[0, 1, 1] = 103.0                     # the high lands exactly on the TP3 level: reached
    p[1, 1, 3] = 103.0                     # the CLOSE is 103 while the sampled High says 100: the path reached 103
    probs = K.path_probabilities(p, c0, (5,))
    assert probs[("long", 5, 3, 3)] == pytest.approx(1.0)
    p2 = flat_paths(1, c0)
    p2[0, 1, 1] = 102.999
    assert K.path_probabilities(p2, c0, (5,))[("long", 5, 3, 3)] == 0.0
    # same-step touch of both levels counts as the stop, for the long and the short side
    p3 = flat_paths(1, c0)
    p3[0, 0, 1], p3[0, 0, 2] = 103.0, 97.0
    pr = K.path_probabilities(p3, c0, (5,))
    assert pr[("long", 5, 3, 3)] == 0.0 and pr[("short", 5, 3, 3)] == 0.0
    # without the touch of the stop level in that step, the same step is a clean short TP
    p3[0, 0, 1] = 100.0
    assert K.path_probabilities(p3, c0, (5,))[("short", 5, 3, 3)] == 1.0


def test_horizon_and_shape_guards():
    with pytest.raises(ValueError):
        K.path_probabilities(flat_paths(3), 100.0, (30,))
    with pytest.raises(ValueError):
        K.path_probabilities(np.ones((3, T)), 100.0, (5,))


# ------------------------------------------------------------------ what the sampler is shown
def test_sampler_gets_last_512_bars_ending_at_s_and_the_next_business_days():
    ad = adapter_for(flat_paths(4))
    n = 700
    dates = pd.bdate_range(end=pd.Timestamp(SESSION), periods=n)
    df = pd.DataFrame(ohlcv(n, 100.0), index=dates, columns=["Open", "High", "Low", "Close", "Volume"])
    ad.forecast_batch([("AAPL", SESSION, df)])
    ctxs, xd, yd, n_paths, seeds = ad.sample_fn.calls[0]
    assert len(ctxs[0]) == 512 and ctxs[0].shape == (512, 5) and ctxs[0][-1, 3] == pytest.approx(100.0)
    assert xd[0][-1] == np.datetime64(SESSION, "D") and len(xd[0]) == 512
    assert xd[0][0] == dates[-512].to_datetime64().astype("datetime64[D]")
    assert len(yd[0]) == T and yd[0][0] == np.datetime64("2024-01-08")           # Friday -> next Monday
    assert all(np.is_busday(d) for d in yd[0]) and yd[0][-1] == np.datetime64("2024-02-02")
    assert n_paths == 4 and seeds == [K.derive_seed(ad.key, "AAPL", SESSION)]


def test_array_context_gets_synthesised_business_day_dates():
    ad = adapter_for(flat_paths(4))
    ad.forecast_batch([("AAPL", SESSION, ohlcv(512, 100.0))])
    xd = ad.sample_fn.calls[0][1][0]
    assert xd[-1] == np.datetime64(SESSION, "D") and len(xd) == 512


# ------------------------------------------------------------------ validation
def test_context_problems_are_input_data_errors():
    ad = adapter_for(flat_paths(4))
    ok = ohlcv(600, 100.0)
    with pytest.raises(InputDataError, match="needs at least 512"):
        ad.forecast_batch([("X", SESSION, ohlcv(100))])
    bad = ok.copy(); bad[-3, 1] = np.nan
    with pytest.raises(InputDataError, match="non-finite"):
        ad.forecast_batch([("X", SESSION, bad)])
    bad = ok.copy(); bad[-1, 3] = np.inf
    with pytest.raises(InputDataError, match="non-finite"):
        ad.forecast_batch([("X", SESSION, bad)])
    bad = ok.copy(); bad[-5, 2] = 0.0
    with pytest.raises(InputDataError, match="positive"):
        ad.forecast_batch([("X", SESSION, bad)])
    bad = ok.copy(); bad[-5, 4] = -1.0
    with pytest.raises(InputDataError, match="volume"):
        ad.forecast_batch([("X", SESSION, bad)])
    with pytest.raises(InputDataError, match="OHLCV must be"):
        ad.forecast_batch([("X", SESSION, np.ones((600, 3)))])
    df = pd.DataFrame(ok, index=pd.bdate_range(end=pd.Timestamp("2024-01-04"), periods=600),
                      columns=["Open", "High", "Low", "Close", "Volume"])
    with pytest.raises(InputDataError, match="not the session"):
        ad.forecast_batch([("X", SESSION, df)])
    with pytest.raises(InputDataError, match="lacks columns"):
        ad.forecast_batch([("X", SESSION, df.drop(columns=["Volume"]))])
    assert ad.sample_fn.calls == []                 # nothing reached the model


def test_model_output_problems_are_contract_errors_not_input_errors():
    item = [("X", SESSION, ohlcv(600, 100.0))]
    for make in (lambda c, x, y, n, s: [flat_paths(n)[:, :10]],                 # wrong horizon
                 lambda c, x, y, n, s: [flat_paths(n + 1)],                      # wrong path count
                 lambda c, x, y, n, s: [np.where(np.arange(T)[None, :, None] == 3, np.nan, flat_paths(n))],
                 lambda c, x, y, n, s: [np.where(np.arange(T)[None, :, None] == 3, -1.0, flat_paths(n))],
                 lambda c, x, y, n, s: []):                                      # wrong item count
        ad = K.KronosAdapter(K.kronos_card("small", 4), make, 4)
        with pytest.raises(ValueError) as e:
            ad.forecast_batch(item)
        assert not isinstance(e.value, InputDataError)


def test_a_glitch_path_with_a_non_positive_price_is_dropped_not_fatal():
    n = 8
    close = 100.0 * (1 + (np.arange(n) - 3.5)[:, None] / 1000.0 * np.arange(1, T + 1)[None, :])
    paths = np.repeat(close[:, :, None], 4, axis=2)
    ok = adapter_for(paths).forecast_batch([("X", SESSION, ohlcv(600, 100.0))])[0]
    bad = paths.copy()
    bad[0, 9, 2] = -15.7                       # path 0 (the lowest drift) holds one impossible Low
    ad = adapter_for(bad)
    row = ad.forecast_batch([("X", SESSION, ohlcv(600, 100.0))])[0]
    assert ad.rejected_paths == 1 and ad.forecasts_with_rejects == 1
    assert len(ad.reject_log) == 1 and ad.reject_log[0].startswith("X 2024-01-05: 1/8 paths dropped")
    k = (np.arange(1, n) - 3.5) / 1000.0       # the 7 surviving paths
    np.testing.assert_allclose(row.deciles[10], np.percentile(k * 10, DECILE_LEVELS), atol=1e-12)
    assert row.deciles[10] != ok.deciles[10]
    # too many glitches: the model is broken, refuse
    many = paths.copy()
    many[:3, 9, 2] = -1.0
    with pytest.raises(ValueError, match="only 5/8"):
        adapter_for(many).forecast_batch([("X", SESSION, ohlcv(600, 100.0))])
    nan = paths.copy()
    nan[0, 9, 2] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        adapter_for(nan).forecast_batch([("X", SESSION, ohlcv(600, 100.0))])


def test_empty_batch():
    assert adapter_for(flat_paths(4)).forecast_batch([]) == []


def test_import_does_not_pull_in_torch():
    prog = ("import sys; import tools.ml_forecast.adapters.kronos, tools.ml_forecast.build_store; "
            "print('torch' in sys.modules)")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parents[1])]))
    out = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, check=True, env=env).stdout
    assert out.strip() == "False"


# ------------------------------------------------------------------ the builder on OHLCV
CARD = K.kronos_card("small", 8)
KEY = model_key(CARD, ())


@pytest.fixture
def cache(tmp_path):
    store.reset_cache()
    d = tmp_path / bs.OHLCV_DIRNAME
    d.mkdir()
    for sym, n in (("AAA", 700), ("BBB", 700), ("NEW", 300)):
        dates = pd.bdate_range("2023-02-01" if sym == "NEW" else "2022-01-03", periods=n)
        close = 100.0 + np.arange(n, dtype=float)               # the close encodes the bar position
        pd.DataFrame({"Date": dates, "Open": close - 0.5, "High": close + 1, "Low": close - 1, "Close": close,
                      "Volume": np.arange(n) + 1000}).to_parquet(d / f"{sym}_1d.parquet")
    yield tmp_path
    store.reset_cache()


class Spy:
    def __init__(self):
        self.items = []

    def __call__(self, ctxs, xd, yd, n_paths, seeds):
        self.items.extend(zip(ctxs, xd))
        return [np.full((n_paths, T, 4), float(c[-1, 3])) * np.linspace(1.0, 1.02, T)[None, :, None] for c in ctxs]


def run_build(cache, spy, **kw):
    data, _ = bs.load_daily_ohlcv(cache, ["AAA", "BBB", "NEW"])
    sessions = bs.session_calendar(data, date(2024, 1, 2), date(2024, 1, 12), "all")
    ad = K.KronosAdapter(CARD, spy, 8)
    return bs.build(CARD, data, sessions, ad, cache, **kw), data, sessions


def test_build_round_trip_point_in_time_and_skips(cache):
    spy = Spy()
    st, data, sessions = run_build(cache, spy, flush_sessions=3, batch_size=2)
    assert len(sessions) == 9 and st.forecasts == 18 and st.skipped_short_history == 9
    d, v = data["AAA"]                      # AAA and BBB share their dates, and the close encodes the bar position
    for ctx, xd in spy.items:
        assert ctx.shape == (512, 5) and xd.max() == xd[-1] and len(xd) == 512
        assert xd[-1].astype(object) in sessions
        assert ctx[-1, 3] == pytest.approx(100.0 + int(np.searchsorted(d, xd[-1])))
    for sess in sessions:
        row = store.read_row(KEY, "AAA", sess, 20, cache)
        assert row.session == sess and row.has_paths
        assert row.decile(20, 50) == pytest.approx(0.02, abs=1e-9)
        # the paths rise 2% in a straight line: TP1 is touched at step 10 (< 20), TP3 never, the stop never
        assert row.path_prob("long", 20, 1, 1) == 1.0 and row.path_prob("long", 20, 3, 3) == 0.0
        assert row.path_prob("short", 20, 1, 1) == 0.0
    assert store.read_row(KEY, "AAA", date(2024, 1, 5), 5, cache).path_prob("long", 5, 1, 1) == 0.0   # step 10 > 5
    meta = store.read_meta(KEY, cache)
    assert meta["card"]["family"] == "kronos" and meta["horizons"] == [5, 10, 20]


def test_ohlcv_window_never_holds_a_later_bar(cache):
    data, _ = bs.load_daily_ohlcv(cache, ["AAA"])
    d, v = data["AAA"]
    sess = date(2024, 1, 5)
    ctx1, why = bs.item_context(d, v, sess, 512)
    v2 = v.copy()
    v2[np.searchsorted(d, np.datetime64(sess, "D"), side="right"):] = 1e9
    ctx2, _ = bs.item_context(d, v2, sess, 512)
    pd.testing.assert_frame_equal(ctx1, ctx2)
    assert ctx1.index[-1] == pd.Timestamp(sess) and list(ctx1.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert bs.item_context(d, v, date(2024, 1, 6), 512) == (None, "no_bar")


def test_closes_path_is_unchanged_by_the_ohlcv_support(cache):
    data, _ = bs.load_daily_closes(cache, ["AAA"])
    d, c = data["AAA"]
    ctx, why = bs.item_context(d, c, date(2024, 1, 5), 512)
    assert why is None and isinstance(ctx, np.ndarray) and ctx.shape == (512,)


def test_resume_does_not_recompute(cache):
    run_build(cache, Spy())
    spy = Spy()
    st, _, sessions = run_build(cache, spy, resume=True)
    assert spy.items == [] and st.forecasts == 0 and st.skipped_resume == 2 * len(sessions)


def test_card_ids_cover_both_families():
    assert bs.CARD_IDS["kronos-small"] == "kronos" and bs.CARD_IDS["kronos-base"] == "kronos"
    assert bs.CARD_IDS["timesfm3-ctx512"] == "timesfm"
    assert bs.CARD_IDS["timesfm3-log-ctx2048"] == "timesfm"      # a log id is dispatched via card_for_id, as a log card
    from tools.ml_forecast.adapters.timesfm3 import card_for_id
    assert card_for_id("timesfm3-log-ctx512").input_fields != card_for_id("timesfm3-ctx512").input_fields


# ------------------------------------------------------------------ the real model (needs torch + CUDA + a Kronos clone)
def _real_model_available():
    if not os.environ.get(K.KRONOS_ENV):
        return False
    try:
        import torch
    except ImportError:
        return False
    return torch.cuda.is_available()


@pytest.mark.skipif(not _real_model_available(), reason=f"needs torch+CUDA and {K.KRONOS_ENV}")
def test_real_kronos_is_deterministic_for_a_fixed_composition():
    fn = K.load_kronos_sample_fn("small", "cuda")
    ad = K.KronosAdapter(K.kronos_card("small", 16), fn, 16)
    rng = np.random.default_rng(0)
    items = []
    for sym in ("AAA", "BBB"):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 512)))
        dates = pd.bdate_range(end=pd.Timestamp(SESSION), periods=512)
        items.append((sym, SESSION, pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                                                  "Close": close, "Volume": 1e6}, index=dates)))
    a, b = ad.forecast_batch(items), ad.forecast_batch(items)
    assert [r.to_record() for r in a] == [r.to_record() for r in b]
