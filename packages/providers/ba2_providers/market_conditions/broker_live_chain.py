"""LIVE iv-v1 reader over a BROKER quote-source account (the prior session's chain, captured after the close).

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` section 6.6. Gates trained on the Alpha Vantage
store may be served live by a quote-source account whose snapshot is CERTIFIED equal to that vendor's
end-of-day chain (``market_conditions.LIVE_SERVING_PAIRINGS``; TastyTrade: 3,478 contracts on
2026-11-13, 100% identical quotes and fields).

THE SAME DOWNSTREAM CODE as the Alpha Vantage reader: the snapshot's quotes are handed to the store's own
acceptance rule (``alphavantage.map_chain``, as AV-shaped rows), filtered by the store reader's
``chain_quotes_from_frame``, combined with the ohlcv-v1 live window, and computed by
``market_conditions.compute_iv_v1``. Cache format, per-pass refusal memo, capture/replay evidence and
the failure policy are the shared ones (``av_live_chain`` / ``market_condition_iv_live``).

CAPTURE, NOT ON DEMAND. A broker snapshot is the prior session's chain only between that session's
close and the next open; once the next session quotes, it is gone and there is no historical quote
endpoint to backfill from. So:

* :func:`capture_chains` (the live capture job, off the decision path) fetches each symbol of the
  bounded universe of the iv-gated experts, VALIDATES the snapshot on its data
  (``option_chain_quotes.validate_prior_session_snapshot``: quotes of S, nothing after S, no flush) and
  caches only a valid one -- with the snapshot facts (summary-date counts, updated-at range, account
  id) in the parquet metadata. A refused snapshot is never cached and is retried on the next attempt.
  Every attempt is recorded in the session's capture manifest.
* :class:`BrokerLiveChainReader` (the decision pass) only READS that cache. A symbol without a cached
  chain is refused with the manifest's explicit reason: no capture ran for the session (the app was
  down from close to open), the symbol was not in the captured universe (a screener pick outside
  ``screener_symbol_universe``), or its capture was refused (and why).
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from ba2_common.core.market_condition_iv_live import ChainLiveReader, IvEvidence
from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
from ba2_common.core.market_conditions import CHAIN_COMPUTE_BY_PROFILE

from ba2_providers.market_conditions.av_live_chain import (
    _COLUMNS, _STOP, DailyWindowMixin, Stopped, _replace, chain_path, chain_quotes, maintain_cache, read_cached,
)

__all__ = ["BrokerLiveChainReader", "capture_chains", "broker_chain_dir", "snapshot_rows", "map_snapshot",
           "read_snapshot_facts", "capture_manifest", "CAPTURE_PAUSE_S"]

#: Pause between two symbols of one capture run (gentle on the broker; the run is off the decision path).
CAPTURE_PAUSE_S = 0.2
_META_KEY = b"ba2_broker_snapshot"
_MANIFEST_LOCK = threading.Lock()


def broker_chain_dir(account_id: int) -> str:
    return os.path.join("market_conditions_live", "broker", f"account_{int(account_id)}")


def _manifest_path(cache_root: str, account_id: int, session: date) -> str:
    return os.path.join(cache_root, broker_chain_dir(account_id), "_capture", f"{session.isoformat()}.json")


def capture_manifest(cache_root: str, account_id: int, session: date) -> Optional[Dict[str, Any]]:
    path = _manifest_path(cache_root, account_id, session)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"unreadable": True}


def _update_manifest(cache_root: str, account_id: int, session: date, serving_kind: str,
                     results: Dict[str, Dict[str, Any]], run: Dict[str, Any]) -> None:
    path = _manifest_path(cache_root, account_id, session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _MANIFEST_LOCK:
        current = capture_manifest(cache_root, account_id, session) or {}
        if current.get("unreadable"):
            current = {}
        symbols = dict(current.get("symbols") or {})
        for sym, res in results.items():
            if symbols.get(sym, {}).get("status") == "captured":
                continue                          # a valid capture is final for its session (keeps its facts)
            symbols[sym] = res
        out = {"session": session.isoformat(), "account_id": int(account_id), "serving_kind": serving_kind,
               "symbols": symbols, "runs": (list(current.get("runs") or []) + [run])[-50:]}
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, sort_keys=True, default=str)
        _replace(tmp, path)


def snapshot_rows(quotes: Iterable[Any], session: date) -> list:
    """The validated quotes as AV-shaped rows, so the store's own ``map_chain`` accepts and maps them."""
    rows = []
    for q in quotes:
        rows.append({"contractID": q.occ, "date": session.isoformat(), "bid": q.bid, "ask": q.ask,
                     "last": q.last, "volume": q.volume})
    return rows


