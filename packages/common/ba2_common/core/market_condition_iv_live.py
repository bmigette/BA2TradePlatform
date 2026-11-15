"""LIVE chain-profile readers (``iv-v1``): the shared base, the source seam, capture and replay.

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` (live reader). The backtest serves iv-v1
from a published manifest built from an option store; LIVE has no such snapshot for the session it
decides on, so it reads the PRIOR session's end-of-day chain from the same vendor and computes the
row with the SAME function the store build uses (``market_conditions.compute_iv_v1``) on the SAME
inputs (the vendor chain mapped by the store's own mapping, and the OHLCV window the ohlcv-v1 live
reader reads). Same function, same inputs -> identical values (pinned by the certification test).

* :class:`ChainLiveReader` -- the reader protocol for a chain profile. A subclass supplies
  ``_evidence(symbol, session) -> IvEvidence`` (where the chain and the bars come from); the base
  memoises per (symbol, session), never raises into a gate (every failure becomes the profile's
  fields at ``no_context`` with a reason, logged once per (symbol, session, reason)), and keeps the
  evidence of each served row for capture.
* the SEAM: ``ba2_common`` must not import a provider, so the host registers each live option
  source's reader factory (``register_live_chain_reader``); the live resolver looks it up by the
  expert's recorded option source. Nothing is registered in a backtest or a test platform process.
* :class:`CapturingChainReader` / :class:`ReplayChainReader` -- capture records, per (symbol,
  session), the values/statuses/reasons AND the evidence (the normalized chain arrays and the
  128x5 window bytes, each with its digest); replay re-verifies both digests, RECOMPUTES the row
  from the retained evidence with the shared function, refuses a recomputation that differs from
  the recorded values, and never touches the network or a cache.
"""
from __future__ import annotations

import base64
import hashlib
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Hashable, Iterable, Mapping, Optional, Tuple

import numpy as np

from ba2_common.core.market_conditions import (
    CHAIN_COMPUTE_BY_PROFILE, PROFILES, STATUS_NO_CONTEXT, STATUS_VALID, WINDOW, FeatureRow, Observation,
)

__all__ = ["IvEvidence", "ChainLiveReader", "register_live_chain_reader", "live_chain_reader_factory",
           "clear_live_chain_readers", "CapturingChainReader", "ReplayChainReader", "CAPTURE_METHOD_CHAIN",
           "CAPTURE_SCHEMA_CHAIN", "chain_bytes", "chain_from_bytes", "LIVE_CHAIN_SOURCE_SETTING",
           "SOURCE_ALPHAVANTAGE", "SOURCE_TASTYTRADE", "CAPTURE_SOURCES", "LiveChainSource", "parse_live_chain_source",
           "configured_live_chain_source_value", "bounded_iv_universe", "UNIVERSE_SETTING"]

CAPTURE_METHOD_CHAIN = "chain_window"
CAPTURE_SCHEMA_CHAIN = "market_condition_chain_window/v1"
MEMO_SIZE = 4000

#: APP SETTING choosing WHERE live reads the prior session's chain: blank (unset: every active iv gate
#: refuses, naming it), ``alphavantage`` (HISTORICAL_OPTIONS, the store's vendor) or ``tastytrade``
#: (TastyTrade market data with the app-level market-data credentials -- never an account definition,
#: never an order or account endpoint).
LIVE_CHAIN_SOURCE_SETTING = "iv_live_chain_source"
SOURCE_ALPHAVANTAGE = "alphavantage"
SOURCE_TASTYTRADE = "tastytrade"
#: Sources whose chain can only be CAPTURED between the prior close and the next open (a broker snapshot):
#: they need a bounded expert universe and the capture job.
CAPTURE_SOURCES = frozenset({SOURCE_TASTYTRADE})
#: The expert setting that bounds a screener expert's universe (``MarketExpertInterface``).
UNIVERSE_SETTING = "screener_symbol_universe"


@dataclass(frozen=True)
class LiveChainSource:
    raw: str
    source: str                       # SOURCE_ALPHAVANTAGE | SOURCE_TASTYTRADE

    @property
    def captured(self) -> bool:
        return self.source in CAPTURE_SOURCES


