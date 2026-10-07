"""Re-run a stored row under ONE variant of the look-ahead fix, in memory (no DB row), capturing entries.

    python tools/measure_variant.py <variant>[@HH:MM][+skip] <row> --window START END --out RESULT.json

Variants (the final strict rule: the decision price is the close of the latest bar that has ENDED at T,
the daily history is the sessions finished at T):
  A         NO patch: whatever code is checked out. Run it from a checkout of the code BEFORE the fix to
            get today's behaviour (same-session daily bar) -- the "A" baseline.
  full      the fix as committed: daily-bar clamp + strict decision price + event-data rules
  bars      the same WITHOUT the event-data rules
  crude     the daily clamp with the price = prior DAILY close (the experimental switch), no event rules
  priceonly isolation: NO daily clamp anywhere, strict intraday price, no event rules
  sessiononly isolation: only the per-day-store session date (screener scan day, regime calendar,
            metric-store ATR) clamped; daily reads and the price legacy, no event rules
@HH:MM  overrides the row's stored entry time (everything else as stored).
+skip   the exact-bar-skip FIX active (a symbol is decided on the price knowable at T). Without it the
        PRE-fix behaviour is restored (universe = symbols with a bar stamped exactly at the clock,
        account price = the clock bar's close), so the earlier measurements stay comparable.

Writes RESULT.json (the summary), RESULT.json.trades.json and RESULT.json.entries.json (per entry: fill
price, TP, SL, quantity, decision price, next bar, clock bar).
"""
import contextlib
import sys
from pathlib import Path

spec = sys.argv[1]
newskip = spec.endswith("+skip")
spec = spec[:-5] if newskip else spec
variant, _, vtime = spec.partition("@")
sys.argv = ["rerun_stored_row.py"] + sys.argv[2:]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rerun_stored_row as R  # noqa: E402  (enters the backend)

if not newskip and variant not in ("A", "basetr"):
    from app.services.backtest import daily_engine as _DEL
    from app.services.backtest import backtest_account as _BAL

    def _legacy_universe(as_of, config, price_source):
        return [s for s in config["enabled_instruments"] if price_source.bar_at(s, as_of) is not None]

    _DEL.resolve_universe = _legacy_universe

    def _legacy_price(self, symbol_or_symbols, price_type="bid"):
        if isinstance(symbol_or_symbols, (list, tuple, set)):
            return {s: self._price.close_at(s) for s in symbol_or_symbols}
        px = self._price.close_at(symbol_or_symbols)
        if px is None:
            raise ValueError(f"No backtest price for {symbol_or_symbols} at {self._price.now()}")
        return px

    _BAL.BacktestAccount._get_instrument_current_price_impl = _legacy_price

    import bisect as _bis
    from app.services.backtest import price_source as _PSL

    def _legacy_decision_price(self, symbol, as_of):
        k = self._keys.get(symbol)
        if k is None or not len(k):
            return None
        key = _PSL._key64(as_of, self._interval)
        if not self._intraday:
            i = _bis.bisect_right(k, key) - 1
            return None if i < 0 else float(self._c[symbol][i])
        f = _bis.bisect_right(k, key - _PSL._interval_ns(self._interval)) - 1
        return None if f < 0 else float(self._c[symbol][f])

    _PSL.AsOfPriceSource.decision_price = _legacy_decision_price

if variant in ("bars", "crude", "priceonly", "sessiononly"):
    import ba2_common.core.knowability as K

    @contextlib.contextmanager
    def _noop(active):
        yield

    K.intraday_decisions = _noop
if variant == "crude":
    from app.services.backtest import daily_engine as DE
    from ba2_common.core.backtest_context import LiveProviderBundle

    DE._BacktestProviderBundle.price_at_date = lambda self, s, a: LiveProviderBundle.price_at_date(self, s, a)
if variant in ("priceonly", "sessiononly"):
    from app.services.backtest import price_source as PSM2
    PSM2.MemoizedOHLCVProvider.bind_price_source = lambda self, ps: None
    if variant == "priceonly":
        PSM2.AsOfPriceSource.daily_session_date = lambda self, a: PSM2._to_utc(a).date()
    else:
        from app.services.backtest import daily_engine as DE2
        from ba2_common.core.backtest_context import LiveProviderBundle as LPB2
        DE2._BacktestProviderBundle.price_at_date = lambda self, s, a: LPB2.price_at_date(self, s, a)
if vtime:
    from app.services.backtest import rerun_handler as RH
    _orig = RH.rebuild_config_for_backtest

    def _patched(*a, **k):
        cfg = _orig(*a, **k)
        sched = dict(cfg["run_schedule_override"])
        sched["times"] = [vtime]
        cfg["run_schedule_override"] = sched
        return cfg

    RH.rebuild_config_for_backtest = _patched

import json as _json  # noqa: E402
from app.services.backtest.backtest_account import BacktestAccount as _BA  # noqa: E402
from ba2_common.core.db import get_instance as _gi  # noqa: E402
from ba2_common.core.models import Transaction as _Tx  # noqa: E402
from ba2_common.core.types import AssetClass as _AC  # noqa: E402

_ENTRIES = {}
_orig_fill = _BA._apply_fill


def _cap_fill(self, order, fill_px, as_of):
    try:
        if (order.depends_on_order is None and getattr(order, "asset_class", None) != _AC.OPTION
                and order.transaction_id):
            tx = _gi(_Tx, order.transaction_id)
            nb = self._price.next_bar(order.symbol, as_of)
            _ENTRIES[order.transaction_id] = {
                "fill_px": float(fill_px), "sl": tx.stop_loss, "tp": tx.take_profit,
                "qty": float(order.quantity), "decision": str(as_of),
                "dec_price": None if not hasattr(self._price, "decision_price")
                else self._price.decision_price(order.symbol, as_of),
                "next_bar": None if nb is None else {k: float(v) for k, v in nb.items()},
                "clock_bar": self._price.bar_at(order.symbol, as_of)}
    except Exception as e:  # noqa: BLE001 - never perturb the run
        _ENTRIES[-1] = {"err": repr(e)}
    return _orig_fill(self, order, fill_px, as_of)


_BA._apply_fill = _cap_fill
from app.services.backtest import daily_backtest_handler as _H  # noqa: E402

_orig_run = _H.run_daily_backtest
_out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None


def _run_and_dump(config):
    res = _orig_run(config)
    if _out:
        with open(_out + ".trades.json", "w") as fh:
            _json.dump(res.get("trades") or [], fh, default=str)
        with open(_out + ".entries.json", "w") as fh:
            _json.dump({str(k): v for k, v in _ENTRIES.items()}, fh, default=str)
    return res


_H.run_daily_backtest = _run_and_dump
print("VARIANT", variant, "time", vtime or "stored", "skip-fix", newskip, flush=True)
sys.exit(R.main())
