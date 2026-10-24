"""Live backfill of a symbol (or one interval of it) the Alpaca history build does not hold (plan
2026-10-24-history-source-task11, Task 0 decisions 5 and 6; owner 2026-10-24: "We should support new symbols", "It should not
download more than needed. If we need that symbol for BT it will be downloaded aside.").

``ensure_coverage(symbol, key, want_start)`` is called with the start date the CALLER asked for (indicator warm-up already in
it: ``PandasIndicatorCalc`` asks 365 days before the visible window, DeterministicScorer 600 days, ...):

* NO file for that interval -> fetch ONLY that interval (daily OR 5-minute, never both) for ONLY ``[want_start, newest finished
  session]``; the file starts at the symbol's first bar when that is later than ``want_start`` (an IPO).
* a PARTIAL live file whose ``covered_from`` is later than ``want_start`` -> extend it BACKWARDS by just the missing range,
  contiguous with the existing first bar. Later sessions arrive through the normal top-up.
* a built (full) file, or a partial one already covering ``want_start`` -> nothing.

Rules of every fetch: NYSE-calendar US tickers only; CONTINUITY (every NYSE session from the first bar to the end of the range
present for daily, >= 1 regular bar for 5-minute, else nothing is written and an ERROR names the missing sessions); corporate
actions fetched once for the range and judged by ``decide_splits`` (``corporate_actions.decide_events``); a symbol already in the
manifest is adjusted with the manifest's events (one basis per symbol); an unexplained overnight jump refuses. The manifest entry
is marked ``origin: live_backfill`` and each interval carries ``partial: true`` + ``covered_from``, so the backtest reader
(``AlpacaHistoryOHLCVProvider``, keyless) REFUSES a window that starts before the coverage ("downloaded aside" by a rebuild). Under
the symbol lock the manifest + split table + certificate are written BEFORE the bar file.
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


class PartialLiveBackfill(ValueError):
    """A backtest asked a live-backfilled (partial) file for a window that starts before what was downloaded."""


def check_backtest_window(symbol: str, key: str, want_start: date) -> None:
    """Refuse (named, loudly) a read starting before the live-backfilled coverage of ``symbol``/``key``."""
    stamp = live_state.partial_coverage(symbol, key)
    if stamp is None:
        return
    want_start = max(want_start, date.fromisoformat(live_state.build_history_start(key)))   # nothing earlier exists in a build
    if want_start < date.fromisoformat(stamp["covered_from"]):
        raise PartialLiveBackfill(
            f"{symbol} ({key}): the Alpaca history of this symbol is a PARTIAL live backfill covering only "
            f"{stamp['covered_from']} onwards (first bar {stamp['first']}); a window starting {want_start} needs the full "
            f"history. Rebuild or download it aside (tools/prepare_history.py --source alpaca --symbols {symbol}) before running "
            f"a backtest on it")


def _prefetch_checks(sym: str, key: str, raw: pd.DataFrame, last_expected: date) -> date:
    """Order, uniqueness, session and CONTINUITY checks; returns the first bar's day."""
    raw["Date"] = pd.to_datetime(raw["Date"])
    if raw["Date"].duplicated().any():
        raise TopUpRefused(f"{sym} ({key}): Alpaca returned duplicate bar timestamps; nothing written")
    days = raw["Date"].dt.normalize()
    first_day = days.min().date()
    expected = regular_session_dates(first_day, last_expected)
    have = set(days.dt.date)
    stray = sorted(have - set(expected))
    if stray:
        raise TopUpRefused(f"{sym} ({key}): Alpaca returned bars outside NYSE sessions {stray[:5]}; nothing written")
    missing = [d for d in expected if d not in have]
    if missing:
        logger.error(f"{sym} ({key}): backfill refused, Alpaca has no bars for {len(missing)} session(s) after the first bar "
                     f"{first_day}: {[d.isoformat() for d in missing[:10]]}; nothing written, the next read retries")
        raise TopUpGap(sym, key, missing)
    return first_day


def _fetch(fetcher: Any, sym: str, key: str, start: date, end_excl: date) -> pd.DataFrame:
    raw = fetcher.daily(sym, start, end_excl) if key == "1d" else fetcher.intraday_5min(sym, start, end_excl)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["Date"])
    return raw.sort_values("Date", kind="stable").reset_index(drop=True)


def ensure_coverage(symbol: str, key: str, want_start: date, *, fetcher: Any, now: datetime, ca_client: Any,
                    fmp_rows: Optional[Callable[[date, date], List[dict]]] = None) -> bool:
    """Make the ``key`` ('1d' | '5min') file of ``symbol`` cover ``want_start`` (see the module doc). True when it wrote."""
    sym = symbol.upper()
    if not _US_TICKER.match(sym) or not ohlcv_final_bars.uses_nyse_calendar(sym):
        raise SymbolUnavailable(f"{sym}: not an NYSE-calendar US ticker; the Alpaca history does not backfill it")
    with live_state.locked_symbol(sym):
        existing = native_cache.read_timeseries(live_state.PROVIDER, sym, key, as_of=None)
        have_file = existing is not None and not existing.empty
        stamp = None
        if have_file:
            stamp = live_state.partial_coverage(sym, key)
            if stamp is None:
                return False                                   # a built (full) file
        start = max(want_start, date.fromisoformat(live_state.build_history_start(key)))   # nothing earlier exists in a build
        if have_file and start >= date.fromisoformat(stamp["covered_from"]):
            return False                                       # already covering the request
        newest = ohlcv_final_bars.last_final_session_day(sym, now)
        if newest is None:
            raise SymbolUnavailable(f"{sym}: no finished session to backfill to")
        if have_file:
            return _extend_backwards(sym, key, start, existing, stamp, fetcher, now, ca_client, fmp_rows)
        if start > newest:
            raise SymbolUnavailable(f"{sym}: requested start {start} is after the newest finished session {newest}")
        raw = _fetch(fetcher, sym, key, start, newest + timedelta(days=1))
        if raw.empty:
            raise SymbolUnavailable(f"{sym}: Alpaca has no {key} bars from {start} to {newest}")
        first_day = _prefetch_checks(sym, key, raw, newest)
        try:
            events = live_state.manifest_events(sym)
        except KeyError:
            events = _decide_symbol_events(sym, key, raw, first_day, newest, now, fetcher, ca_client, fmp_rows)
        frame = _adjusted(sym, key, raw, events)
        live_state.register_backfilled_symbol(
            sym, key, events, first_day.isoformat(), pd.to_datetime(frame["Date"]).max().date().isoformat(), len(frame),
            now=now, file_name=f"{sym}_{key}.parquet", covered_from=start.isoformat())
        native_cache.write_timeseries(live_state.PROVIDER, sym, key, frame)
        logger.info(f"{sym} ({key}): backfilled {len(frame)} bars {first_day}..{newest} (asked from {want_start}; "
                    f"events {events}); registered in the manifest as a partial live_backfill")
        return True