def parse_live_chain_source(value: Any) -> Optional[LiveChainSource]:
    """The app setting's value -> :class:`LiveChainSource`; None when blank. ValueError when malformed."""
    raw = str(value or "").strip()
    if not raw:
        return None
    for source in (SOURCE_ALPHAVANTAGE, SOURCE_TASTYTRADE):
        if raw.lower() == source:
            return LiveChainSource(raw, source)
    raise ValueError(f"{raw!r}: expected blank, '{SOURCE_ALPHAVANTAGE}' or '{SOURCE_TASTYTRADE}'")


def configured_live_chain_source_value() -> Optional[str]:
    """The stored app setting (None when absent, blank or unreadable -- an unreadable DB reads as unset,
    so the gates refuse naming the setting)."""
    try:
        from ba2_common.config import get_app_setting

        value = get_app_setting(LIVE_CHAIN_SOURCE_SETTING)
    except Exception as e:  # noqa: BLE001 -- a settings read must never take the decision pass down
        from ba2_common.core.failure_modes import absorb_if_benign

        absorb_if_benign(e, OSError, ValueError, LookupError, RuntimeError)
        return None
    value = str(value or "").strip()
    return value or None


def _universe_tokens(raw: Any) -> list:
    text = str(raw or "")
    for sep in (";", "\n", "\t", " "):
        text = text.replace(sep, ",")
    return sorted({t.strip().upper() for t in text.split(",") if t.strip()})


def bounded_iv_universe(expert: Any) -> Tuple[Optional[list], str]:
    """``(symbols, reason)``: the symbols an expert's iv gates can ever be asked about, when BOUNDED --
    its fixed instruments, or (a screener expert) its ``screener_symbol_universe`` -- else
    ``(None, reason)`` naming what to set. A captured source (``CAPTURE_SOURCES``) can only serve chains
    captured between the close and the next open, so it needs this bound to know what to capture."""
    from ba2_common.core.market_condition_live import UNIVERSE_SENTINELS

    names = [str(n).strip().upper() for n in (expert.get_enabled_instruments() or ()) if str(n).strip()]
    sentinels = sorted({n for n in names if n in UNIVERSE_SENTINELS})
    fixed = sorted({n for n in names if n not in UNIVERSE_SENTINELS})
    if not sentinels:
        if not fixed:
            return None, "the expert has no enabled instruments"
        return fixed, ""
    if sentinels == ["SCREENER"]:
        universe = _universe_tokens(expert.settings.get(UNIVERSE_SETTING))
        if universe:
            return sorted(set(universe) | set(fixed)), ""
        return None, (f"its screener picks names at the decision from the WHOLE market ({UNIVERSE_SETTING} is "
                      f"empty); the live IV chain source can only serve chains captured after the prior close, so "
                      f"set {UNIVERSE_SETTING} to the names it may pick")
    return None, (f"its instruments are chosen at analysis time ({', '.join(sentinels)}) with no bounded "
                  f"universe; the live IV chain source can only serve chains captured after the prior close")


#: live chain source -> ``factory(profile, *, cache_root, source_profile) -> ChainLiveReader``.
_LIVE_CHAIN_READERS: Dict[str, Callable[..., Any]] = {}
_REG_LOCK = threading.Lock()


def register_live_chain_reader(option_source: str, factory: Callable[..., Any]) -> None:
    """Host seam (``ba2_trade_platform.core.seam_wiring``): the live reader of ``option_source``."""
    with _REG_LOCK:
        _LIVE_CHAIN_READERS[str(option_source)] = factory


def live_chain_reader_factory(option_source: str) -> Optional[Callable[..., Any]]:
    with _REG_LOCK:
        return _LIVE_CHAIN_READERS.get(str(option_source))


def clear_live_chain_readers() -> None:
    """Test hook."""
    with _REG_LOCK:
        _LIVE_CHAIN_READERS.clear()


