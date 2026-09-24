"""RESULTS-IDENTITY GUARD for the CALIBRATED option spread model (plan Part F), end to end.

``test_option_golden_run.py`` pins the O_LEAP chain under the explicit legacy zero-spread
config. This file runs THE SAME fixture through the real ``DailyBacktestEngine.run()`` with
``option_spread_model = SPREAD_MODEL_VERSION`` and pins the result, so the as-of-quote /
fallback spread path is covered by a real run and not only by the unit tests in
``test_option_spread_asof_model.py``.

THE QUOTES. The sqlite fixture store has no bid/ask on its bars (exactly like the TastyTrade
tree), so the reader is wrapped to publish a deterministic NBBO on two of every three premium
bars and none on the third. Both spread sources therefore fire inside the run, and the golden
records how many fills each priced (``option_spread_fill_sources``) -- a change that silently
stopped reading the as-of quote would move that count even if the prices happened to agree.

NEW PIN, approved 2026-09-23 (Part F). Regenerate DELIBERATELY, with a written reason:

    BA2_REGEN_OPTION_POW_GOLDEN=1 python -m pytest tests/backtest/test_option_golden_run_pow.py
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict

from ba2_common.core.option_spread_model import SPREAD_MODEL_VERSION
from tests.backtest import test_option_golden_run as G
from tests.backtest.test_equity_golden_run import _diff_lines

GOLDEN_PATH = Path(__file__).with_name("golden") / "option_leap_pow_golden_run.json"
_ACCOUNT_ID = 9402

_CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    "option_spread_model": SPREAD_MODEL_VERSION,
}


def _quote_for(day_index: int, close: float):
    """Deterministic NBBO for the day_index-th premium bar: none on every third bar (the
    fallback must price those), else a spread that varies with the bar so the pin covers it."""
    if day_index % 3 == 2:
        return None, None
    half = round(0.05 + 0.03 * (day_index % 5), 4)
    return round(close - half, 4), round(close + half, 4)


class _QuotedReader:
    """The fixture reader with an NBBO on its bars. Everything else is the real reader."""

    def __init__(self, inner):
        self._inner = inner
        self._quotes = {}
        for t, d in enumerate(G._BAR_DAYS):
            self._quotes[d] = _quote_for(t, G._PREM_CLOSES[t])

    def get_bar(self, occ_symbol, as_of):
        bar = self._inner.get_bar(occ_symbol, as_of)
        if bar is None:
            return None
        bar = dict(bar)
        bar["bid"], bar["ask"] = self._quotes.get(as_of, (None, None))
        return bar

    def __getattr__(self, name):
        return getattr(self._inner, name)


def run_pow_golden_backtest() -> Dict[str, Any]:
    from tests.backtest.test_grid2_engine_paths import (
        _PlainBuyExpert, _harness, _launcher, _leap_rules)

    m = _launcher()
    entry_rules, exit_rules = _leap_rules(m, dte_floor=G._DTE_FLOOR)
    # The FULL entry concession (the F3 gene at 1.0): the buy quotes at the as-of touch the
    # fill will charge (``option_modelled_half_spread``), so the seam is exercised too. At the
    # 0.0 default a buy quoted at the mid must wait for the premium to FALL by a whole
    # half-spread overnight, and on this slow premium wave it never does -- the run would pin
    # an empty trade list.
    entry_rules[0]["actions"][0]["option_entry_cross"] = 1.0
    engine, account, ctx = _harness(
        symbol=G._SYMBOL, underlying_rows=G._underlying_rows(), chain_rows=G._chain_rows(),
        bar_rows=G._bar_rows(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, G._SYMBOL),
        start=G._START, end=G._END, account_id=_ACCOUNT_ID, cfg=dict(_CFG))
    account._options = _QuotedReader(account._options)
    try:
        engine.run()
        fp = G.fingerprint(account)
        fp.pop("sha256")
        fp.update(account.option_spread_record())
        fp["sha256"] = hashlib.sha256(
            json.dumps(fp, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return fp
    finally:
        ctx.__exit__(None, None, None)


_CACHE: Dict[str, Any] = {}


def _cached_run() -> Dict[str, Any]:
    if "fp" not in _CACHE:
        _CACHE["fp"] = run_pow_golden_backtest()
    return _CACHE["fp"]


def test_pow_golden_run_matches_pinned_fingerprint():
    fp = _cached_run()
    if os.environ.get("BA2_REGEN_OPTION_POW_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with GOLDEN_PATH.open("w", encoding="utf-8") as fh:
            json.dump({"_metadata": {
                "key": "O_LEAP",
                "spread_model": SPREAD_MODEL_VERSION,
                "note": "NEW PIN (plan 2026-09-22 Part F, approved 2026-09-23): the O_LEAP "
                        "golden fixture priced by the calibrated spread model, with a "
                        "deterministic NBBO on 2 of every 3 premium bars. Regenerate only "
                        "with a written justification.",
                "rebaselines": [{
                    "date": "2026-09-23",
                    "reason": "Option trade rows take exit_reason from the RECORDED close trigger (OptionCloseReason, 2026-09-22 BT/live option parity plan, Task 8 / Part C3), not the price-proximity guess: the opt_dte close had only a limit price and was labelled take_profit; it is dte_exit. No price, P&L, date or curve value moved.",
                    "before": {"exit_reason": "take_profit",
                               "sha256": "b1c34285bd1472fe5a9a35de491a3f36c18b0d3cd6a61c82e3137bd2d31989b2"},
                    "after": {"exit_reason": "dte_exit",
                              "sha256": "eed1032e2a0f4c720f22eb536fc0264f69950dc3d81e52a1ea694238adfa551b"},
                }],
            }, "fingerprint": fp}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return
    with GOLDEN_PATH.open("r", encoding="utf-8") as fh:
        golden = json.load(fh)["fingerprint"]
    assert fp["sha256"] == golden["sha256"], (
        "POW-MODEL OPTION BACKTEST RESULTS MOVED.\n" + _diff_lines(golden, fp) +
        f"\n  golden sha256={golden['sha256']}\n  now    sha256={fp['sha256']}")


def test_the_pow_run_trades_and_uses_both_spread_sources():
    fp = _cached_run()
    assert fp["option_spread_model"] == SPREAD_MODEL_VERSION
    assert fp["n_trades"] >= G.MIN_ROUND_TRIPS
    src = fp["option_spread_fill_sources"]
    assert src["quote"] + src["model"] >= 2, src          # entry AND exit were priced
    assert src["quote"] >= 1, f"no fill read the as-of quote: {src}"
    assert src["model"] >= 1, f"no fill used the calibrated fallback: {src}"


def test_the_pow_run_differs_from_the_zero_spread_golden():
    """If the spread model were not reaching the fills, this run would reproduce the legacy
    zero-spread golden's trades exactly."""
    fp = _cached_run()
    zero = G._cached_run()
    assert fp["trades"] and zero["trades"]
    assert [(t["entry_price"], t["exit_price"]) for t in fp["trades"]] != \
        [(t["entry_price"], t["exit_price"]) for t in zero["trades"]]