def _adjusted(sym: str, key: str, raw: pd.DataFrame, events: List[live_state.Event]) -> pd.DataFrame:
    splits = [(d, f) for d, f, k in events if k == "split"]
    spinoffs = [(d, f) for d, f, k in events if k == "spinoff"]
    frame = build.finish(build.adjust(raw, splits, spinoffs), key)
    err = unexplained_jump(frame)
    if err is not None:
        raise corporate_actions.PendingCorporateAction(f"{sym} ({key}): backfill refused: {err}; nothing written")
    return frame


def _extend_backwards(sym: str, key: str, start: date, existing: pd.DataFrame, stamp: dict, fetcher: Any, now: datetime,
                      ca_client: Any, fmp_rows) -> bool:
    ex = existing.sort_values("Date").reset_index(drop=True)
    first_old = pd.to_datetime(ex["Date"]).min().date()
    events = live_state.manifest_events(sym)
    raw = _fetch(fetcher, sym, key, start, first_old) if start < first_old else pd.DataFrame(columns=["Date"])
    if raw.empty:
        # nothing earlier exists (the symbol is newer than the request): only the coverage claim moves
        live_state.register_backfilled_symbol(
            sym, key, events, stamp["first"], stamp["last"], stamp["bars"], now=now,
            file_name=f"{sym}_{key}.parquet", covered_from=start.isoformat())
        logger.info(f"{sym} ({key}): no bars before {first_old}; coverage now claimed from {start}")
        return True
    prev_session = regular_session_dates(first_old - timedelta(days=12), first_old - timedelta(days=1))[-1]
    first_day = _prefetch_checks(sym, key, raw, prev_session)       # contiguous with the existing first bar
    have_dates = {d for d, _, _ in events}
    new_events = _decide_symbol_events(sym, key, raw, first_day, prev_session, now, fetcher, ca_client, fmp_rows,
                                       known=have_dates)
    all_events = events + [e for e in new_events if e[0] not in have_dates]
    chunk = _adjusted(sym, key, raw, all_events)
    seam = pd.concat([chunk.iloc[-1:], ex[list(chunk.columns)].iloc[:1]], ignore_index=True)
    err = unexplained_jump(seam)
    if err is not None:
        raise corporate_actions.PendingCorporateAction(
            f"{sym} ({key}): backward extension refused at the seam: {err}; nothing written")
    merged = pd.concat([chunk, ex[list(chunk.columns)]], ignore_index=True)
    live_state.register_backfilled_symbol(
        sym, key, all_events, first_day.isoformat(), stamp["last"], len(merged), now=now,
        file_name=f"{sym}_{key}.parquet", covered_from=start.isoformat())
    native_cache.write_timeseries(live_state.PROVIDER, sym, key, merged)
    logger.info(f"{sym} ({key}): extended backwards by {len(chunk)} bars {first_day}..{prev_session} (asked from {start})")
    return True


def _decide_symbol_events(sym: str, key: str, raw: pd.DataFrame, first_day: date, last_day: date, now: datetime, fetcher: Any,
                          ca_client: Any, fmp_rows, known=()) -> List[live_state.Event]:
    today = now.astimezone(timezone.utc).date()
    stats: Counter = Counter()
    month = ca_client.actions([sym], first_day, min(today, last_day + LOOKAHEAD))
    cand = corporate_actions.candidate_events(month, sym, stats)
    known = set(known)
    cand = {k: [e for e in v if e.date.isoformat() not in known] for k, v in cand.items()}
    if not cand["splits"] and not cand["spinoffs"]:
        return []
    # the judge reads RAW DAILY bars; a 5-minute backfill fetches the same range of daily bars for it
    daily = raw if key == "1d" else _fetch(fetcher, sym, "1d", first_day, last_day + timedelta(days=1))
    fmp_all = fmp_events_from_rows(fmp_rows(first_day, today), {}, stats) if fmp_rows is not None else []
    res = corporate_actions.decide_events(sym, cand, fmp_all, load_overrides(), stats, today=today,
                                          raw_fetcher=fetcher.daily, raw_native=daily)
    if res["review"]:
        rows = [(e["row"]["event_date"], e["row"]["verdict"]) for e in res["review"]]
        raise corporate_actions.PendingCorporateAction(
            f"{sym}: corporate action(s) {rows} need review (an override in split_overrides.json); backfill refused, "
            f"nothing written")
    return res["accepted"]
