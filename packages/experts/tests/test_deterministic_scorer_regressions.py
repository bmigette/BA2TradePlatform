"""DeterministicScorer regression guards for the 2026-08-07 review findings.

The original suite (test_deterministic_scorer_calculators.py) covers ONLY the pure
calculators, which is exactly why two blockers shipped unnoticed: the live
persistence path and the macro/exposure wiring are integration-shaped, not
formula-shaped. Everything here is a bug that was reproduced on the shipped code
before being fixed -- each test names the failure it locks out.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from ba2_common.core.models import AnalysisOutput
from ba2_experts.DeterministicScorer import DeterministicScorer
# `ba2_experts.DeterministicScorer` as an ATTRIBUTE resolves to the class (the
# package __init__ rebinds the name), so patching needs the module object.
import importlib
_ds_module = importlib.import_module("ba2_experts.DeterministicScorer")
from ba2_experts.DeterministicScorer import combine as C
from ba2_experts.DeterministicScorer import data as D
from ba2_experts.DeterministicScorer import fundamental as F
from ba2_experts.DeterministicScorer import macro as M
from ba2_experts.DeterministicScorer import technical as T

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# BLOCKER 1 - live persistence wrote to fields AnalysisOutput does not have.
# SQLModel silently drops unknown kwargs, so this did not fail at construction:
# it failed at COMMIT (NOT NULL on name/type) with the payload already discarded.
# --------------------------------------------------------------------------- #
def test_analysis_outputs_persist_with_valid_required_fields(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    closed = {"n": 0}
    real_close = session.close

    def _tracking_close():
        closed["n"] += 1
        real_close()

    session.close = _tracking_close  # type: ignore[method-assign]
    monkeypatch.setattr(_ds_module, "get_db", lambda: session)

    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1

    class _Rec:
        raw_outputs = {
            "combination": {"final": 0.42},
            "technical": {"score": 0.5},
            "fundamental": {"score": 0.1},
            "analyst": {},
            "regime": {"score": 1.0},
        }

    e._store_analysis_outputs(123, "AAPL", _Rec())

    with Session(engine) as check:
        rows = check.exec(select(AnalysisOutput)).all()
    assert rows, "no AnalysisOutput persisted"
    for row in rows:
        assert row.name, "name is NOT NULL in the schema"
        assert row.type, "type is NOT NULL in the schema"
        assert row.text, "the score payload must survive, not be silently dropped"
        assert row.symbol == "AAPL"
    # the JSON payload is really round-trippable, not a repr
    tree = next(json.loads(r.text) for r in rows if r.type == "score_tree")
    assert tree["final"] == 0.42
    assert closed["n"] == 1, "session must be closed exactly once (pool exhaustion)"


def test_analysis_outputs_rollback_and_close_on_failure(monkeypatch):
    """A commit failure must roll back AND close, never leak the session."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    calls = {"rollback": 0, "close": 0}
    session.commit = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore
    real_rollback, real_close = session.rollback, session.close
    session.rollback = lambda: (calls.__setitem__("rollback", 1), real_rollback())  # type: ignore
    session.close = lambda: (calls.__setitem__("close", 1), real_close())  # type: ignore
    monkeypatch.setattr(_ds_module, "get_db", lambda: session)

    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1

    class _Rec:
        raw_outputs = {"combination": {"final": 0.1}}

    with pytest.raises(RuntimeError):
        e._store_analysis_outputs(1, "AAPL", _Rec())
    assert calls == {"rollback": 1, "close": 1}


# --------------------------------------------------------------------------- #
# BLOCKER 2 - with only the (binary +-1) index-trend input available, a single
# input drove regime to exactly -1.0 < hard_riskoff -> exposure 0 -> the expert
# went flat on EVERY name whenever SPY sat below its SMA200.
# --------------------------------------------------------------------------- #
def test_single_binary_macro_input_cannot_zero_exposure():
    r = M.regime_composite(
        {"trend_index": -1.0, "breadth": None, "vix": None, "credit": None,
         "yield_curve": None, "pmi": None, "sahm": None}, dict(M.DEF_MW))
    assert r["n_inputs"] == 1
    m = M.exposure_multiplier(r["score"], 0.25, -0.75, n_inputs=r["n_inputs"])
    assert m > 0.0, "one binary input must not force a full de-risk"
    assert m == pytest.approx(0.25), "it should fall back to the floor, not to zero"


