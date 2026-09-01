"""RESULTS-IDENTITY GUARD: a pinned, full-precision fingerprint of a real equity backtest.

WHAT THIS IS FOR
----------------
The option program's standing acceptance criterion is that option work leaves NON-OPTION
backtest RESULTS bit-identical. Scoping arguments ("the option code only runs when an option
action is configured") are not evidence — a pinned golden run is. This file drives a real,
deterministic equity backtest through the REAL engine and pins EVERY trade at full float
precision, so any change that moves an equity result by even 1e-9 fails here loudly, with a
readable per-field diff rather than a bare hash mismatch.

The pin covers the FULL order path — decision -> ExpertRecommendation -> TradeActionEvaluator
enter ruleset (with an entry TP/SL bracket) -> classic TradeRiskManagement sizing ->
BacktestAccount.submit_order -> next-bar fill -> intrabar TP/SL bracket fills -> per-bar equity
snapshot -> round-trip P&L. That is deliberate: the perf harness runs at orders/run = 0 and so
cannot see the order path at all, which is exactly where a results regression would hide.

THE RUN
-------
  * 6 synthetic symbols, 120 daily bars, an in-memory triangular price wave built from exact
    rational arithmetic — no RNG, no wall clock, no network, no FMP key, no cache.
  * A stub expert that recommends BUY on every bar; the enter ruleset's ``HasNoPosition``
    trigger gates re-entry, so a symbol re-enters once its bracket has closed the last trade.
  * An entry bracket of +8% take-profit / -5% stop-loss off ``order_open_price``, which over
    the wave produces ~149 CLOSED round-trips (both take_profit and stop_loss exits, plus a
    handful still open at run end) rather than one buy-and-hold position.

THE CROSS-CHECK (2026-09-01)
----------------------------
This exact harness was also run against the last PRE-OPTIONS dev commit
``8109dca397df688c718f41a0664d7e940bea8a71`` (``abcee41f^`` — the parent of the first option
merge into dev, "Merge option-review-fixes"), extracted with ``git archive`` into a clean tree
with its own PYTHONPATH. The fingerprints were compared:

    pre-options ref 8109dca3 : f6226a66e47a15f17c9c5a85e2dde91d07489dd1baffb974b938e90aa163f067
    options HEAD    a901e360 : f6226a66e47a15f17c9c5a85e2dde91d07489dd1baffb974b938e90aa163f067

    VERDICT: EQUAL — byte-identical, trade for trade, at full float precision.

So the 89 commits from the reference to HEAD — every option merge the operator named
(abcee41f, 3176f4d5, 06de5148, 70603e3a) plus their fix rounds, and the non-option grid/UI
work merged alongside — left equity backtest results untouched. The value committed in
``golden/equity_golden_run.json`` is therefore the pre-``abcee41f`` value, not merely HEAD's.

SCOPE, stated precisely: ``abcee41f`` is the first option merge into DEV, but the reference
tree it parents ALREADY carries the earlier option engine (``options_provider.py``,
``option_greeks.py``, ``options_store.py``, ``OptionsAccountInterface`` on
``BacktestAccount``), which reached dev before it. This pin therefore proves results identity
across the option program-review era and everything after it — NOT across the option engine's
original introduction, which predates the reference and has no golden of its own.

IF THIS TEST FAILS
------------------
Assume a results regression until proven otherwise. Read the printed diff, explain the moved
trades, and only then regenerate the golden DELIBERATELY, with operator-visible justification:

    BA2_REGEN_EQUITY_GOLDEN=1 python -m pytest tests/backtest/test_equity_golden_run.py

Run from the backend dir:
    python -m pytest tests/backtest/test_equity_golden_run.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

# --------------------------------------------------------------------------- #
# 1. The synthetic price fixture (no network, no cache, no FMP).
# --------------------------------------------------------------------------- #
SYMBOLS = ("SYNA", "SYNB", "SYNC", "SYND", "SYNE", "SYNF")
N_BARS = 120
_FIRST_DAY = date(2024, 1, 2)
_AMPLITUDE = 0.12

TP_PCT = 8.0
SL_PCT = -5.0

STARTING_CASH = 100_000.0
ACCOUNT_ID = 9101
EXPERT_ID = 9101
SEED = 42


def _business_days(start: date, n: int) -> List[date]:
    out: List[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


BAR_DATES = _business_days(_FIRST_DAY, N_BARS)


def _closes(idx: int) -> List[float]:
    """A deterministic triangular price wave (exact rational arithmetic, no RNG, no sin)."""
    base = 100.0 + 10.0 * idx
    period = 8 + 2 * idx
    phase = 3 * idx
    half = period / 2.0
    out: List[float] = []
    for t in range(N_BARS):
        pos = (t + phase) % period
        tri = pos / half if pos < half else (period - pos) / half
        out.append(round(base * (1.0 + _AMPLITUDE * (tri - 0.5)), 4))
    return out


def _bar_rows(idx: int) -> List[Dict[str, Any]]:
    closes = _closes(idx)
    rows: List[Dict[str, Any]] = []
    for t, d in enumerate(BAR_DATES):
        c = closes[t]
        o = closes[t - 1] if t else c
        rows.append({
            "Date": d,
            "Open": o,
            "High": round(max(o, c) * 1.004, 4),
            "Low": round(min(o, c) * 0.996, 4),
            "Close": c,
            "Volume": 1_000_000,
        })
    return rows


# --------------------------------------------------------------------------- #
# 2. The deterministic stub expert.
# --------------------------------------------------------------------------- #
def _make_expert_cls():
    from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
    from ba2_common.core.types import OrderRecommendation, Recommendation

    class _GoldenStubExpert(MarketExpertInterface):
        """BUY every bar for every symbol at that bar's own close.

        The enter ruleset's HasNoPosition trigger is what gates re-entry, so a symbol
        re-enters on the first bar after its bracket closed the previous round-trip.
        """

        def __init__(self, id: int, price_source):
            super().__init__(id)
            self._ps = price_source

        @classmethod
        def description(cls) -> str:
            return "Deterministic golden-run stub expert (BUY every bar)."

        @classmethod
        def get_settings_definitions(cls) -> dict:
            return {}

        def render_market_analysis(self, market_analysis) -> str:
            return ""

        def run_analysis(self, symbol: str, market_analysis) -> None:
            return None

        def analyze_as_of(self, as_of, context):
            # The engine pins the bar's symbol on the shared expert object (``_gather_symbol``)
            # exactly as the live ``run_analysis`` does -- that, not ``context.extra``, is how
            # every real expert learns which symbol it is being asked about.
            symbol = getattr(self, "_gather_symbol", None)
            close = self._ps.close_at(symbol, as_of) if symbol else None
            if close is None:
                return Recommendation(signal=OrderRecommendation.HOLD, confidence=50.0,
                                      current_price=0.0, details="no bar",
                                      expected_profit_percent=0.0)
            return Recommendation(
                signal=OrderRecommendation.BUY,
                confidence=80.0,
                current_price=float(close),
                details="golden stub buy",
                expected_profit_percent=TP_PCT,
            )

    return _GoldenStubExpert


class _NoAtrIndicatorProvider:
    """A hermetic indicator provider that reports NO ATR for every symbol.

    ``DailyBacktestEngine.run()`` builds a REAL, FMP-backed indicator provider whenever
    ``indicator_provider`` is None -- which needs an FMP key and could reach the network.
    Injecting this stub keeps the run offline and pins the safeguard-stop input: with no ATR
    value available (``position_sizing.get_latest_atr``'s documented no-ATR path), the only
    protective stop is the entry ruleset's own SL leg, so the fingerprint depends on nothing
    outside this file.
    """

    def get_indicator(self, symbol, indicator, **kwargs):
        return {"values": []}


# --------------------------------------------------------------------------- #
# 3. The run.
# --------------------------------------------------------------------------- #
def run_golden_backtest() -> Dict[str, Any]:
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    cfg = {
        "starting_cash": STARTING_CASH,
        "commission_per_trade": 1.0,
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
    }

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"equity-golden-{ACCOUNT_ID}")
    ctx.__enter__()
    try:
        seed_account_definition(ACCOUNT_ID, cfg)
        ruleset_id = seed_ruleset_from_tree(
            None,
            name="golden-enter-bracket",
            entry_actions=[
                {"id": "tp", "action_type": "adjust_take_profit",
                 "reference_value": "order_open_price", "action_value": TP_PCT},
                {"id": "sl", "action_type": "adjust_stop_loss",
                 "reference_value": "order_open_price", "action_value": SL_PCT},
            ],
        )
        seed_expert_instance(
            account_id=ACCOUNT_ID,
            expert_class_name="_GoldenStubExpert",
            enter_market_ruleset_id=ruleset_id,
            instance_id=EXPERT_ID,
        )

        ps = AsOfPriceSource(ohlcv_provider=None)
        for i, sym in enumerate(SYMBOLS):
            ps.load_bars(sym, _bar_rows(i))

        account = BacktestAccount(ACCOUNT_ID, ps, cfg)
        resolver.register_account(ACCOUNT_ID, account)

        expert = _make_expert_cls()(EXPERT_ID, ps)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
        })
        resolver.register_expert(EXPERT_ID, expert)

        config = {
            "start_date": datetime(BAR_DATES[0].year, BAR_DATES[0].month, BAR_DATES[0].day),
            "end_date": datetime(BAR_DATES[-1].year, BAR_DATES[-1].month, BAR_DATES[-1].day),
            "enabled_instruments": list(SYMBOLS),
            "seed": SEED,
        }
        engine = DailyBacktestEngine(
            account=account,
            experts=[(expert, EXPERT_ID, {}, ruleset_id)],
            price_source=ps,
            config=config,
            indicator_provider=_NoAtrIndicatorProvider(),
        )
        engine.run()
        return fingerprint(account)
    finally:
        ctx.__exit__(None, None, None)


def _r(x) -> Any:
    """Full float precision via repr (never a rounded / shortened form)."""
    if x is None:
        return None
    return repr(float(x))


def _ts(x) -> Any:
    if x is None:
        return None
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    return str(x)


def fingerprint(account) -> Dict[str, Any]:
    """The canonical, full-precision result fingerprint of a finished run.

    Every trade field is carried at FULL float precision (``repr``), so a 1e-9 perturbation
    anywhere in the fill path changes the fingerprint. The fields are exactly the ones an
    EQUITY round-trip has on BOTH sides of the option work (symbol / entry+exit timestamps /
    quantity / entry+exit price / P&L / P&L% / bars held / exit reason); the option-only
    columns (``contract_symbol``, ``underlying_symbol``, ``multiplier``) and the DB-assigned
    ``transaction_id`` are deliberately excluded -- they are not equity results.
    """
    rts = account.get_round_trip_trades()
    rows = [
        {
            "symbol": str(t["symbol"]),
            "direction": str(t["direction"]),
            "entry_time": _ts(t["entry_time"]),
            "exit_time": _ts(t["exit_time"]),
            "size": _r(t["size"]),
            "entry_price": _r(t["entry_price"]),
            "exit_price": _r(t["exit_price"]),
            "pnl": _r(t["pnl"]),
            "pnl_pct": _r(t["pnl_pct"]),
            "bars_held": t["bars_held"],
            "exit_reason": str(t["exit_reason"]),
        }
        for t in rts
    ]
    # Canonical order, independent of dict/insertion order inside the account.
    rows.sort(key=lambda r: (r["entry_time"] or "", r["symbol"], r["exit_time"] or "",
                             r["entry_price"], r["exit_price"], r["size"]))
    hist = account.get_balance_history()
    curve = [[_ts(h["date"]), _r(h["net_liquidating_value"]),
              _r(h["cash_balance"]), _r(h["equity_value"])] for h in hist]
    payload: Dict[str, Any] = {
        "n_trades": len(rows),
        "trades": rows,
        "final_equity": _r(account.equity()),
        "equity_curve_len": len(curve),
        "equity_first": curve[0] if curve else None,
        "equity_last": curve[-1] if curve else None,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload

# --------------------------------------------------------------------------- #
# 4. The committed golden + the tests.
# --------------------------------------------------------------------------- #
GOLDEN_PATH = Path(__file__).with_name("golden") / "equity_golden_run.json"

#: The floor the run must clear for the pin to mean anything. The fixture produces 149 closed
#: round-trips today; an engine change that silently stops trading would otherwise sail through
#: ``test_matches_pinned_fingerprint`` only by ALSO moving the golden, so this is the guard that
#: makes "identical" mean "identical AND still trading".
MIN_ROUND_TRIPS = 100

_CACHE: Dict[str, Any] = {}


def _cached_run() -> Dict[str, Any]:
    """One golden run per pytest session (the run takes a few seconds)."""
    if "fp" not in _CACHE:
        _CACHE["fp"] = run_golden_backtest()
    return _CACHE["fp"]


def _load_golden() -> Dict[str, Any]:
    with GOLDEN_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_golden(fp: Dict[str, Any], metadata: Dict[str, Any]) -> None:
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GOLDEN_PATH.open("w", encoding="utf-8") as fh:
        json.dump({"_metadata": metadata, "fingerprint": fp}, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _diff_lines(expected: Dict[str, Any], actual: Dict[str, Any], limit: int = 12) -> str:
    """A READABLE per-field diff (this is why the golden is a committed JSON, not just a hash)."""
    out: List[str] = []
    for key in ("n_trades", "equity_curve_len", "final_equity", "equity_first", "equity_last"):
        if expected.get(key) != actual.get(key):
            out.append(f"  {key}: golden={expected.get(key)!r}  now={actual.get(key)!r}")

    exp_t, act_t = expected.get("trades") or [], actual.get("trades") or []
    for i in range(min(len(exp_t), len(act_t))):
        if exp_t[i] != act_t[i]:
            fields = sorted(set(exp_t[i]) | set(act_t[i]))
            moved = [f"{f}: golden={exp_t[i].get(f)!r} now={act_t[i].get(f)!r}"
                     for f in fields if exp_t[i].get(f) != act_t[i].get(f)]
            out.append(f"  trade[{i}] ({exp_t[i].get('symbol')} "
                       f"{exp_t[i].get('entry_time')}): " + "; ".join(moved))
            if len(out) >= limit:
                out.append(f"  ... (diff truncated at {limit} lines)")
                break
    if len(exp_t) != len(act_t):
        out.append(f"  trade COUNT: golden={len(exp_t)} now={len(act_t)}")
        extra = act_t[len(exp_t):] if len(act_t) > len(exp_t) else exp_t[len(act_t):]
        side = "now-only" if len(act_t) > len(exp_t) else "golden-only"
        for row in extra[:3]:
            out.append(f"  {side} trade: {row}")
    return "\n".join(out) or "  (no field-level difference found - compare the raw JSON)"


def test_equity_golden_run_matches_pinned_fingerprint():
    """The real-engine equity run reproduces the committed golden EXACTLY, trade for trade.

    A failure here means non-option backtest results MOVED. That is a stop-the-line finding:
    explain the moved trades before touching the golden (see the module docstring).
    """
    fp = _cached_run()

    if os.environ.get("BA2_REGEN_EQUITY_GOLDEN") == "1":
        golden = _load_golden() if GOLDEN_PATH.exists() else {}
        meta = dict(golden.get("_metadata") or {})
        meta["regenerated"] = "manually, via BA2_REGEN_EQUITY_GOLDEN=1"
        _write_golden(fp, meta)
        import pytest
        pytest.skip(f"golden REGENERATED at {GOLDEN_PATH} (sha256={fp['sha256']})")

    golden = _load_golden()["fingerprint"]
    assert fp["sha256"] == golden["sha256"], (
        "EQUITY RESULTS MOVED — the pinned golden run no longer reproduces.\n"
        f"  golden sha256 = {golden['sha256']}\n"
        f"  current sha256 = {fp['sha256']}\n"
        "Field-level diff:\n" + _diff_lines(golden, fp)
    )
    # Belt and braces: the hash is over the canonical JSON, so an equal hash implies equal
    # content — compare the content anyway so a future hashing change cannot mask a drift.
    assert fp == golden, "fingerprint content differs despite matching sha256:\n" + _diff_lines(golden, fp)


def test_golden_run_actually_traded():
    """The pinned run really exercises the order path — entries AND exits, both outcomes.

    Without this floor, an engine change that silently stopped trading could be "fixed" by
    regenerating the golden to an empty run and would still look identical forever after.
    """
    fp = _cached_run()
    closed = [t for t in fp["trades"] if t["exit_reason"] != "open_at_end"]
    reasons = {t["exit_reason"] for t in fp["trades"]}

    assert len(closed) >= MIN_ROUND_TRIPS, (
        f"the golden run closed only {len(closed)} round-trips (floor {MIN_ROUND_TRIPS}) — "
        "the engine has stopped trading, so 'results identical' would be vacuous"
    )
    assert "take_profit" in reasons, f"no take-profit exit fired; exit reasons={sorted(reasons)}"
    assert "stop_loss" in reasons, f"no stop-loss exit fired; exit reasons={sorted(reasons)}"
    assert len({t["symbol"] for t in fp["trades"]}) >= 5, "the run must trade a real universe"
    assert fp["equity_curve_len"] == N_BARS, "one equity snapshot per simulated bar"


def test_golden_run_is_deterministic_in_process():
    """Three back-to-back runs in ONE process produce the IDENTICAL fingerprint.

    This is what proves the pin is a property of the ENGINE and not of a lucky ordering: no
    dict/set-iteration order, no wall clock and no unseeded RNG leaks into a result. The engine
    seeds ``random``/``numpy`` from ``config['seed']`` before the first decision, and this
    fixture's own price series is generated by exact arithmetic with no RNG at all — so the
    seed is pinned (SEED) rather than averaged over.
    """
    shas = [run_golden_backtest()["sha256"] for _ in range(3)]
    assert len(set(shas)) == 1, f"nondeterministic engine — three runs gave {shas}"
    assert shas[0] == _cached_run()["sha256"]