@dataclass(frozen=True)
class IvEvidence:
    """What one live row was computed from. ``chain`` is an ``implied_vol.ChainQuotes`` (None when
    no chain could be obtained, with ``failure`` saying why); ``window`` the OHLCV window arrays
    ``(o, h, l, c, v)`` when it assembled, else ``window_status``/``window_reason``."""

    row: FeatureRow
    chain: Any = None
    window: Optional[Tuple[np.ndarray, ...]] = None
    window_status: str = STATUS_VALID
    window_reason: str = ""
    failure: str = ""
    session: Optional[date] = None
    #: what the serving source reported about the chain (tastytrade: summary-date counts, updated-at range,
    #: the source); recorded with the capture evidence.
    source_facts: Optional[Dict[str, Any]] = None


def chain_bytes(chain: Any) -> bytes:
    """Normalized chain arrays as bytes: int64 expiry days, u1 call flag, f8 strike/bid/ask."""
    parts = [np.ascontiguousarray(chain.expiry_days.astype("<i8")).tobytes(),
             np.ascontiguousarray(chain.is_call.astype("u1")).tobytes(),
             np.ascontiguousarray(chain.strike.astype("<f8")).tobytes(),
             np.ascontiguousarray(chain.bid.astype("<f8")).tobytes(),
             np.ascontiguousarray(chain.ask.astype("<f8")).tobytes()]
    return len(chain).to_bytes(8, "little") + b"".join(parts)


def chain_from_bytes(raw: bytes) -> Any:
    from ba2_common.core.implied_vol import ChainQuotes

    n = int.from_bytes(raw[:8], "little")
    off = 8
    e = np.frombuffer(raw, dtype="<i8", count=n, offset=off); off += 8 * n
    c = np.frombuffer(raw, dtype="u1", count=n, offset=off).astype(bool); off += n
    k = np.frombuffer(raw, dtype="<f8", count=n, offset=off); off += 8 * n
    b = np.frombuffer(raw, dtype="<f8", count=n, offset=off); off += 8 * n
    a = np.frombuffer(raw, dtype="<f8", count=n, offset=off)
    return ChainQuotes(e.copy(), c, k.copy(), b.copy(), a.copy())


#: Capture keeps only the contracts the computation can read: the 7..130-day band is already all a
#: chain holds; within it, strikes are trimmed to the narrowest band around each expiry's forward
#: whose RECOMPUTED row is identical to the live one (else the full chain is kept). Exactness is
#: checked, never assumed.
_TRIM_BANDS = (0.15, 0.30, 0.5)


def _trimmed_chain(ev: "IvEvidence") -> Any:
    from ba2_common.core import implied_vol as iv

    chain = ev.chain
    if chain is None or not len(chain):
        return chain
    compute = CHAIN_COMPUTE_BY_PROFILE["iv-v1"]
    session = _session_of(ev)
    if session is None:
        return chain
    want = dict(ev.row.by_field())
    forwards = {}
    for e in chain.expiries():
        lo, hi = chain.rows_for(e)
        calls, puts = iv._side_quotes(chain, lo, hi)
        forwards[e] = iv._forward_from(calls, puts)
    for band in _TRIM_BANDS:
        keep = np.zeros(len(chain), dtype=bool)
        for e, F in forwards.items():
            if F is None:
                continue
            lo, hi = chain.rows_for(e)
            k = chain.strike[lo:hi]
            keep[lo:hi] = (k >= F * (1 - band)) & (k <= F * (1 + band))
        trimmed = chain.select(keep)
        row = compute(session, trimmed, ev.window, ev.window_status, ev.window_reason)
        if dict(row.by_field()) == want:
            return trimmed
    return chain


def _session_of(ev: "IvEvidence") -> Optional[date]:
    return getattr(ev, "session", None)


def _uniform(profile: str, status: str, reason: str) -> FeatureRow:
    return FeatureRow.uniform(PROFILES[profile], status, reason)


class _PassState:
    """One decision pass's refusals, refusal summary and HTTP circuit breaker."""

    __slots__ = ("token", "refusals", "reasons", "http_failures", "circuit_open")

    def __init__(self, token: Any):
        self.token = token
        self.refusals: Dict[Hashable, "IvEvidence"] = {}
        self.reasons: "OrderedDict[str, list]" = OrderedDict()
        self.http_failures = 0
        self.circuit_open = False