def test_hard_riskoff_still_fires_on_a_corroborated_regime():
    """The safety valve must survive: several inputs agreeing still zeroes exposure."""
    r = M.regime_composite(
        {"trend_index": -1.0, "vix": -1.0, "pmi": -1.0, "sahm": -1.0}, dict(M.DEF_MW))
    m = M.exposure_multiplier(r["score"], 0.25, -0.75, n_inputs=r["n_inputs"])
    assert m == 0.0


def test_strong_signal_is_attenuated_not_annihilated_by_a_lone_input():
    """The bug was the score being multiplied by EXACTLY 0, which erased the
    verdict entirely (no BUY *and* no SELL, everything pinned to HOLD).

    Attenuation itself is by design: in multiply mode `score * M` is compared to
    a fixed theta_buy, so a regime below roughly -0.34 makes a long impossible
    whatever the signal. What must not happen is a SINGLE uncorroborated binary
    input driving M to zero and flattening the book.
    """
    lone = C.final_score(technical=1.0, fundamental=1.0, analyst=None,
                         regime=-1.0, s={}, regime_n_inputs=1)
    corroborated = C.final_score(technical=1.0, fundamental=1.0, analyst=None,
                                 regime=-1.0, s={}, regime_n_inputs=4)
    assert lone["exposure_multiplier"] == pytest.approx(0.25)
    assert lone["final"] > 0.0, "the verdict must survive, merely scaled down"
    assert corroborated["final"] == 0.0, "a corroborated risk-off still de-risks fully"


# --------------------------------------------------------------------------- #
# GRAVE 3 - data.py wrapped every fetch in `except Exception` + logger.debug,
# swallowing FMPHermeticViolation / cache-miss and silently degrading scores.
# --------------------------------------------------------------------------- #
def test_hermetic_violation_propagates_out_of_fetch_ohlcv():
    from ba2_providers.fmp_common import FMPHermeticViolation

    class _Boom:
        def ohlcv(self):
            class _P:
                def get_ohlcv_data(self, **kw):
                    raise FMPHermeticViolation("network in backtest")
            return _P()

    with pytest.raises(FMPHermeticViolation):
        D.fetch_ohlcv(_Boom(), "AAPL", NOW)


def test_real_network_error_is_still_absorbed():
    """OSError is the benign class: a genuine outage must not kill the run."""
    class _Down:
        def ohlcv(self):
            class _P:
                def get_ohlcv_data(self, **kw):
                    raise ConnectionError("dns")
            return _P()

    assert D.fetch_ohlcv(_Down(), "AAPL", NOW) is None


# --------------------------------------------------------------------------- #
# GRAVE 4 - settings declared but never read (dead GA genes), and settings read
# but never declared (phantom, permanently pinned to its inline default). This is
# the same failure class as the ATR tz bug: a gene the grid thinks it is tuning.
# --------------------------------------------------------------------------- #
def test_every_declared_setting_is_resolved():
    declared = set(DeterministicScorer.get_settings_definitions())
    resolved = set(DeterministicScorer._SETTING_KEYS)
    assert declared == resolved, (
        f"declared-but-unresolved={declared - resolved}; "
        f"resolved-but-undeclared={resolved - declared}")


def test_every_setting_read_by_the_code_is_declared():
    """Greps the package for settings.get(...)/s.get(...) keys and asserts each one
    is a real ExpertSetting, so a typo can never silently become a hardcoded default."""
    import re
    from pathlib import Path

    root = Path(__import__("ba2_experts").__file__).parent / "DeterministicScorer"
    declared = set(DeterministicScorer.get_settings_definitions())
    # Deliberately NOT exposed as ExpertSettings: both arms only do something
    # when the caller threads prev_signal, which the stateless per-bar _process
    # does not. Declaring them would create GA genes that cannot move anything.
    exempt = {"exit_hysteresis", "min_score_delta"}
    pattern = re.compile(r"""\b(?:settings|s)\.get\(\s*["']([a-z_0-9]+)["']""")
    seen: set = set()
    for path in root.glob("*.py"):
        seen |= set(pattern.findall(path.read_text(encoding="utf-8")))
    unknown = seen - declared - exempt
    assert not unknown, f"settings read but never declared: {sorted(unknown)}"