def map_snapshot(symbol: str, session: date, quotes: Iterable[Any]):
    """``(frame with the cache columns, ChainStats)`` through the shared acceptance rule."""
    from ba2_providers.options.alphavantage import map_chain

    frame, stats = map_chain(symbol, session, snapshot_rows(quotes, session))
    return frame[list(_COLUMNS)], stats


def _write_cached(cache_root: str, account_id: int, symbol: str, session: date, frame, facts: Dict[str, Any]) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = chain_path(cache_root, symbol, session, broker_chain_dir(account_id))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    table = pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
    meta = dict(table.schema.metadata or {})
    meta[_META_KEY] = json.dumps(facts, sort_keys=True, default=str).encode("utf-8")
    table = table.replace_schema_metadata(meta)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        pq.write_table(table, tmp, compression="zstd")
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


def read_snapshot_facts(path: str) -> Optional[Dict[str, Any]]:
    import pyarrow.parquet as pq

    meta = pq.read_schema(path).metadata or {}
    raw = meta.get(_META_KEY)
    return json.loads(raw.decode("utf-8")) if raw else None


class BrokerLiveChainReader(DailyWindowMixin, ChainLiveReader):
    """Serves iv-v1 from the chains :func:`capture_chains` cached for a quote-source account. Never fetches."""

    option_source = "broker"

    def __init__(self, profile: str = "iv-v1", *, account_id: int, serving_kind: str,
                 cache_root: Optional[str] = None, source_profile: str = SOURCE_PROFILE_FMP_DAILY):
        super().__init__(profile)
        if cache_root is None:
            from ba2_common.core import native_cache
            cache_root = native_cache.CACHE_FOLDER
        self.cache_root = os.fspath(cache_root)
        self.source_profile = source_profile
        self.account_id = int(account_id)
        self.serving_kind = serving_kind
        self.chain_dir = broker_chain_dir(self.account_id)

    def _missing_reason(self, symbol: str, session: date) -> Tuple[str, str]:
        man = capture_manifest(self.cache_root, self.account_id, session)
        if man is None:
            return (f"no broker chain capture ran for session {session} between its close and the next open "
                    f"(was the app down?); a broker snapshot cannot be backfilled", "not_captured_session")
        entry = (man.get("symbols") or {}).get(symbol)
        if entry is None:
            return (f"{symbol} is not in the captured universe for {session} (the union of the iv-gated experts' "
                    f"bounded universes: fixed instruments / screener_symbol_universe); a screener pick outside "
                    f"it cannot be served", "not_in_captured_universe")
        return (f"the {session} chain of {symbol} was not captured: {entry.get('status')}: {entry.get('reason')}",
                "capture_refused")

    def _evidence(self, symbol: str, session: date) -> IvEvidence:
        frame = read_cached(self.cache_root, symbol, session, self.chain_dir)
        if frame is None:
            reason, kind = self._missing_reason(symbol, session)
            return self._refusal(symbol, session, reason, kind)
        facts = read_snapshot_facts(chain_path(self.cache_root, symbol, session, self.chain_dir))
        chain = chain_quotes(frame, symbol, session)
        window, wstatus, wreason = self._window(symbol, session)
        row = CHAIN_COMPUTE_BY_PROFILE[self.profile](session, chain, window, wstatus, wreason)
        return IvEvidence(row=row, chain=chain, window=window, window_status=wstatus, window_reason=wreason,
                          session=session, source_facts=facts)