class ChainLiveReader:
    """Base live reader of a chain profile. Subclasses implement :meth:`_evidence`.

    PER-PASS STATE is keyed by the pass token (``market_condition_live.current_decision()``, one object
    per decision pass) in a lock-guarded dict, so the entry and the open-positions pass of one expert
    -- which share this reader and may run concurrently -- never wipe each other's refusal memo or
    flush each other's summary. A pass's state is dropped when its summary is flushed (scope end)."""

    live_chain = True
    mapped_reader = None
    option_source = ""
    #: the certified-pairing kind this reader serves (``market_conditions.LIVE_SERVING_PAIRINGS``).
    serving_kind = ""
    #: consecutive HTTP failures in one pass after which the pass refuses further fetches outright.
    CIRCUIT_AFTER = 3

    def __init__(self, profile: str, *, memo_size: int = MEMO_SIZE):
        if profile not in CHAIN_COMPUTE_BY_PROFILE:
            raise KeyError(f"{profile!r} is not a chain profile")
        self.profile = profile
        self.calc_version = PROFILES[profile].calc_version
        self._memo: "OrderedDict[Hashable, IvEvidence]" = OrderedDict()
        self._memo_size = int(memo_size)
        self._lock = threading.Lock()
        self._passes: Dict[int, _PassState] = {}
        #: rows computed / refused (visibility for tests and diagnostics).
        self.computed = 0
        self.refused = 0

    # -- subclass hooks
    def _evidence(self, symbol: str, session: date) -> IvEvidence:
        raise NotImplementedError

    def _memo_extra(self, symbol: str) -> Tuple[Any, ...]:
        """Extra memo-key parts identifying the row's mutable inputs (the daily-bar file's signature,
        as the ohlcv-v1 live reader keys its memo), so a row computed from a stale file is recomputed
        once the file changes. Default: none."""
        return ()

    # -- pass state
    def _current_pass(self) -> Any:
        try:
            from ba2_common.core.market_condition_live import current_decision
        except ImportError:  # pragma: no cover -- ba2_common always ships it
            return None
        return current_decision()

    def _pass_state(self, token: Any = None, create: bool = True) -> Optional[_PassState]:
        token = self._current_pass() if token is None else token
        if token is None:
            return None
        with self._lock:
            st = self._passes.get(id(token))
            if st is not None and st.token is not token:     # an id reused by a new object
                st = None
            if st is None and create:
                st = self._passes[id(token)] = _PassState(token)
            return st

    def note_http_failure(self) -> bool:
        """Count one HTTP failure in this pass; True when the pass's circuit is (now) open."""
        st = self._pass_state()
        if st is None:
            return False
        with self._lock:
            st.http_failures += 1
            if st.http_failures >= self.CIRCUIT_AFTER:
                st.circuit_open = True
            return st.circuit_open

    def note_http_success(self) -> None:
        st = self._pass_state(create=False)
        if st is not None:
            with self._lock:
                st.http_failures = 0

    def circuit_open(self) -> bool:
        st = self._pass_state(create=False)
        return bool(st is not None and st.circuit_open)

    def _refusal(self, symbol: str, session: date, reason: str, kind: str = "error",
                 row: Optional[FeatureRow] = None) -> IvEvidence:
        """An unknown for every field of the profile (or ``row``, when the refusal still has a
        computed row), with ``reason``. Counted, and summarised per ``kind`` (a stable category) when
        the pass ends -- one WARNING per kind, never one per symbol per expert."""
        self.refused += 1
        st = self._pass_state()
        if st is not None:
            with self._lock:
                st.reasons.setdefault(kind, []).append((symbol, reason))
        return IvEvidence(row=row if row is not None else _uniform(self.profile, STATUS_NO_CONTEXT, reason),
                          failure=reason, session=session)

    def flush_pass_summary(self, token: Any = None) -> None:
        """Log one WARNING per refusal kind of the pass ``token`` (every open pass when None), then drop
        that pass's state."""
        with self._lock:
            if token is None:
                states, self._passes = list(self._passes.values()), {}
            else:
                st = self._passes.get(id(token))
                states = [st] if st is not None and st.token is token else []
                if states:
                    del self._passes[id(token)]
        from ba2_common.logger import logger

        for st in states:
            for kind, hits in st.reasons.items():
                symbols = list(dict.fromkeys(sym for sym, _r in hits))
                shown = ", ".join(symbols[:5]) + (f" and {len(symbols) - 5} more" if len(symbols) > 5 else "")
                logger.warning(f"{self.profile} live ({self.option_source}): {kind}: {len(symbols)} symbol(s) "
                               f"refused this pass ({shown}); e.g. {hits[0][1]} -- every gate on their fields "
                               f"refused the entry")

    # -- reading
    def _key(self, symbol: str, session: date) -> Hashable:
        return (str(symbol).upper(), session) + tuple(self._memo_extra(str(symbol).upper()))

    def peek_evidence(self, symbol: str, session: date) -> Optional[IvEvidence]:
        """The evidence already held for (symbol, session) -- a computed row or this pass's refusal --
        WITHOUT fetching anything. The capture recorder uses it, so recording an OHLCV leaf never
        triggers a chain fetch."""
        key = self._key(symbol, session)
        st = self._pass_state(create=False)
        with self._lock:
            hit = self._memo.get(key)
            if hit is None and st is not None:
                hit = st.refusals.get(key)
        return hit

    def observe_evidence(self, symbol: str, session: date) -> IvEvidence:
        key = self._key(symbol, session)
        st = self._pass_state()
        with self._lock:
            hit = self._memo.get(key)
            if hit is not None:
                self._memo.move_to_end(key)
                return hit
            if st is not None and key in st.refusals:
                return st.refusals[key]                 # same pass: the second leaf costs nothing
        try:
            ev = self._evidence(key[0], session)
        except Exception as e:
            # ENFORCE-MODE CONVENTION (failure_modes): the EXPECTED faults of a live read -- I/O and
            # parse errors of the cached chain or the bar file, a value the data cannot satisfy --
            # become an unknown (the gate refuses, countably). A programming error (TypeError,
            # AttributeError, ...) PROPAGATES: the live entry pass evaluates each recommendation in
            # its own try/except (TradeManager.process_expert_recommendations_after_analysis), so it
            # fails THAT symbol's evaluation loudly without touching other symbols or experts; on an
            # EXIT pass the condition turns it into an unknown (``_unknown_on_exit_pass``).
            from ba2_common.core.failure_modes import absorb_if_benign

            absorb_if_benign(e, OSError, ValueError, KeyError, EOFError)
            ev = self._refusal(key[0], session, f"{type(e).__name__}: {e}", "error")
        with self._lock:
            if ev.failure:
                # NEVER memoised beyond this pass: the next pass may find the chain published, the
                # key set, the budget free, the bar file repaired.
                if st is not None:
                    st.refusals[key] = ev
            else:
                self.computed += 1
                self._memo[key] = ev
                while len(self._memo) > self._memo_size:
                    self._memo.popitem(last=False)
        return ev

    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        return self.observe_evidence(symbol, session).row


