"""BIT-IDENTITY PROBE for the sqlite option-store path (ad-hoc; NOT collected by pytest).

Runs the REAL ``DailyBacktestEngine`` over a fixture ``OptionsHistoryCache`` sqlite — the
harness of ``tests/backtest/test_options_e2e.py``, i.e. the full submit -> fill -> mark ->
ITM-expiry-settle lifecycle — and prints ONE canonical JSON digest of everything the run
produced: the engine's whole results dict, every per-bar balance snapshot, final cash/equity,
and every transaction and order row.

USE:
    PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend \
      ./venv/bin/python test_files/probe_options_store_identity.py            > before.json
    (apply the change)
    ... same command ...                                                      > after.json
    diff before.json after.json     # MUST be empty for the sqlite path

SENSITIVITY (do this too, or the empty diff proves nothing):
    ... --mutate premium > mutated.json ; diff before.json mutated.json       # MUST differ
``--mutate premium`` moves ONE premium bar's close by +0.01 (a 0.1% mark on one bar of one
contract) — about the smallest perturbation the engine can express — and the digest must
notice. ``--mutate epsilon`` moves the fill-bar OPEN by 1e-9.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ("packages/common", "packages/providers", "packages/experts", "testplatform/backend"):
    p = os.path.join(_REPO, _p)
    if p not in sys.path:
        sys.path.insert(0, p)

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface  # noqa: E402
from ba2_common.core.option_types import OptionLeg  # noqa: E402
from ba2_common.core.types import (  # noqa: E402
    OptionRight, OrderDirection, OrderRecommendation, Recommendation,
)

_OCC = "AAPL240207C00180000"
_STRIKE = 180.0
_EXPIRY = date(2024, 2, 7)
START = datetime(2024, 2, 1)
END = datetime(2024, 2, 7)

_AAPL_BARS = [
    (date(2024, 2, 1), 185, 186, 184, 185),
    (date(2024, 2, 2), 186, 188, 185, 187),
    (date(2024, 2, 5), 188, 191, 187, 190),
    (date(2024, 2, 6), 191, 196, 190, 195),
    (date(2024, 2, 7), 198, 201, 197, 200),
]
_PREMIUM_BARS = [
    (date(2024, 2, 2), 6.5, 6.8, 6.4, 6.7),
    (date(2024, 2, 5), 10.2, 10.7, 10.1, 10.5),
    (date(2024, 2, 6), 15.2, 15.7, 15.1, 15.5),
]

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


class _HoldStubExpert(MarketExpertInterface):
    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub HOLD expert for the option-store identity probe."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=0.0,
                              current_price=None, details="hold", raw_outputs={})


def _underlying_rows(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


def _seed_cache(db_path: str, mutate: str) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    premium = [list(r) for r in _PREMIUM_BARS]
    if mutate == "premium":
        premium[1][4] += 0.01          # 2024-02-05 close 10.5 -> 10.51
    elif mutate == "epsilon":
        premium[0][1] += 1e-9          # fill bar open 6.5 -> 6.500000001

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows("AAPL", START.date().isoformat(), [{
        "occ_symbol": _OCC, "option_type": "call", "strike": _STRIKE,
        "expiry": _EXPIRY.isoformat(), "bid": 6.4, "ask": 6.6, "last": 6.5, "iv": 0.25}])
    cache.write_bar_rows([{
        "occ_symbol": _OCC, "date": d.isoformat(), "open": o, "high": h, "low": low,
        "close": c, "volume": 400, "underlying": "AAPL", "option_type": "call",
        "strike": _STRIKE, "expiry": _EXPIRY.isoformat()}
        for (d, o, h, low, c) in premium])


#: WALL-CLOCK columns, dropped from the digest. They are ``datetime.now()`` stamps written by
#: the ORM, so they differ between two runs of the SAME code and would drown the signal. Every
#: other timestamp in the digest is a BAR date (deterministic) and is kept. Verified by
#: ``--selfcheck``: two in-process runs of identical code produce identical digests.
_WALL_CLOCK_KEYS = {"created_at", "updated_at", "last_updated", "timestamp_utc"}


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)                 # full precision, no formatting loss
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))
                if k not in _WALL_CLOCK_KEYS}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value"):
        return _jsonable(v.value)
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    return str(v)


def run(mutate: str) -> dict:
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    tmp = tempfile.mkdtemp(prefix="optprobe-")
    cache_db = os.path.join(tmp, "options_cache.sqlite")
    _seed_cache(cache_db, mutate)

    # THE SEAM UNDER TEST: build the reader exactly the way run_daily_backtest does. On dev
    # that is the direct HistoricalOptionsProvider construction; on the branch it is
    # options_store.build_options_provider with the sqlite default. Both must produce the
    # same numbers below.
    try:
        from app.services.backtest.options_store import build_options_provider
        provider = build_options_provider({"options_cache_db": cache_db}, price_source=None)
        seam = "options_store.build_options_provider(default)"
    except ImportError:
        from app.services.backtest.options_provider import HistoricalOptionsProvider
        provider = HistoricalOptionsProvider(cache_db)
        seam = "HistoricalOptionsProvider(cache_db)"

    account_id = expert_id = 71
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("options-store-identity-probe")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        ruleset_id = seed_enter_long_ruleset(name=f"probe-{account_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_HoldStubExpert",
                             enter_market_ruleset_id=ruleset_id, instance_id=expert_id)

        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", _underlying_rows(_AAPL_BARS))
        ps.set_clock(START)

        account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
        resolver.register_account(account_id, account)
        expert = _HoldStubExpert(expert_id)
        resolver.register_expert(expert_id, expert)

        account.submit_option_order(
            legs=[OptionLeg(contract_symbol=_OCC, side=OrderDirection.BUY,
                            position_intent="buy_to_open", option_type=OptionRight.CALL,
                            strike=_STRIKE, expiry=_EXPIRY, underlying="AAPL")],
            quantity=1, order_type="market", option_strategy="long_call")

        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, expert.settings, ruleset_id)],
            price_source=ps,
            config={"start_date": START, "end_date": END,
                    "enabled_instruments": ["AAPL"], "seed": 42},
            indicator_provider=object())
        results = engine.run()

        from ba2_common.core.db import get_all_instances
        from ba2_common.core.models import TradingOrder, Transaction
        orders = [o.model_dump() for o in get_all_instances(TradingOrder)]
        txns = [t.model_dump() for t in get_all_instances(Transaction)]

        return {
            "seam": seam,
            "mutate": mutate,
            "results": _jsonable(results),
            "balance_history": _jsonable(account.get_balance_history()),
            "cash": repr(account.get_balance()),
            "equity": repr(account.equity()),
            "option_positions": _jsonable(
                [p.__dict__ for p in account.get_option_positions()]),
            "positions": _jsonable(account.get_positions()),
            "orders": _jsonable(sorted(orders, key=lambda r: str(r.get("id")))),
            "transactions": _jsonable(sorted(txns, key=lambda r: str(r.get("id")))),
        }
    finally:
        ctx.__exit__(None, None, None)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", default="none", choices=["none", "premium", "epsilon"])
    # The stack logs to STDOUT, so the digest goes to a FILE — a redirected stdout would
    # interleave log lines into the artifact being diffed.
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    d = run(a.mutate)
    with open(a.out, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    sys.stderr.write(f"digest written to {a.out}\n")
