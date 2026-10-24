"""Live corporate-actions refresh of the Alpaca history cache (plan 2026-10-24-history-source-task11, Task 6).

The build judged every split with ``split_table.decide_splits``; the live refresh judges the splits that happened AFTER
the build with the SAME function, the same constants and the same ``split_overrides.json`` (one rule, two callers).

* Candidates: Alpaca corporate actions (forward / reverse / unit splits, stock dividends, spin-offs) with an ex-date after the
  build's ``store_asof``, for the symbols the host passes (held / traded by ``alpaca`` history accounts), minus the events the
  manifest already records.
* An event the price cannot prove yet (no bar on or after its ex-date) stays ``pending``: not applied, retried next run.
* ``decide_splits`` accepts -> ``live_state.rebase_symbol(..., sync_split_table=True)``: files, then manifest + split table +
  gate certificate in one locked section. ``decide_splits`` sends to review -> NEVER applied; written to
  ``_meta/live_split_review.csv``, ERROR logged, and the top-up of that symbol REFUSES (``PendingCorporateAction``) until
  the owner adds an override. Nothing is skipped silently.
* ``_meta/corporate_actions_live.json`` records the run; a top-up requires it to be no older than the previous NYSE session
  (``assert_fresh``): a dead job stops the live reads loudly.
"""
from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from ba2_common.core.market_calendar import regular_session_dates
from ba2_common.logger import logger
from ba2_providers.ohlcv.alpaca_history import live_state
from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveBars, AlpacaLiveFetchError
from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction, TopUpRefused
from ba2_providers.ohlcv.alpaca_history.split_table import (
    REVIEW_COLUMNS, ListedSplit, alpaca_events_from_actions, decide_splits, fmp_events_from_rows, load_overrides,
    spinoff_events_from_actions)

STATUS_NAME = "corporate_actions_live.json"
REVIEW_CSV_NAME = "live_split_review.csv"
CA_PATH = "/v1/corporate-actions"
CA_TYPES = "forward_split,reverse_split,unit_split,stock_dividend,spin_off"
CA_PAGE_LIMIT = 1000
SYMBOLS_PER_REQUEST = 100
LOOKBACK_DAYS = 20          # raw bars before the first event of a symbol the judge needs (about 10 sessions)
LOOKAHEAD_DAYS = 14         # announced events this far ahead are listed as pending


class CorporateActionsStale(TopUpRefused):
    """The corporate-actions refresh has not run since the previous NYSE session: a live top-up could append bars across a
    split the cache does not know about."""


class AlpacaCorporateActions(AlpacaLiveBars):
    """``actions(symbols, start, end) -> {kind: [row, ...]}`` in the shape of the build's monthly files."""

    def actions(self, symbols: Sequence[str], start: date, end: date) -> Dict[str, List[dict]]:
        from ba2_providers.ohlcv.alpaca_history.live_fetch import alpaca_symbol
        merged: Dict[str, List[dict]] = {}
        names = [alpaca_symbol(s) for s in symbols]
        for i in range(0, len(names), SYMBOLS_PER_REQUEST):
            params = {"symbols": ",".join(names[i:i + SYMBOLS_PER_REQUEST]), "types": CA_TYPES,
                      "start": start.isoformat(), "end": end.isoformat(), "limit": CA_PAGE_LIMIT, "sort": "asc"}
            token, seen = None, set()
            while True:
                q = dict(params)
                if token:
                    q["page_token"] = token
                payload = self._request_json(CA_PATH, q)
                if (not isinstance(payload, dict) or "next_page_token" not in payload
                        or not isinstance(payload["corporate_actions"] if "corporate_actions" in payload else None, dict)):
                    raise AlpacaLiveFetchError(f"unexpected corporate-actions response shape: {str(payload)[:120]}")
                for kind, rows in payload["corporate_actions"].items():
                    if not isinstance(rows, list):
                        raise AlpacaLiveFetchError(f"corporate-actions kind {kind!r} is not a list")
                    merged.setdefault(kind, []).extend(rows)
                token = payload["next_page_token"]
                if not token:
                    break
                if token in seen:
                    raise AlpacaLiveFetchError(f"corporate-actions pagination loop at token {token}")
                seen.add(token)
        return merged