# --------------------------------------------------------------------------- capture / replay
def _digest(prefix: bytes, raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + raw).hexdigest()


def _identity(reader: Any, symbol: str, session: date, source_profile: str, timing_policy: str) -> Dict[str, Any]:
    return {"symbol": symbol, "session": session.isoformat(), "profile": reader.profile,
            "option_source": reader.option_source, "source_profile": source_profile,
            "timing_policy": timing_policy, "calc_version": reader.calc_version}


def _jsonable(facts: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if facts is None:
        return None
    import json

    return json.loads(json.dumps(dict(facts), default=str))


class CapturingChainReader:
    """Record every row a :class:`ChainLiveReader` serves, with its evidence, once per
    (symbol, session, evidence digest). Recording is evidence, never a dependency."""

    live_chain = True
    mapped_reader = None

    def __init__(self, inner: ChainLiveReader, capture: Any, *, source_profile: str, timing_policy: str):
        self.inner = inner
        self._capture = capture
        self._source_profile, self._timing_policy = source_profile, timing_policy
        self._recorded: set = set()
        self._lock = threading.Lock()

    profile = property(lambda self: self.inner.profile)
    calc_version = property(lambda self: self.inner.calc_version)
    option_source = property(lambda self: self.inner.option_source)

    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        ev = self.inner.observe_evidence(symbol, session)
        try:
            self._record(str(symbol).upper(), session, ev)
        except Exception as exc:  # noqa: BLE001 -- recording is evidence, not a dependency
            self._capture.note_failure(f"{self.profile} chain window {symbol}@{session} not recorded", exc)
        return ev.row

    def flush_pass_summary(self, token: Any = None) -> None:
        self.inner.flush_pass_summary(token)

    def record(self, symbol: str, session: date, row: Any = None) -> None:
        # The condition's recorder runs after EVERY valid market read, an OHLCV leaf's included: only
        # evidence this reader already holds is recorded -- never a fetch on behalf of another field.
        try:
            ev = self.inner.peek_evidence(symbol, session)
            if ev is not None:
                self._record(str(symbol).upper(), session, ev)
        except Exception as exc:  # noqa: BLE001 -- recording is evidence, not a dependency
            self._capture.note_failure(f"{self.profile} chain window {symbol}@{session} not recorded", exc)

    def _record(self, symbol: str, session: date, ev: IvEvidence) -> None:
        from ba2_common.core.market_condition_readers import CAPTURE_PROVIDER
        from ba2_common.core.market_condition_source import normalized_window_bytes
        from ba2_common.core.replay.observe import sanitize_identity
        from ba2_common.core.replay.schemas import ReplayStatus

        # The dedupe key uses the digest of the chain AS SERVED (cheap); the trim + compression run
        # only for a row not recorded yet.
        served_raw = chain_bytes(ev.chain) if ev.chain is not None else None
        served_dg = _digest(b"mc-chain/v1\x00", served_raw) if served_raw is not None else None
        win_raw = normalized_window_bytes(*ev.window) if ev.window is not None else None
        win_dg = _digest(b"mc-window/v1\x00", win_raw) if win_raw is not None else None
        fields = ev.row.by_field()
        key = (symbol, session, served_dg, win_dg,
               tuple((f, o.status, o.reason) for f, o in fields.items()) if ev.failure else None)
        with self._lock:
            if key in self._recorded:
                return
            self._recorded.add(key)
        chain_raw = chain_bytes(_trimmed_chain(ev)) if ev.chain is not None else None
        chain_dg = _digest(b"mc-chain/v1\x00", chain_raw) if chain_raw is not None else None
        identity = _identity(self.inner, symbol, session, self._source_profile, self._timing_policy)
        payload = {
            "schema": CAPTURE_SCHEMA_CHAIN, **identity,
            "failure": ev.failure,
            "values": {f: o.value for f, o in fields.items()},
            "statuses": {f: o.status for f, o in fields.items()},
            "reasons": {f: o.reason for f, o in fields.items()},
            "chain_digest": chain_dg,
            # the digest of the chain AS SERVED / cached (before the capture trim): ties the replay
            # evidence to what the vendor delivered.
            "chain_served_digest": served_dg,
            # zlib + base64 (the replay codec has no bytes type); the digest is over the RAW bytes.
            "chain_encoding": "zlib",
            "chain_b64": (base64.b64encode(zlib.compress(chain_raw, 6)).decode("ascii")
                          if chain_raw is not None else None),
            "window_digest": win_dg,
            "window_f8_b64": base64.b64encode(win_raw).decode("ascii") if win_raw is not None else None,
            "window_status": ev.window_status, "window_reason": ev.window_reason,
            # which source served the chain (alphavantage | tastytrade) and what it reported about it
            "serving_kind": getattr(self.inner, "serving_kind", "") or self.inner.option_source,
            "source_facts": _jsonable(ev.source_facts),
        }
        self._capture.record_observation(
            provider=CAPTURE_PROVIDER, method=CAPTURE_METHOD_CHAIN,
            request_identity=sanitize_identity(identity), payload=payload,
            provenance=ReplayStatus.PROVENANCE_DISK_CACHE)


class ReplayChainReader:
    """Serve recorded chain-profile rows by RECOMPUTING them from the retained evidence.

    ``ReplayMiss`` for anything not on the tape, a digest that does not match its bytes, or a
    recomputation that differs from what live recorded (so a replay can never quietly diverge).
    A recorded refusal (``failure``) replays as the same refusal."""

    live_chain = True
    mapped_reader = None

    def __init__(self, recorded: Mapping[Tuple[str, str, date], Mapping[str, Any]], *, profile: str,
                 option_source: str):
        self.profile = profile
        self.option_source = option_source
        self.calc_version = PROFILES[profile].calc_version
        self._recorded = dict(recorded)
        self._rows: Dict[Tuple[str, date], FeatureRow] = {}

    @classmethod
    def from_payloads(cls, payloads: Iterable[Mapping[str, Any]], *, profile: str, option_source: str
                      ) -> "ReplayChainReader":
        recorded = {}
        for p in payloads:
            if p.get("schema") != CAPTURE_SCHEMA_CHAIN or p.get("profile") != profile:
                continue
            recorded[(p["profile"], p["symbol"], date.fromisoformat(p["session"]))] = p
        return cls(recorded, profile=profile, option_source=option_source)

    @classmethod
    def from_bundle(cls, bundle: Any, analysis_id: str, **kwargs) -> "ReplayChainReader":
        from ba2_common.core.market_condition_readers import CAPTURE_PROVIDER

        payloads = [bundle.decode(o.payload_object) for o in bundle.observations_for(analysis_id)
                    if o.provider == CAPTURE_PROVIDER and o.method == CAPTURE_METHOD_CHAIN
                    and o.payload_object is not None]
        return cls.from_payloads(payloads, **kwargs)

    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        key = (str(symbol).upper(), session)
        if key not in self._rows:
            self._rows[key] = self._build(*key)
        return self._rows[key]

    def _build(self, symbol: str, session: date) -> FeatureRow:
        from ba2_common.core.market_condition_source import window_from_bytes
        from ba2_common.core.replay.context import ReplayMiss

        p = self._recorded.get((self.profile, symbol, session))
        ident = [self.profile, symbol, session.isoformat()]
        if p is None:
            raise ReplayMiss("market_condition_chain_window", request_identity=ident,
                             detail="no recorded chain-profile row for this profile/symbol/session")
        if p.get("option_source") != self.option_source:
            raise ReplayMiss("market_condition_chain_window", request_identity=ident,
                             detail=f"recorded option source {p.get('option_source')!r} differs")
        recorded = FeatureRow(
            values={f: Observation(p["values"][f] if p["statuses"][f] == STATUS_VALID else None,
                                   p["statuses"][f], p["reasons"][f]) for f in p["statuses"]},
            calc_versions={f: p["calc_version"] for f in p["statuses"]})
        if p.get("failure"):
            return recorded
        chain = window = None
        if p.get("chain_b64") is not None:
            raw = base64.b64decode(p["chain_b64"])
            if p.get("chain_encoding") == "zlib":
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raise ReplayMiss("market_condition_chain_digest", request_identity=ident,
                                     detail="the retained chain does not decompress (corrupt evidence)") from None
            if _digest(b"mc-chain/v1\x00", raw) != p["chain_digest"]:
                raise ReplayMiss("market_condition_chain_digest", request_identity=ident,
                                 detail="the retained chain does not hash to its recorded digest")
            chain = chain_from_bytes(raw)
        if p.get("window_f8_b64") is not None:
            raw = base64.b64decode(p["window_f8_b64"])
            if _digest(b"mc-window/v1\x00", raw) != p["window_digest"]:
                raise ReplayMiss("market_condition_chain_digest", request_identity=ident,
                                 detail="the retained window does not hash to its recorded digest")
            window = window_from_bytes(raw)
            if len(window[0]) != WINDOW:
                raise ReplayMiss("market_condition_chain_digest", request_identity=ident,
                                 detail=f"retained window has {len(window[0])} bars")
        row = CHAIN_COMPUTE_BY_PROFILE[self.profile](
            session, chain, window, STATUS_VALID if window is not None else p["window_status"],
            "" if window is not None else p["window_reason"])
        if dict(row.by_field()) != dict(recorded.by_field()):
            raise ReplayMiss("market_condition_chain_recompute", request_identity=ident,
                             detail="the row recomputed from the retained evidence differs from the recorded one")
        return row
