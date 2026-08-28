"""Measure the parquet option reader's hot paths against the REAL local tree.

Ad-hoc probe (test_files/, not collected by pytest). Three measurements, all on real data:

  1. COLD/WARM/NEW-SCOPE underlying load — finding 1 (the cache keyed on too much).
  2. get_bar / get_quote us/call, warm, real contracts — finding 2 (the hot path).
  3. A chain fingerprint (contract symbols + prices + greeks) so a refactor can be shown
     not to move a single number.

Run:
  PYTHONPATH=packages/common:packages/providers:packages/experts:testplatform/backend:. \
  ./venv/bin/python test_files/probe_parquet_reader_perf.py [--fingerprint-out PATH]
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
from datetime import date

ROOT = os.path.expanduser("~/Documents/ba2/common/cache/TastyTradeOptionsProvider")
RATE = 0.045

# Deep names from the local tree (SPY/QQQ/IWM absent, AAPL has 1 partition).
DEEP = ["GOOG", "GOOGL", "BABA", "AMZN", "BAC", "SCHW", "INTC", "F", "T"]
WIDE = DEEP + ["AA", "AAL", "ABNB"]


def _spot(underlying: str, on: date):
    # Deterministic, cheap, and NOT flat: a real AsOfPriceSource is a dict lookup too.
    return 90.0 + (on.toordinal() % 17)


def _provider(scope: str):
    from app.services.backtest.parquet_options_provider import ParquetOptionsProvider
    return ParquetOptionsProvider(ROOT, spot_source=_spot, risk_free_rate=RATE,
                                  spot_scope=scope)


def measure_loads():
    import app.services.backtest.parquet_options_provider as pq

    print("== 1. underlying load: cold / warm / NEW SCOPE ==")
    pq.clear_worker_parquet_options_cache()
    p = _provider("scope-A")

    t0 = time.perf_counter(); p._u("GOOG"); cold = (time.perf_counter() - t0) * 1e3
    t0 = time.perf_counter(); p._u("GOOG"); warm = (time.perf_counter() - t0) * 1e6
    n_before = len(pq._WORKER_UNDERLYING_CACHE)
    p2 = _provider("scope-B")
    t0 = time.perf_counter(); p2._u("GOOG"); newscope = (time.perf_counter() - t0) * 1e3
    n_after = len(pq._WORKER_UNDERLYING_CACHE)
    raw_copies = getattr(pq, "_WORKER_RAW_CACHE", None)
    print(f"  GOOG cold        {cold:8.1f} ms")
    print(f"  GOOG warm        {warm:8.2f} us")
    print(f"  GOOG NEW SCOPE   {newscope:8.1f} ms   "
          f"(underlying-cache entries {n_before} -> {n_after}"
          + (f", raw-cache entries {len(raw_copies)}" if raw_copies is not None else "")
          + ")")

    pq.clear_worker_parquet_options_cache()
    p = _provider("scope-cold-sweep")
    times = []
    for sym in WIDE:
        gc.collect()
        t0 = time.perf_counter()
        p._u(sym)
        times.append((time.perf_counter() - t0) * 1e3)
    print(f"  mean cold load over {len(WIDE)} underlyings: "
          f"{statistics.mean(times):.1f} ms  (median {statistics.median(times):.1f})")
    return {"cold_ms": cold, "warm_us": warm, "newscope_ms": newscope,
            "mean_cold_ms": statistics.mean(times)}


def measure_hot_path(n_contracts=300, passes=50):
    import app.services.backtest.parquet_options_provider as pq

    print(f"\n== 2. hot path: GOOG, {n_contracts} contracts x {passes} passes, WARM ==")
    pq.clear_worker_parquet_options_cache()
    p = _provider("hot")
    u = p._u("GOOG")

    # Pick contracts that actually have a bar on the probe date.
    as_of = date(2023, 2, 15)
    as_of_ord = as_of.toordinal()
    picked = []
    for ci in range(len(u.c_occ)):
        if u.exact_row(ci, as_of_ord) >= 0:
            picked.append(u.c_occ[ci])
            if len(picked) >= n_contracts:
                break
    print(f"  contracts with an exact bar on {as_of}: {len(picked)}")

    # Warm every memo first (greeks, bar dicts, occ->underlying).
    for occ in picked:
        p.get_bar(occ, as_of)
        p.get_quote(occ, as_of)

    gc.collect()
    t0 = time.perf_counter()
    for _ in range(passes):
        for occ in picked:
            p.get_bar(occ, as_of)
    bar_us = (time.perf_counter() - t0) / (passes * len(picked)) * 1e6

    gc.collect()
    t0 = time.perf_counter()
    for _ in range(passes):
        for occ in picked:
            p.get_quote(occ, as_of)
    quote_us = (time.perf_counter() - t0) / (passes * len(picked)) * 1e6

    # bar_dict standalone (no dispatch / occ parse), and greeks on a memo hit.
    cis = [u.c_index[o] for o in picked]
    rows = [u.exact_row(ci, as_of_ord) for ci in cis]
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(passes):
        for i, ci in zip(rows, cis):
            u.bar_dict(i, ci, p.spot_source)
    bd_us = (time.perf_counter() - t0) / (passes * len(picked)) * 1e6

    gc.collect()
    t0 = time.perf_counter()
    for _ in range(passes):
        for i, ci in zip(rows, cis):
            u.greeks_tuple(i, ci, p.spot_source)
    gr_us = (time.perf_counter() - t0) / (passes * len(picked)) * 1e6

    # get_chain: the SELECTION path. One call per (symbol, bar) and it runs the as-of clamp
    # once per contract, so it is the other place the per-contract cost shows up.
    gc.collect()
    t0 = time.perf_counter()
    for _ in range(20):
        chain = p.get_chain("GOOG", as_of, expiry_min=date(2023, 1, 1),
                            expiry_max=date(2023, 12, 31))
    chain_ms = (time.perf_counter() - t0) / 20 * 1e3

    print(f"  get_chain        {chain_ms:6.2f} ms/call  (warm, {len(chain)} contracts)")
    print(f"  get_bar          {bar_us:6.2f} us/call")
    print(f"  get_quote        {quote_us:6.2f} us/call")
    print(f"  bar_dict         {bd_us:6.2f} us/call  (standalone)")
    print(f"  greeks_tuple     {gr_us:6.2f} us/call  (memo hit)")
    return {"get_bar_us": bar_us, "get_quote_us": quote_us,
            "bar_dict_us": bd_us, "greeks_us": gr_us, "get_chain_ms": chain_ms}


def fingerprint():
    """Deterministic digest of what the reader SAYS, for before/after comparison."""
    import app.services.backtest.parquet_options_provider as pq

    pq.clear_worker_parquet_options_cache()
    p = _provider("fingerprint")
    out = {}
    for sym in ["GOOG", "BAC", "INTC", "F", "T"]:
        for d in (date(2023, 1, 17), date(2023, 2, 15), date(2023, 3, 10)):
            chain = p.get_chain(sym, d, expiry_min=date(2023, 1, 1),
                                expiry_max=date(2023, 12, 31))
            out[f"chain:{sym}:{d}"] = [
                [c.symbol, c.strike, str(c.expiry), str(c.option_type), c.bid, c.ask, c.last,
                 c.implied_volatility, c.delta, c.gamma, c.theta, c.vega,
                 c.open_interest, c.volume]
                for c in chain]
            out[f"atm_iv:{sym}:{d}"] = p.get_atm_iv(sym, d)
            bars = {}
            for c in chain[:40]:
                b = p.get_bar(c.symbol, d)
                if b is not None:
                    bars[c.symbol] = {k: (str(v) if isinstance(v, date) else v)
                                      for k, v in sorted(b.items())}
                q = p.get_quote(c.symbol, d)
                if q is not None:
                    bars.setdefault(c.symbol, {})["__quote"] = [q.bid, q.ask, q.last]
            out[f"bars:{sym}:{d}"] = bars
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprint-out")
    ap.add_argument("--skip-perf", action="store_true")
    a = ap.parse_args()
    if not os.path.isdir(ROOT):
        sys.exit(f"no local parquet tree at {ROOT}")
    if not a.skip_perf:
        measure_loads()
        measure_hot_path()
    if a.fingerprint_out:
        fp = fingerprint()
        with open(a.fingerprint_out, "w") as fh:
            json.dump(fp, fh, sort_keys=True, indent=0)
        n = sum(len(v) for k, v in fp.items() if k.startswith("chain:"))
        print(f"\nfingerprint: {n} chain rows -> {a.fingerprint_out}")


if __name__ == "__main__":
    main()