# --------------------------------------------------------------------------- status file
def _status_path() -> str:
    return os.path.join(live_state.cache_dir(), "_meta", STATUS_NAME)


def read_status() -> Optional[dict]:
    path = _status_path()
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_status(status: dict) -> None:
    live_state._atomic_json(_status_path(), status)
    rows = [r for entry in status["review"] for r in [entry["row"]]]
    path = os.path.join(live_state.cache_dir(), "_meta", REVIEW_CSV_NAME)
    with open(path + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol"] + [c for c in REVIEW_COLUMNS if c != "symbol"], extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    live_state.replace_with_retry(path + ".tmp", path)


def _previous_session(now: datetime) -> date:
    from zoneinfo import ZoneInfo
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    return regular_session_dates(today - timedelta(days=10), today - timedelta(days=1))[-1]


def assert_fresh(now: datetime) -> None:
    """``CorporateActionsStale`` unless the last full refresh ran on or after the previous NYSE session."""
    status = read_status()
    need = _previous_session(now)
    if status is None:
        raise CorporateActionsStale(
            f"no corporate-actions refresh has run ({_status_path()} missing); live Alpaca history reads are refused "
            f"until the daily job has run (needs a run on or after {need})")
    run = date.fromisoformat(status["run_date"])
    if run < need:
        raise CorporateActionsStale(
            f"the corporate-actions refresh last ran {run}, the previous session is {need}; live Alpaca history reads "
            f"are refused until the daily job runs (check the job log)")


def assert_not_in_review(symbol: str) -> None:
    live_state.assert_no_interrupted_rebase(symbol)
    status = read_status()
    if status is None:
        return
    for entry in status["review"]:
        if entry["symbol"] == symbol.upper():
            raise PendingCorporateAction(
                f"{symbol}: the corporate action {entry['row']['event_date']} (verdict {entry['row']['verdict']}) is "
                f"under review and not applied; add an override to split_overrides.json (see {REVIEW_CSV_NAME}); "
                f"nothing appended")


def _symbol_issue(symbol: str, now: datetime, newest_final: date) -> Optional[str]:
    """Why ``symbol`` may not be topped up right now, or None. It must have been CHECKED by a run no older than the previous
    session, have no review verdict, and no pending corporate action dated on or before the newest session the append would
    add (an ex-date bar must not be appended on the old basis)."""
    sym = symbol.upper()
    status = read_status()
    need = _previous_session(now)
    on = status["checked_on"][sym] if status is not None and sym in status["checked_on"] else None
    if on is None or date.fromisoformat(on) < need:
        return (f"{sym}: its corporate actions were not checked since the previous session ({need}); last check "
                f"{on or 'never'}")
    for entry in status["review"]:
        if entry["symbol"] == sym:
            return (f"{sym}: the corporate action {entry['row']['event_date']} (verdict {entry['row']['verdict']}) is under "
                    f"review and not applied; add an override to split_overrides.json (see {REVIEW_CSV_NAME})")
    for p in status["pending"]:
        if p["symbol"] == sym and p["date"] <= newest_final.isoformat():
            return f"{sym}: the corporate action dated {p['date']} ({p['kind']}) is pending and not applied"
    return None


def assert_clear_to_append(symbol: str, now: datetime, newest_final: date,
                           refresher: Optional[Callable[[str], None]] = None) -> None:
    """The gate of every live top-up: no interrupted re-base, the daily refresh alive (``assert_fresh``), and THIS symbol checked
    / without an unapplied corporate action up to the newest session. A symbol with an issue is refreshed on demand once
    (``refresher``) and re-judged; still an issue -> ``CorporateActionsStale`` / ``PendingCorporateAction``."""
    live_state.assert_no_interrupted_rebase(symbol)
    assert_fresh(now)
    issue = _symbol_issue(symbol, now, newest_final)
    if issue is not None and refresher is not None:
        logger.warning(f"{issue}; refreshing its corporate actions now")
        refresher(symbol)
        issue = _symbol_issue(symbol, now, newest_final)
    if issue is not None:
        raise (PendingCorporateAction if "review" in issue or "pending" in issue else CorporateActionsStale)(
            issue + "; nothing appended")


def build_age_sessions(today: date) -> int:
    """NYSE sessions between the build's ``store_asof`` and ``today``."""
    return len(regular_session_dates(date.fromisoformat(live_state.store_asof()) + timedelta(days=1), today))


def build_age_problem(today: date) -> Optional[str]:
    """A WARNING text when the build is older than one top-up chunk (``CHUNK_SESSIONS``): every symbol not read since is
    caught up in several requests on its first read (slower, never refused)."""
    from ba2_providers.ohlcv.alpaca_history.live_topup import CHUNK_SESSIONS
    n = build_age_sessions(today)
    if n <= CHUNK_SESSIONS:
        return None
    return (f"the Alpaca history build ends {live_state.store_asof()}, {n} sessions ago: symbols not read since are caught up "
            f"in chunks of {CHUNK_SESSIONS} sessions on their first read; consider pushing a fresher build")


def all_symbols_with_files() -> List[str]:
    """Every manifest symbol that has a daily file in the cache: the refresh covers ALL of them (a 3:2 split or a stock dividend
    stays inside the seam bounds, so only the corporate-action list can tell)."""
    have = {n[:-len("_1d.parquet")] for n in os.listdir(live_state.cache_dir()) if n.endswith("_1d.parquet")}
    return sorted(s for s in live_state._events_by_symbol() if s in have)


# --------------------------------------------------------------------------- the refresh
def _decide_frame(native: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"t": pd.to_datetime(native["Date"]), "o": native["Open"].astype("float64"),
                         "c": native["Close"].astype("float64")})


