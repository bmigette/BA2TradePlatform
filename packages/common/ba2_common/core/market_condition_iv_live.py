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
           "CAPTURE_SCHEMA_CHAIN", "chain_bytes", "chain_from_bytes"]

CAPTURE_METHOD_CHAIN = "chain_window"
CAPTURE_SCHEMA_CHAIN = "market_condition_chain_window/v1"
MEMO_SIZE = 4000

#: option source -> ``factory(profile, *, cache_root, source_profile) -> ChainLiveReader``.
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


def _uniform(profile: str, status: str, reason: str) -> FeatureRow:
    return FeatureRow.uniform(PROFILES[profile], status, reason)


class ChainLiveReader:
    """Base live reader of a chain profile. Subclasses implement :meth:`_evidence`."""

    live_chain = True
    mapped_reader = None
    option_source = ""

    def __init__(self, profile: str, *, memo_size: int = MEMO_SIZE):
        if profile not in CHAIN_COMPUTE_BY_PROFILE:
            raise KeyError(f"{profile!r} is not a chain profile")
        self.profile = profile
        self.calc_version = PROFILES[profile].calc_version
        self._memo: "OrderedDict[Hashable, IvEvidence]" = OrderedDict()
        self._memo_size = int(memo_size)
        self._lock = threading.Lock()
        self._warned: set = set()
        #: rows computed / refused (visibility for tests and diagnostics).
        self.computed = 0
        self.refused = 0

    # -- subclass hook
    def _evidence(self, symbol: str, session: date) -> IvEvidence:
        raise NotImplementedError

    def _refusal(self, symbol: str, session: date, reason: str) -> IvEvidence:
        self.refused += 1
        key = (symbol, session, reason)
        if key not in self._warned:
            self._warned.add(key)
            from ba2_common.logger import logger

            logger.warning(f"{self.profile} live ({self.option_source}) {symbol}@{session}: {reason} -- "
                           f"every gate on its fields refuses this entry")
        return IvEvidence(row=_uniform(self.profile, STATUS_NO_CONTEXT, reason), failure=reason)

    def observe_evidence(self, symbol: str, session: date) -> IvEvidence:
        key = (str(symbol).upper(), session)
        with self._lock:
            hit = self._memo.get(key)
            if hit is not None:
                self._memo.move_to_end(key)
                return hit
        try:
            ev = self._evidence(key[0], session)
        except Exception as e:  # noqa: BLE001 -- a live read failure is an unknown, never a pass or a crash
            ev = self._refusal(key[0], session, f"{type(e).__name__}: {e}")
        if not ev.failure:
            self.computed += 1
        with self._lock:
            # A FAILURE is not memoised: the next pass may find the chain published / the key set.
            if not ev.failure:
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

    def record(self, symbol: str, session: date, row: Any = None) -> None:
        try:
            self._record(str(symbol).upper(), session, self.inner.observe_evidence(symbol, session))
        except Exception as exc:  # noqa: BLE001
            self._capture.note_failure(f"{self.profile} chain window {symbol}@{session} not recorded", exc)

    def _record(self, symbol: str, session: date, ev: IvEvidence) -> None:
        from ba2_common.core.market_condition_readers import CAPTURE_PROVIDER
        from ba2_common.core.market_condition_source import normalized_window_bytes
        from ba2_common.core.replay.observe import sanitize_identity
        from ba2_common.core.replay.schemas import ReplayStatus

        chain_raw = chain_bytes(ev.chain) if ev.chain is not None else None
        win_raw = normalized_window_bytes(*ev.window) if ev.window is not None else None
        chain_dg = _digest(b"mc-chain/v1\x00", chain_raw) if chain_raw is not None else None
        win_dg = _digest(b"mc-window/v1\x00", win_raw) if win_raw is not None else None
        fields = ev.row.by_field()
        key = (symbol, session, chain_dg, win_dg,
               tuple((f, o.status, o.reason) for f, o in fields.items()) if ev.failure else None)
        with self._lock:
            if key in self._recorded:
                return
            self._recorded.add(key)
        identity = _identity(self.inner, symbol, session, self._source_profile, self._timing_policy)
        payload = {
            "schema": CAPTURE_SCHEMA_CHAIN, **identity,
            "failure": ev.failure,
            "values": {f: o.value for f, o in fields.items()},
            "statuses": {f: o.status for f, o in fields.items()},
            "reasons": {f: o.reason for f, o in fields.items()},
            "chain_digest": chain_dg,
            "chain_b64": base64.b64encode(chain_raw).decode("ascii") if chain_raw is not None else None,
            "window_digest": win_dg,
            "window_f8_b64": base64.b64encode(win_raw).decode("ascii") if win_raw is not None else None,
            "window_status": ev.window_status, "window_reason": ev.window_reason,
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