def capture_chains(symbols: Iterable[str], session: date, *, snapshot_fn: Callable[[str], Any], account_id: int,
                   serving_kind: str, cache_root: Optional[str] = None, deadline: Optional[float] = None,
                   stop: Optional[threading.Event] = None, pause_s: float = CAPTURE_PAUSE_S,
                   jitter_s: float = 0.0, log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Capture ``session``'s chain for every symbol not cached yet (resumable: the cache is the record).

    ``snapshot_fn(symbol)`` returns an ``OptionChainQuoteSnapshot`` (the account's
    ``get_option_chain_quote_snapshot`` bound to the window). Each snapshot is validated on its data; only
    a valid one is mapped (shared acceptance rule) and cached. One symbol's failure never stops the run;
    an ``OptionChainQuotesUnsupported`` account stops it (every remaining symbol records the reason).
    Stops at ``deadline`` (``time.monotonic()``) or on ``stop`` (default: the module stop flag set by
    ``av_live_chain.request_stop`` -- JobManager stop/shutdown, interpreter exit)."""
    from ba2_common.core.option_chain_quotes import (
        OptionChainQuotesFetchError, OptionChainQuotesUnsupported, validate_prior_session_snapshot,
    )

    stop = _STOP if stop is None else stop
    if cache_root is None:
        from ba2_common.core import native_cache
        cache_root = native_cache.CACHE_FOLDER
    cache_root = os.fspath(cache_root)
    chain_dir = broker_chain_dir(account_id)
    t0 = time.monotonic()
    started = datetime.now(timezone.utc).isoformat()
    report: Dict[str, Any] = {"session": session.isoformat(), "account_id": int(account_id), "symbols": 0,
                              "cached": 0, "captured": 0, "refused": {}, "failed": {}, "remaining": [],
                              "stopped": False, "unsupported": ""}
    try:
        report["maintenance"] = maintain_cache(cache_root, reference=session, chain_dir=chain_dir)
    except (OSError, ValueError) as e:
        report["maintenance"] = {"error": f"{type(e).__name__}: {e}"}
    syms = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    report["symbols"] = len(syms)
    results: Dict[str, Dict[str, Any]] = {}
    now = lambda: datetime.now(timezone.utc).isoformat()   # noqa: E731
    try:
        for i, sym in enumerate(syms):
            if stop.is_set():
                report["remaining"], report["stopped"] = syms[i:], True
                break
            if deadline is not None and time.monotonic() >= deadline:
                report["remaining"] = syms[i:]
                break
            try:
                if read_cached(cache_root, sym, session, chain_dir) is not None:
                    report["cached"] += 1
                    results.setdefault(sym, {"status": "captured", "reason": "already cached", "at": now()})
                    continue
                if i and pause_s:
                    stop.wait(pause_s + (random.uniform(0.0, jitter_s) if jitter_s else 0.0))
                snap = snapshot_fn(sym)
                verdict = validate_prior_session_snapshot(snap, session)
                if not verdict.ok:
                    report["refused"][sym] = verdict.reason
                    results[sym] = {"status": "refused", "kind": verdict.kind, "reason": verdict.reason,
                                    "at": now(), "facts": verdict.facts}
                    continue
                frame, stats = map_snapshot(sym, session, verdict.kept)
                facts = dict(verdict.facts, mapped_rows=int(len(frame)), dropped_by_acceptance=int(
                    stats.rows_in - stats.rows_out), serving_kind=serving_kind)
                _write_cached(cache_root, account_id, sym, session, frame, facts)
                report["captured"] += 1
                results[sym] = {"status": "captured", "reason": "", "at": now(), "facts": facts}
            except OptionChainQuotesUnsupported as e:
                report["unsupported"] = str(e)
                for rest in syms[i:]:
                    results[rest] = {"status": "unsupported", "reason": str(e), "at": now()}
                report["remaining"] = syms[i:]
                break
            except Stopped:
                report["remaining"], report["stopped"] = syms[i:], True
                break
            except (OptionChainQuotesFetchError, OSError, ValueError, KeyError, TimeoutError, ConnectionError) as e:
                # ONE symbol's failure (HTTP, a malformed response, I/O) fails THAT symbol, never the run.
                report["failed"][sym] = f"{type(e).__name__}: {e}"
                results[sym] = {"status": "failed", "reason": f"{type(e).__name__}: {e}", "at": now()}
    finally:
        report["elapsed_s"] = round(time.monotonic() - t0, 1)
        run = {"started": started, "elapsed_s": report["elapsed_s"], "captured": report["captured"],
               "cached": report["cached"], "refused": len(report["refused"]), "failed": len(report["failed"]),
               "remaining": len(report["remaining"]), "stopped": report["stopped"]}
        try:
            _update_manifest(cache_root, account_id, session, serving_kind, results, run)
        except OSError as e:
            report["manifest_error"] = f"{type(e).__name__}: {e}"
    log(f"iv-v1 broker chain capture {session} (account {account_id}): {report['captured']} captured, "
        f"{report['cached']} already cached, {len(report['refused'])} refused (not the prior-session close), "
        f"{len(report['failed'])} failed, {len(report['remaining'])} not attempted"
        f"{' (stopped)' if report['stopped'] else ''} ({report['elapsed_s']}s)")
    return report


def make_reader(profile: str, *, account_id: int, serving_kind: str, cache_root: Optional[str] = None,
                source_profile: str = SOURCE_PROFILE_FMP_DAILY) -> BrokerLiveChainReader:
    return BrokerLiveChainReader(profile, account_id=account_id, serving_kind=serving_kind,
                                 cache_root=cache_root, source_profile=source_profile)