def refresh(symbols: Iterable[str], *, today: date, now: datetime, ca_client: Any, raw_fetcher: Callable,
            fmp_rows: Optional[Callable[[date, date], List[dict]]] = None,
            overrides: Optional[Sequence[dict]] = None, full_run: bool = True) -> dict:
    """Judge and apply the corporate actions after the build for ``symbols``; return (and write) the run report.

    ``ca_client.actions(symbols, start, end)`` -> kind -> rows; ``raw_fetcher(symbol, start, end_exclusive)`` -> RAW
    native daily frame; ``fmp_rows(start, end)`` -> FMP split-calendar rows (None: no FMP key, said in the report).
    ``full_run=False`` (one symbol, on demand) does not move ``run_date``."""
    overrides = list(overrides) if overrides is not None else load_overrides()
    if full_run:
        live_state.repair_consistency(now)              # an interrupted earlier live update must not outlive this run
    asof = date.fromisoformat(live_state.store_asof())
    known, unknown = [], []
    for s in sorted({x.strip().upper() for x in symbols if x and x.strip()}):
        try:
            live_state.manifest_events(s)
            known.append(s)
        except KeyError:
            unknown.append(s)
    report: Dict[str, Any] = {"run_date": today.isoformat(), "symbols_checked": known, "not_in_history": unknown,
                              "applied": [], "pending": [], "review": [], "errors": [],
                              "fmp": "used" if fmp_rows is not None else "no key: fmp_events=[]"}
    if unknown:
        logger.warning(f"corporate-actions refresh: {len(unknown)} symbol(s) not in the Alpaca build, skipped: {unknown[:10]}")
    stats: Counter = Counter()
    candidates: Dict[str, Dict[str, list]] = {}
    if known:
        month = ca_client.actions(known, asof + timedelta(days=1), today + timedelta(days=LOOKAHEAD_DAYS))
        listed = alpaca_events_from_actions([month], {}, stats)
        spins = spinoff_events_from_actions([month], {}, stats)
        recorded = {s: {d for d, _, _ in live_state.manifest_events(s)} for s in known}
        for ev in listed:
            if ev.symbol in recorded and ev.date.isoformat() not in recorded[ev.symbol]:
                candidates.setdefault(ev.symbol, {"splits": [], "spinoffs": []})["splits"].append(ev)
        for so in spins:
            if so.symbol in recorded and so.date.isoformat() not in recorded[so.symbol]:
                candidates.setdefault(so.symbol, {"splits": [], "spinoffs": []})["spinoffs"].append(so)
    fmp_all: List[ListedSplit] = []
    if fmp_rows is not None and candidates:
        first = min(e.date for c in candidates.values() for e in c["splits"] + c["spinoffs"])
        fmp_all = fmp_events_from_rows(fmp_rows(first - timedelta(days=5), today), {}, stats)
    for sym in sorted(candidates):
        try:
            _judge_symbol(sym, candidates[sym], fmp_all, overrides, stats, today=today, now=now, raw_fetcher=raw_fetcher,
                          report=report)
        except (AlpacaLiveFetchError, live_state.BasisRecordMismatch, live_state.LockTimeout) as e:
            logger.error(f"corporate-actions refresh failed for {sym}: {e}", exc_info=True)
            report["errors"].append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
    report["stats"] = dict(sorted(stats.items()))
    _merge_and_write(report, known, full_run)
    logger.info(f"corporate-actions refresh: {len(known)} symbol(s), applied {len(report['applied'])}, pending "
                f"{len(report['pending'])}, review {len(report['review'])}, errors {len(report['errors'])}")
    return report