def test_declared_settings_actually_reach_the_code():
    """Inverse guard: a declared setting nobody reads is an inert GA gene."""
    import re
    from pathlib import Path

    root = Path(__import__("ba2_experts").__file__).parent / "DeterministicScorer"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    declared = set(DeterministicScorer.get_settings_definitions())
    # settings consumed by name-building rather than a literal .get("<key>")
    indirect = {f"mw_{k}" for k in M.DEF_MW}
    unread = {k for k in declared - indirect
              if not re.search(rf"""["']{re.escape(k)}["']""", blob.replace(
                  f'"{k}": {{', ""))}
    assert not unread, f"declared but never read: {sorted(unread)}"


# --------------------------------------------------------------------------- #
# GRAVE 5 - in backtest the OHLCV cache was bypassed entirely (`if as_of is None`),
# so every bar refetched the full 600-day window per symbol.
# --------------------------------------------------------------------------- #
def test_cached_window_covers_historical_bars_not_just_today():
    """Caching by symbol is only safe if the payload COVERS the requested bar.

    Anchoring the fetch window on `now` (rather than on the first as_of) makes
    every historical slice come back empty, so a backtest silently trades
    nothing and merely looks idle -- no error, no warning.
    """
    seen = {}
    idx = pd.date_range("2022-01-01", periods=1500, freq="D")
    frame = pd.DataFrame({"Date": idx, "Close": range(1500),
                          "High": range(1500), "Low": range(1500)})

    class _P:
        def get_ohlcv_data(self, **kw):
            seen["start"] = pd.Timestamp(kw["start_date"])
            return frame[(frame["Date"] >= pd.Timestamp(kw["start_date"]).tz_localize(None))]

    class _Bundle:
        def ohlcv(self):
            return _P()

    D.reset_caches()
    as_of = datetime(2024, 3, 1)
    out = D.fetch_ohlcv(_Bundle(), "AAPL", as_of)
    assert out is not None and not out.empty, "historical bar got an empty slice"
    assert seen["start"] < pd.Timestamp(as_of), "fetch window must reach back BEFORE as_of"
    assert out["Date"].max() <= pd.Timestamp(as_of)


def test_backtest_reuses_one_ohlcv_fetch_across_bars():
    calls = {"n": 0}
    idx = pd.date_range("2024-01-01", periods=400, freq="D")

    class _P:
        def get_ohlcv_data(self, **kw):
            calls["n"] += 1
            return pd.DataFrame({"Date": idx, "Close": range(400),
                                 "High": range(400), "Low": range(400)})

    class _Bundle:
        def ohlcv(self):
            return _P()

    D.reset_caches()
    b = _Bundle()
    a = D.fetch_ohlcv(b, "AAPL", datetime(2024, 6, 1))
    c = D.fetch_ohlcv(b, "AAPL", datetime(2024, 6, 2))
    assert calls["n"] == 1, "one fetch per symbol per run, sliced locally per bar"
    assert len(a) < len(c), "each bar must still see only its own causal slice"
    assert a["Date"].max() <= pd.Timestamp("2024-06-01")


# --------------------------------------------------------------------------- #
# MOYEN 6 - three of the seven macro inputs were structurally dead:
# `isinstance(x, object)` is always True, and the series keys were never written.
# --------------------------------------------------------------------------- #
def test_yield_curve_and_sahm_inputs_are_actually_wired():
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    bundle = {
        "macro_inputs": {
            "vix": 12.0, "pmi": 55.0,
            "spread_10y3m_series": pd.Series([1.5] * 70),
            "unrate_series": pd.Series([4.0] * 12 + [5.2, 5.4, 5.6]),
            "oas_series": pd.Series([4.0] * 100 + [9.0]),
        },
        "index_closes": pd.Series(range(1, 301), dtype=float),
    }
    regime = e._build_regime(bundle, {})
    used = set(regime["components"])
    for key in ("yield_curve", "sahm", "credit"):
        assert key in used, f"{key} never contributes (dead macro input)"


def test_yield_curve_score_reads_percent_units():
    """FRED serves 10y-3m in PERCENT; a 0.005 'decimal' scale saturated the tanh
    on every observation, making a graded input effectively binary."""
    flat = M.yield_curve_score(pd.Series([0.10] * 70))     # 10bp, nearly flat
    steep = M.yield_curve_score(pd.Series([2.50] * 70))    # 250bp, clearly steep
    assert abs(flat) < 0.5, f"a 10bp curve must not saturate (got {flat})"
    assert steep > 0.9
    inverted = M.yield_curve_score(pd.Series([-1.0] * 70))
    assert inverted < -0.5


