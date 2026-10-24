"""Backfill-on-first-read of a symbol that is not in the Alpaca history build (plan 2026-10-24-history-source-task11,
Task 0 decision 5, owner 2026-10-24: "We should support new symbols").

A live provider (account keys) asked for an interval of a symbol with NO file:

1. NYSE symbols only (``ohlcv_final_bars.uses_nyse_calendar``); Alpaca having no bars for it is a named refusal.
2. RAW bars from the build's own history start for that interval (``live_state.build_history_start``) or the first bar
   Alpaca has for the symbol, whichever is LATER, to the newest finished session.
3. CONTINUITY (the top-up rule): every NYSE session from the first bar on must be present (daily), or have >= 1 regular
   5-minute bar (5min). Otherwise nothing is written and an ERROR names the missing sessions.
4. The corporate actions of the symbol are fetched once and judged by ``decide_splits`` (``corporate_actions.decide_events``,
   the rule the build and the daily refresh use). An undecided (review) event refuses; an event whose ex-date has no bar yet
   is pending and does not block. A symbol already in the manifest (a 5-minute backfill of a built or earlier backfilled
   symbol) is adjusted with the events the manifest records, so every file of a symbol is on ONE basis.
5. The adjusted series must show no unexplained overnight jump (``live_topup.unexplained_jump``).
6. Under the symbol lock: manifest entry + split table + certificate first (``register_backfilled_symbol``, origin
   ``live_backfill``), then the bar file atomically (``native_cache.write_timeseries``). A second read finds the file.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

import pandas as pd

from ba2_common.core import native_cache, ohlcv_final_bars
from ba2_common.core.market_calendar import regular_session_dates
from ba2_common.logger import logger
from ba2_providers.ohlcv.alpaca_history import build, corporate_actions, live_state
from ba2_providers.ohlcv.alpaca_history.live_topup import TopUpGap, TopUpRefused, unexplained_jump
from ba2_providers.ohlcv.alpaca_history.split_table import fmp_events_from_rows, load_overrides

_US_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}(-[A-Z])?$")      # a plain US ticker; pairs (BTC/USD) and suffixed names are not
LOOKAHEAD = timedelta(days=corporate_actions.LOOKAHEAD_DAYS)


class SymbolUnavailable(TopUpRefused):
    """Alpaca has no bars for the symbol (or it is not on the NYSE calendar): it cannot be backfilled."""


def backfill(symbol: str, key: str, *, fetcher: Any, now: datetime, ca_client: Any,
             fmp_rows: Optional[Callable[[date, date], List[dict]]] = None) -> pd.DataFrame:
    """Create the ``key`` ('1d' | '5min') file of ``symbol`` and return it (ADJUSTED, native layout). Raises a
    ``TopUpRefused`` subtype and writes nothing when the series cannot be proven complete and on one basis."""
    sym = symbol.upper()
    if not (ohlcv_final_bars.uses_nyse_calendar(sym) and _US_TICKER.match(sym)):
        raise SymbolUnavailable(f"{sym}: not an NYSE-calendar symbol; the Alpaca history does not backfill it")
    with live_state.locked_symbol(sym):
        existing = native_cache.read_timeseries(live_state.PROVIDER, sym, key, as_of=None)
        if existing is not None and not existing.empty:
            return existing                                    # another reader / process filled it meanwhile
        newest = ohlcv_final_bars.last_final_session_day(sym, now)
        if newest is None:
            raise SymbolUnavailable(f"{sym}: no finished session to backfill to")
        start = date.fromisoformat(live_state.build_history_start(key))
        if key == "1d":
            raw = fetcher.daily(sym, start, newest + timedelta(days=1))
        else:
            raw = fetcher.intraday_5min(sym, start, newest + timedelta(days=1))
        if raw is None or raw.empty:
            raise SymbolUnavailable(f"{sym}: Alpaca has no {key} bars from {start} to {newest}")
        raw = raw.sort_values("Date", kind="stable").reset_index(drop=True)
        if raw["Date"].duplicated().any():
            raise TopUpRefused(f"{sym} ({key}): Alpaca returned duplicate bar timestamps; nothing written")
        days = pd.to_datetime(raw["Date"]).dt.normalize()
        first_day = days.min().date()
        expected = regular_session_dates(first_day, newest)
        have = set(days.dt.date)
        stray = sorted(have - set(expected))
        if stray:
            raise TopUpRefused(f"{sym} ({key}): Alpaca returned bars outside NYSE sessions {stray[:5]}; nothing written")
        missing = [d for d in expected if d not in have]
        if missing:
            logger.error(f"{sym} ({key}): backfill refused, Alpaca has no bars for {len(missing)} session(s) after the "
                         f"first bar {first_day}: {[d.isoformat() for d in missing[:10]]}; nothing written, the next read retries")
            raise TopUpGap(sym, key, missing)

        try:
            events = live_state.manifest_events(sym)
        except KeyError:
            events = _decide_symbol_events(sym, key, raw, first_day, newest, now, fetcher, ca_client, fmp_rows)
        splits = [(d, f) for d, f, k in events if k == "split"]
        spinoffs = [(d, f) for d, f, k in events if k == "spinoff"]
        frame = build.finish(build.adjust(raw, splits, spinoffs), key)
        err = unexplained_jump(frame)
        if err is not None:
            raise corporate_actions.PendingCorporateAction(f"{sym} ({key}): backfill refused: {err}; nothing written")
        live_state.register_backfilled_symbol(
            sym, key, events, first_day.isoformat(), pd.to_datetime(frame["Date"]).max().date().isoformat(),
            len(frame), now=now, file_name=f"{sym}_{key}.parquet")
        native_cache.write_timeseries(live_state.PROVIDER, sym, key, frame)
        logger.info(f"{sym} ({key}): backfilled {len(frame)} bars from {first_day} to {newest} "
                    f"(events {events}); registered in the manifest as live_backfill")
        out = native_cache.read_timeseries(live_state.PROVIDER, sym, key, as_of=None)
        return out if out is not None else frame


def _decide_symbol_events(sym: str, key: str, raw: pd.DataFrame, first_day: date, newest: date, now: datetime, fetcher: Any,
                          ca_client: Any, fmp_rows) -> List[live_state.Event]:
    today = now.astimezone(timezone.utc).date()
    stats: Counter = Counter()
    month = ca_client.actions([sym], first_day, today + LOOKAHEAD)
    cand = corporate_actions.candidate_events(month, sym, stats)
    if not cand["splits"] and not cand["spinoffs"]:
        return []
    # the judge reads RAW DAILY bars; a 5-minute backfill of a new symbol fetches them too
    daily = raw if key == "1d" else fetcher.daily(sym, first_day, newest + timedelta(days=1))
    fmp_all = fmp_events_from_rows(fmp_rows(first_day, today), {}, stats) if fmp_rows is not None else []
    res = corporate_actions.decide_events(sym, cand, fmp_all, load_overrides(), stats, today=today,
                                          raw_fetcher=fetcher.daily, raw_native=daily)
    if res["review"]:
        rows = [(e["row"]["event_date"], e["row"]["verdict"]) for e in res["review"]]
        raise corporate_actions.PendingCorporateAction(
            f"{sym}: corporate action(s) {rows} need review (an override in split_overrides.json); backfill refused, "
            f"nothing written")
    return res["accepted"]