def decide_events(sym: str, cand: Dict[str, list], fmp_all: List[ListedSplit], overrides: Sequence[dict], stats: Counter, *,
                  today: date, raw_fetcher: Callable, raw_native: Optional[pd.DataFrame] = None,
                  recorded_dates: Sequence[str] = ()) -> Dict[str, list]:
    """Judge the candidate events of ONE symbol with ``decide_splits`` (the ONE rule, shared by the daily refresh and the
    first-read backfill). ``raw_native``: the symbol's RAW daily frame (fetched around the first event when None).
    Returns ``{"accepted": [(date, factor, kind)], "review": [entry], "pending": [entry]}``; an event with no verdict is
    reported as pending, never dropped."""
    first = min(e.date for e in cand["splits"] + cand["spinoffs"])
    lookback = timedelta(days=LOOKBACK_DAYS)
    raw = raw_native if raw_native is not None else raw_fetcher(sym, first - lookback, today + timedelta(days=1))
    frame = _decide_frame(raw) if raw is not None and len(raw) else None
    last_bar = pd.to_datetime(raw["Date"]).max().date() if frame is not None else None
    out: Dict[str, list] = {"accepted": [], "review": [], "pending": []}
    provable_splits, provable_spins = [], []
    for kind, evs, bucket in (("split", cand["splits"], provable_splits), ("spinoff", cand["spinoffs"], provable_spins)):
        for e in evs:
            if last_bar is None or e.date > last_bar:
                out["pending"].append({"symbol": sym, "date": e.date.isoformat(), "kind": kind,
                                       "reason": "no bar on or after the ex-date yet"})
            else:
                bucket.append(e)
    if not provable_splits and not provable_spins:
        return out
    spinco_cache: Dict[str, Optional[pd.DataFrame]] = {}

    def spinco_frame(name: str) -> Optional[pd.DataFrame]:
        if name not in spinco_cache:
            r = raw_fetcher(name, first - lookback, today + timedelta(days=1))
            spinco_cache[name] = _decide_frame(r) if r is not None and len(r) else None
        return spinco_cache[name]

    fmp_events = [e for e in fmp_all if e.symbol == sym]
    accepted, review = decide_splits({sym: frame}, provable_splits, fmp_events, overrides, stats, breaks={},
                                     spinoffs=provable_spins, spinco_frame=spinco_frame)
    decided = set()
    done = set(recorded_dates)
    for a in accepted:
        decided.add(a["date"])
        if a["date"] not in done:
            out["accepted"].append((a["date"], float(a["factor"]), a["kind"]))
    for r in review:
        decided.add(r["event_date"])
        if r["applied"]:
            continue                                    # decided and applied through an accepted row above
        out["review"].append({"symbol": sym, "row": {**r, "symbol": sym}})
    for e in provable_splits + provable_spins:
        if e.date.isoformat() not in decided:
            logger.warning(f"corporate-actions: {sym} {e.date} ratio {e.ratio} produced no verdict (see stats)")
            out["pending"].append({"symbol": sym, "date": e.date.isoformat(), "kind": "?",
                                   "reason": "decide_splits gave no verdict (event outside the bars or ratio 1)"})
    return out