# --------------------------------------------------------------------------- #
# MOYEN 7 - a symbol with zero fundamentals scored 0.0 (not None), so the section
# kept its full weight and silently cut every score by ~37%.
# --------------------------------------------------------------------------- #
def test_missing_fundamentals_renormalize_instead_of_diluting():
    empty = F.fundamental_score({}, {})
    assert empty["score"] is None, "no inputs must mean 'no section', not 'neutral 0'"
    res = C.final_score(technical=0.8, fundamental=None, analyst=None,
                        regime=None, s={})
    only_tech = C.final_score(technical=0.8, fundamental=None, analyst=None,
                              regime=None, s={"w_fundamental": 0.0})
    assert res["final"] == pytest.approx(only_tech["final"])


def test_neutral_mode_keeps_the_old_dilution_when_asked():
    res = C.final_score(technical=0.8, fundamental=None, analyst=None, regime=None,
                        s={"skip_on_missing_section": "neutral"})
    full = C.final_score(technical=0.8, fundamental=None, analyst=None, regime=None,
                         s={"skip_on_missing_section": "skip"})
    assert res["final"] < full["final"]


# --------------------------------------------------------------------------- #
# MOYEN 8 - one z_veto threshold was applied to two different Altman scales, and
# a veto forced score to -0.5, i.e. it ACTIVELY SHORTED on a data-integrity flag.
# --------------------------------------------------------------------------- #
def test_altman_threshold_follows_the_variant():
    # Z'' (no market cap -> adjusted) distress cutoff is 1.1, not 1.81.
    snap = {"z": 1.5, "z_variant": "adjusted"}
    assert F.fundamental_score(snap, {})["veto"] is False, \
        "1.5 is healthy on the Z'' scale; vetoing it flags most non-manufacturers"
    assert F.fundamental_score({"z": 0.9, "z_variant": "adjusted"}, {})["veto"] is True
    assert F.fundamental_score({"z": 1.5, "z_variant": "original"}, {})["veto"] is True


def test_veto_blocks_the_buy_without_opening_a_short():
    s = {}
    res = C.final_score(technical=1.0, fundamental=1.0, analyst=None, regime=None,
                        s=s, veto=True)
    assert C.schmitt_trigger(res["final"], s) == "HOLD", \
        "a distress/data veto must block the entry, not flip it into a short"


def test_veto_can_still_be_configured_to_short():
    s = {"veto_cap": -0.9}
    res = C.final_score(technical=1.0, fundamental=1.0, analyst=None, regime=None,
                        s=s, veto=True)
    assert C.schmitt_trigger(res["final"], s) == "SELL"


# --------------------------------------------------------------------------- #
# MOYEN 9 - growth on negative earnings read a deterioration as growth.
# --------------------------------------------------------------------------- #
def test_growth_acceleration_handles_negative_earnings():
    worsening = [-1.0, -1.0, -1.0, -1.0, -1.0, -2.0]   # EPS -1 -> -2 is WORSE
    assert F.growth_acceleration(worsening) < 0
    improving = [-2.0, -2.0, -2.0, -2.0, -2.0, -1.0]   # -2 -> -1 is better
    assert F.growth_acceleration(improving) > 0


def test_growth_acceleration_is_the_one_used_by_the_expert():
    """It was unit-tested but never imported; the expert reimplemented it inline
    on annual data with a formula that diverged (and mis-signed losses)."""
    import importlib
    import inspect

    mod = importlib.import_module("ba2_experts.DeterministicScorer")
    src = inspect.getsource(mod)
    assert "growth_acceleration" in src, "the expert must use the tested calculator"
    assert mod.growth_acceleration is F.growth_acceleration


# --------------------------------------------------------------------------- #
# MOYEN 10 - the "Schmitt trigger" had no hysteresis (same threshold in and out)
# and its min_score_delta branch was unreachable dead code.
# --------------------------------------------------------------------------- #
def test_holding_a_long_uses_a_looser_exit_threshold():
    s = {"theta_buy": 0.5, "theta_sell": 0.2, "exit_hysteresis": 0.2}
    assert C.schmitt_trigger(0.4, s, prev_signal=None) == "HOLD"
    assert C.schmitt_trigger(0.4, s, prev_signal="BUY") == "BUY", \
        "dropping out at the entry threshold is exactly the churn a Schmitt prevents"
    assert C.schmitt_trigger(0.25, s, prev_signal="BUY") == "HOLD"


