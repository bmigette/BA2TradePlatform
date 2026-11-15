"""A broker's option-chain QUOTE SNAPSHOT for one underlying, and the data rule that decides whether it
is the PRIOR SESSION's end-of-day chain (iv-v1 live, broker quote source).

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` (section 6.6, broker quote source).

WHY A RULE ON THE DATA. A broker's snapshot is "the latest quote per contract". Between a session's
close and the next open that is the session's closing quote -- measured for TastyTrade on Sunday
2026-11-15: every contract of 8 underlyings carried ``summary-date`` 2026-11-13 and the identical
bid/ask Alpha Vantage publishes for that session. But a broker may FLUSH or reset its snapshot (a
nightly reset, a pre-market clear, today's session starting to quote), and a clock alone cannot know
when. A snapshot is therefore accepted for session S only on POSITIVE EVIDENCE in the data itself
(:func:`validate_prior_session_snapshot`); anything else is refused, never cached, never computed on.

Pure: no broker, no I/O. The account classes produce :class:`OptionChainQuoteSnapshot`
(``ba2_providers.options.tastytrade_quotes``, app-level market-data credentials); the capture job
validates and caches it.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["OptionChainQuote", "OptionChainQuoteSnapshot", "OptionChainSourceUnavailable",
           "OptionChainQuotesFetchError", "SnapshotVerdict", "validate_prior_session_snapshot",
           "FETCH_STRIKE_BAND", "NEAR_MONEY_BAND", "MIN_NEAR_MONEY_CONTRACTS", "MIN_ON_SESSION_SHARE",
           "fetch_expiry_window", "session_close_and_next_open"]

#: Strikes fetched: within this fraction of the underlying's close of S. The computation reads the
#: at-the-money neighbourhood of each 7..130-day expiry; measured on 8 underlyings (2026-11-13), a
#: +-15% band reproduces the full-chain row exactly (tests pin it on the fixtures). 20% is the margin.
FETCH_STRIKE_BAND = 0.20
#: "The contracts that matter" for the validity rule: 7..130-day expiries, strikes within this band.
NEAR_MONEY_BAND = 0.05
#: At least this many near-money contracts must be present ...
MIN_NEAR_MONEY_CONTRACTS = 4
#: ... and at least this share of them must carry a quote OF S (see the rule).
MIN_ON_SESSION_SHARE = 0.90


class OptionChainSourceUnavailable(RuntimeError):
    """The source cannot serve at all (credentials blank or rejected). The message says why (never the
    credentials themselves); a capture run stops on it and every symbol records the reason."""


class OptionChainQuotesFetchError(RuntimeError):
    """A snapshot request failed (HTTP, timeout, malformed response). Retried on the next attempt."""


@dataclass(frozen=True)
class OptionChainQuote:
    """One contract's latest quote as the broker reports it. ``summary_date`` is the trading session
    the broker files the quote under; ``updated_at`` (aware UTC) when the record last changed."""

    occ: str
    expiry: date
    option_type: str            # "call" | "put"
    strike: float
    bid: Optional[float]
    ask: Optional[float]
    summary_date: Optional[date]
    updated_at: Optional[datetime]
    last: Optional[float] = None
    volume: Optional[float] = None


@dataclass(frozen=True)
class OptionChainQuoteSnapshot:
    underlying: str
    source_kind: str                           # e.g. "tastytrade"
    fetched_at: datetime                       # aware UTC
    spot: Optional[float]                      # the underlying's close of its summary session
    spot_summary_date: Optional[date]
    quotes: Tuple[OptionChainQuote, ...]
    requested: int = 0                         # contracts asked for (a missing one has no data at all)
    strike_band: float = FETCH_STRIKE_BAND
    expiry_min: Optional[date] = None
    expiry_max: Optional[date] = None
    requests: int = 0


@dataclass(frozen=True)
class SnapshotVerdict:
    ok: bool
    reason: str = ""
    kind: str = ""                             # stable refusal category (summaries, tests)
    facts: Dict[str, Any] = field(default_factory=dict)
    kept: Tuple[OptionChainQuote, ...] = ()   # the quotes OF S (the rest is treated as a missing quote)


def fetch_expiry_window(session: date) -> Tuple[date, date]:
    """The expiries a snapshot for ``session`` needs: the iv-v1 DTE window (``implied_vol``)."""
    from ba2_common.core.implied_vol import DTE_MAX, DTE_MIN

    return session + timedelta(days=DTE_MIN), session + timedelta(days=DTE_MAX)


def session_close_and_next_open(session: date) -> Tuple[datetime, datetime]:
    """(close of ``session``, open of the next regular session), aware UTC, from the NYSE calendar
    (early closes included)."""
    from ba2_common.core.market_calendar import (
        next_regular_session, nyse_regular_sessions, regular_session_close_utc,
    )

    nxt = next_regular_session(session)
    opens = nyse_regular_sessions(nxt, nxt)
    if not opens:
        raise ValueError(f"no regular session found after {session}")
    return regular_session_close_utc(session), opens[0][0].astimezone(timezone.utc)


def _finite(x: Optional[float]) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt is not None else None


def validate_prior_session_snapshot(snap: OptionChainQuoteSnapshot, session: date) -> SnapshotVerdict:
    """Is ``snap`` the end-of-day chain of ``session`` (S)? THE RULE (all on the data):

    1. REFUSED outright when ANY contract is filed under a session AFTER S (``summary_date > S``) or was
       updated at/after the next session's open: today's session has started quoting -- this is no
       longer the prior-session chain.
    2. The near-money contracts (7..130-day expiries, strike within ``NEAR_MONEY_BAND`` of the
       underlying's close of S) must number at least ``MIN_NEAR_MONEY_CONTRACTS``, and at least
       ``MIN_ON_SESSION_SHARE`` of them must be quotes OF S: ``summary_date == S``, ``updated_at``
       between S's close and the next open, bid and ask present (not null/NaN). Otherwise REFUSED:
       the snapshot was flushed, nulled or reset (or is still from an earlier session).
    3. The underlying's own close must be filed under S (the band is centred on it).

    Accepted: ``kept`` holds every contract that is a quote of S; a contract that is not (an old
    quote, a null) is treated exactly like a contract with no quote. Zero bids far from the money are
    normal and pass through to the shared acceptance rule (``alphavantage.map_chain``) unchanged.
    ``facts`` (always filled) records what the decision saw: summary-date counts, updated-at range,
    null counts, near-money counts."""
    close_utc, next_open_utc = session_close_and_next_open(session)
    quotes = tuple(snap.quotes)
    sd = Counter(q.summary_date.isoformat() if q.summary_date else "none" for q in quotes)
    ups = [q.updated_at for q in quotes if q.updated_at is not None]
    null_quotes = sum(1 for q in quotes if not (_finite(q.bid) and _finite(q.ask)))
    zero_bids = sum(1 for q in quotes if _finite(q.bid) and float(q.bid) == 0.0)
    facts: Dict[str, Any] = {
        "session": session.isoformat(), "source_kind": snap.source_kind,
        "fetched_at": _iso(snap.fetched_at), "contracts": len(quotes), "requested": snap.requested,
        "summary_dates": dict(sorted(sd.items())),
        "updated_at_min": _iso(min(ups)) if ups else None, "updated_at_max": _iso(max(ups)) if ups else None,
        "null_bid_or_ask": null_quotes, "zero_bids": zero_bids,
        "spot": snap.spot, "spot_summary_date": snap.spot_summary_date.isoformat() if snap.spot_summary_date else None,
        "session_close_utc": _iso(close_utc), "next_open_utc": _iso(next_open_utc),
        "strike_band": snap.strike_band, "requests": snap.requests,
    }

    def refuse(kind: str, why: str) -> SnapshotVerdict:
        return SnapshotVerdict(False, f"snapshot not the prior-session close of {session}: {why}", kind, facts)

    if not quotes:
        return refuse("snapshot_empty", "the broker returned no option quotes")
    after = sorted({d for d in sd if d not in ("none",) and d > session.isoformat()})
    late = [u for u in ups if u >= next_open_utc]
    if after or late:
        why = (f"contracts are filed under a later session ({', '.join(after)})" if after else
               f"{len(late)} contract(s) were updated at/after the next open ({_iso(next_open_utc)})")
        return refuse("snapshot_after_session", f"today's session has started quoting -- {why}")
    if not _finite(snap.spot) or float(snap.spot) <= 0:
        return refuse("no_spot", "the underlying's close is missing")
    if snap.spot_summary_date is not None and snap.spot_summary_date != session:
        return refuse("spot_not_session", f"the underlying's close is filed under {snap.spot_summary_date}")

    lo, hi = fetch_expiry_window(session)

    def of_session(q: OptionChainQuote) -> bool:
        return (q.summary_date == session and q.updated_at is not None
                and close_utc <= q.updated_at < next_open_utc and _finite(q.bid) and _finite(q.ask))

    spot = float(snap.spot)
    near = [q for q in quotes if lo <= q.expiry <= hi and abs(q.strike / spot - 1.0) <= NEAR_MONEY_BAND]
    near_ok = sum(1 for q in near if of_session(q))
    facts.update({"near_money": len(near), "near_money_of_session": near_ok})
    if len(near) < MIN_NEAR_MONEY_CONTRACTS:
        return refuse("too_few_near_money", f"only {len(near)} near-money contract(s) in the 7..130-day "
                                            f"expiries (need {MIN_NEAR_MONEY_CONTRACTS})")
    share = near_ok / len(near)
    if share < MIN_ON_SESSION_SHARE:
        return refuse("not_session_quotes",
                      f"only {near_ok} of {len(near)} near-money contracts carry a quote of {session} "
                      f"(summary-date {session}, updated between its close and the next open, bid and ask "
                      f"present; need {MIN_ON_SESSION_SHARE:.0%}) -- the snapshot looks flushed, nulled or "
                      f"stale")
    kept = tuple(q for q in quotes if of_session(q))
    facts["kept"] = len(kept)
    return SnapshotVerdict(True, "", "", facts, kept)