def _judge_symbol(sym: str, cand: Dict[str, list], fmp_all: List[ListedSplit], overrides: Sequence[dict], stats: Counter, *,
                  today: date, now: datetime, raw_fetcher: Callable, report: dict) -> None:
    recorded = [d for d, _, _ in live_state.manifest_events(sym)]
    res = decide_events(sym, cand, fmp_all, overrides, stats, today=today, raw_fetcher=raw_fetcher, recorded_dates=recorded)
    report["pending"] += res["pending"]
    for entry in res["review"]:
        r = entry["row"]
        report["review"].append(entry)
        logger.error(f"corporate-actions refresh: {sym} {r['event_date']} needs review (verdict {r['verdict']}: "
                     f"{r['reason']}); NOT applied, live top-ups of {sym} are refused until an override decides it")
    if res["accepted"]:
        live_state.rebase_symbol(sym, res["accepted"], now=now, sync_split_table=True)
        for d, f, k in res["accepted"]:
            report["applied"].append({"symbol": sym, "date": d, "factor": f, "kind": k})
        logger.info(f"corporate-actions refresh: {sym} re-based for {res['accepted']}")


def candidate_events(month: Dict[str, list], symbol: str, stats: Counter) -> Dict[str, list]:
    """The listed splits / spin-offs of ``symbol`` in an Alpaca corporate-actions answer."""
    listed = [e for e in alpaca_events_from_actions([month], {}, stats) if e.symbol == symbol]
    spins = [e for e in spinoff_events_from_actions([month], {}, stats) if e.symbol == symbol]
    return {"splits": listed, "spinoffs": spins}


def _merge_and_write(report: dict, checked: Sequence[str], full_run: bool) -> None:
    prior = read_status()
    touched = set(checked)
    if prior is None:
        prior = {"run_date": None, "run_at": None, "symbols_checked": [], "checked_on": {}, "applied": [], "pending": [],
                 "review": []}
    checked_on = dict(prior["checked_on"]) if "checked_on" in prior else {}
    stamp_day = report["run_date"]
    failed = {e["symbol"] for e in report["errors"]}
    for sym in checked:
        if sym not in failed:                       # a symbol whose check errored is NOT checked
            checked_on[sym] = stamp_day
    status = {
        "run_date": report["run_date"] if full_run else (prior["run_date"] if prior["run_date"] else None),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "symbols_checked": report["symbols_checked"] if full_run else sorted(set(prior["symbols_checked"]) | touched),
        "applied": report["applied"] if full_run else prior["applied"] + report["applied"],
        "pending": [p for p in prior["pending"] if p["symbol"] not in touched] + report["pending"],
        "review": [r for r in prior["review"] if r["symbol"] not in touched] + report["review"],
        "checked_on": checked_on,
        "errors": report["errors"], "fmp": report["fmp"], "not_in_history": report["not_in_history"],
    }
    if status["run_date"] is None:
        status["run_date"] = "1970-01-01"               # an on-demand refresh before any full run does not make the cache "fresh"
    _write_status(status)
    report["status_path"] = _status_path()


def refresh_symbol(symbol: str, *, ca_client: Any, raw_fetcher: Callable, fmp_rows=None, overrides=None,
                   today: Optional[date] = None, now: Optional[datetime] = None) -> dict:
    """On-demand refresh of ONE symbol (a top-up met an unexplained jump). Does not move ``run_date``."""
    from zoneinfo import ZoneInfo
    now = now or datetime.now(timezone.utc)
    today = today or now.astimezone(ZoneInfo("America/New_York")).date()
    return refresh([symbol], today=today, now=now, ca_client=ca_client, raw_fetcher=raw_fetcher, fmp_rows=fmp_rows,
                   overrides=overrides, full_run=False)