def test_min_score_delta_applies_on_a_reversal_from_long():
    s = {"theta_buy": 0.5, "theta_sell": 0.2, "min_score_delta": 0.3}
    assert C.schmitt_trigger(-0.25, s, prev_signal="BUY") == "HOLD", \
        "flip-to-short needs theta_sell + delta once long (was dead code)"
    assert C.schmitt_trigger(-0.55, s, prev_signal="BUY") == "SELL"


# --------------------------------------------------------------------------- #
# MOYEN 11 - w_analyst is a GA gene, but _gather only ever saw the LIVE-set
# attribute, so in backtest grades were never fetched and the section never fired.
# --------------------------------------------------------------------------- #
def test_backtest_pins_analyst_weight_and_index_from_settings():
    from ba2_common.core.backtest_context import BacktestContext

    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    e._gather_symbol = "AAPL"
    captured = {}

    def _fake_gather(providers, as_of):
        captured["w_analyst"] = getattr(e, "_gather_w_analyst", None)
        captured["index"] = getattr(e, "_gather_index_symbol", None)
        return {"symbol": "AAPL", "ohlcv": None, "current_price": None,
                "statements": {}, "grades_rows": [], "macro_inputs": {},
                "index_closes": None}

    e._gather = _fake_gather
    ctx = BacktestContext(providers=None, settings={"w_analyst": 0.3,
                                                    "index_symbol": "QQQ"},
                          extra={"symbol": "AAPL"})
    e.analyze_as_of(NOW, ctx)
    assert captured["w_analyst"] == 0.3, "the GA gene never reached _gather"
    assert captured["index"] == "QQQ"


# --------------------------------------------------------------------------- #
# MINEURS - crash guards on GA/UI-reachable parameter combinations.
# --------------------------------------------------------------------------- #
def test_fundamentals_max_age_spans_a_full_annual_filing_cycle():
    """The setting was inert; switching it on with the plan's 180d default would
    have blanked the section for 8 months of every 12, because the fetcher asks
    for ANNUAL statements (one filing per year)."""
    e = DeterministicScorer.__new__(DeterministicScorer)
    e.id = 1
    inc = [{"fillingDate": "2025-02-01", "total_revenue": 1000, "net_income": 90,
            "operating_income": 130, "weighted_average_shares_outstanding": 1000}]
    bal = [{"totalAssets": 5000, "total_shareholder_equity": 3000,
            "total_liabilities": 2000, "totalCurrentAssets": 2000,
            "totalCurrentLiabilities": 900, "retainedEarnings": 1500}]
    bundle = {"statements": {"income": inc, "balance": bal, "cashflow": [{}]},
              "current_price": 100.0}
    default_age = DeterministicScorer.get_settings_definitions()[
        "fundamentals_max_age_days"]["default"]
    # 11 months after the filing the next annual report still is not out
    as_of = datetime(2026, 1, 15, tzinfo=timezone.utc)
    res = e._build_fundamental(bundle, {"fundamentals_max_age_days": default_age}, as_of)
    assert res["score"] is not None, "annual fundamentals must survive their own cycle"
    # but a genuinely abandoned filing (2y+) is still dropped
    stale = e._build_fundamental(
        bundle, {"fundamentals_max_age_days": default_age},
        datetime(2027, 6, 1, tzinfo=timezone.utc))
    assert stale["score"] is None
    assert stale["stale_fundamentals_age_days"] > default_age


def test_momentum_skip_beyond_lookback_returns_none_instead_of_indexerror():
    closes = pd.Series(range(1, 301), dtype=float)
    assert T.momentum_12_1(closes, lookback=252, skip=300) is None


def test_analyst_dates_compare_across_naive_and_aware():
    from ba2_experts.DeterministicScorer.analyst import revision_momentum

    rows = [{"date": "2026-06-10T00:00:00+00:00", "strongBuy": 5, "buy": 3,
             "hold": 1, "sell": 0, "strongSell": 0}]
    naive = datetime(2026, 6, 13)          # tz-naive as_of vs tz-aware row
    assert revision_momentum(rows, naive) is not None
