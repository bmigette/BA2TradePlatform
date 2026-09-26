"""The risk-free rate option pricing uses: the as-of 3-month Treasury (FRED ``DGS3MO``).

WHY. Option backtests invert every bar's own close into iv/greeks with Black-Scholes, and
mark a barless held contract with Black-Scholes off its last iv. Both need a rate. They used
a FLAT 4.5% unless an override was set, and nothing set one, so every option backtest priced
2020-21 (3-month bills at ~0.0-0.1%) and 2023-24 (~5.2-5.4%) at the same 4.5%. The rate
moves long-dated deltas and every barless mark, so it is an input, not a constant.

WHAT THIS IS. :class:`RiskFreeRate` answers ``rate_on(day)`` for one run, from ONE source,
and says which source that was:

  * :data:`SOURCE_FRED` (``fred-dgs3mo``): the daily 3-month Treasury constant-maturity
    yield, READ FROM THE FRED DISK CACHE ONLY (``<CACHE_FOLDER>/fred/DGS3MO.json``, written
    by ``fred_series.refresh_series``). A backtest never reaches the network for it.
  * :data:`SOURCE_EXPLICIT` (``explicit``): a constant the caller chose on purpose (a run
    config value or an env override). Recorded as such in the run's results.

AS OF WHICH DAY -- SAME-DAY, deliberately. ``rate_on(d)`` is the latest observation dated ON
OR BEFORE ``d``, forward-filled over weekends and holidays. DGS3MO is never restated, but its
observation date is NOT when FRED has it: the value dated ``d`` is the par yield the U.S.
Treasury publishes on the EVENING of ``d`` (after the 4 pm close); FRED's copy arrives on
``d+1`` (the H.15 release). Same-day is still the right cut HERE because of what the rate is
used for: the option reader inverts bar ``d``'s OWN CLOSE, which is itself only known after
``d``'s close, so the information set the greek describes already includes the evening of
``d`` -- and the Black-Scholes mark on day ``d`` prices the contract at ``d``'s close too.
Using ``d-1`` would pair each close with the previous evening's rate for no gain. (A decision
made BEFORE ``d``'s close must not read this rate; nothing in the option path does -- entry
decisions read the greeks of bars dated on or before the engine clock.)

Observations after the run's end are not even loaded, so appending newer data to the cache
file cannot change a finished run's rates (or its identity).

FAIL LOUD. There is no fallback rate anywhere in here. The series REFUSES to build
(:class:`RiskFreeRateUnavailable`) when the cache file is missing or unreadable, or when it
does not cover the run window: every day of ``[start, end]`` must have an observation at
most :data:`MAX_STALENESS_DAYS` calendar days old (the longest gap in DGS3MO's whole history
is 5 days). ``rate_on`` applies the same staleness bound to every lookup, so a read outside
the checked window refuses rather than serving a months-old rate.

UNITS. FRED publishes percent; ``rate_on`` returns a decimal (5.25 -> 0.0525), used as-is as
Black-Scholes' continuously-compounded ``r`` -- the same convention the options cache
builder has always used for this series.
"""
from __future__ import annotations

import hashlib
import json
import os
from bisect import bisect_right
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

#: The FRED series id.
SERIES_ID = "DGS3MO"
#: Source labels, recorded in backtest results.
SOURCE_FRED = "fred-dgs3mo"
SOURCE_EXPLICIT = "explicit"
#: How old the latest observation may be, in calendar days, for a day's rate to be served.
#: DGS3MO's longest gap since 1981 is 5 days (a holiday next to a weekend); 7 leaves room
#: for that and nothing more.
MAX_STALENESS_DAYS = 7


class RiskFreeRateUnavailable(RuntimeError):
    """The risk-free rate cannot be stated for the requested day(s). Never swallowed."""


def _as_date(value: Any, what: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"{what} must be a date/datetime/ISO string, got {value!r}")


class RiskFreeRate:
    """One run's risk-free rate: ``rate_on(day) -> decimal``, plus where it came from.

    Build it with :func:`fred_dgs3mo_rate` or :func:`explicit_rate`. Immutable after
    construction; ``rate_on`` memoises per day (a run asks for the same few hundred days
    millions of times).
    """

    __slots__ = ("source", "_constant", "_ords", "_values", "_memo", "identity", "_describe")

    def __init__(self, *, source: str, constant: Optional[float] = None,
                 ords: Optional[List[int]] = None, values: Optional[List[float]] = None,
                 describe: Optional[Dict[str, Any]] = None) -> None:
        self.source = source
        self._constant = None if constant is None else float(constant)
        self._ords = ords
        self._values = values
        self._memo: Dict[int, float] = {}
        self._describe = dict(describe or {})
        if self._constant is not None:
            #: Hashable and stable across processes: keys worker-lifetime caches of values
            #: derived from this rate. Equal identities <=> identical rates on every day.
            self.identity = f"{SOURCE_EXPLICIT}:{self._constant!r}"
        else:
            h = hashlib.sha1()
            for o, v in zip(ords, values):
                h.update(f"{o}:{v!r};".encode())
            self.identity = f"{source}:{h.hexdigest()[:16]}"

    @property
    def is_explicit(self) -> bool:
        return self._constant is not None

    def rate_on(self, day: Any) -> float:
        """The decimal rate in force on ``day`` (as of that day, never later)."""
        if self._constant is not None:
            return self._constant
        o = day if isinstance(day, int) else _as_date(day, "day").toordinal()
        v = self._memo.get(o)
        if v is None:
            j = bisect_right(self._ords, o) - 1
            if j < 0 or o - self._ords[j] > MAX_STALENESS_DAYS:
                last = date.fromordinal(self._ords[j]).isoformat() if j >= 0 else "none"
                raise RiskFreeRateUnavailable(
                    f"No {SERIES_ID} observation within {MAX_STALENESS_DAYS} days before "
                    f"{date.fromordinal(o).isoformat()} (latest on or before it: {last}). "
                    f"Refresh the FRED cache (tools/refresh_fred_cache.py --series {SERIES_ID}).")
            v = self._values[j]
            self._memo[o] = v
        return v

    def describe(self) -> Dict[str, Any]:
        """What a run records about its rate (JSON-serialisable)."""
        return {"source": self.source, "identity": self.identity, **self._describe}

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"RiskFreeRate({self.identity})"


def explicit_rate(value: Any, *, origin: str) -> RiskFreeRate:
    """A constant rate the caller chose deliberately. ``origin`` says who chose it
    (e.g. ``"config:options_risk_free_rate"``, ``"env:BACKTEST_OPTIONS_RISK_FREE_RATE"``)
    and is recorded with it."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"explicit risk-free rate {value!r} ({origin}) is not a number") from None
    if rate != rate or not -0.1 <= rate <= 1.0:
        raise ValueError(f"explicit risk-free rate {rate!r} ({origin}) is not a plausible "
                         f"decimal annual rate (0.045 means 4.5%)")
    return RiskFreeRate(source=SOURCE_EXPLICIT, constant=rate,
                        describe={"rate": rate, "origin": origin})


def as_risk_free_rate(value: Any, *, origin: str) -> RiskFreeRate:
    """``value`` as a :class:`RiskFreeRate`: itself, or a number the caller passed on purpose
    (an explicit constant, labelled with ``origin``). ``None`` is refused -- there is no
    default rate."""
    if isinstance(value, RiskFreeRate):
        return value
    if value is None:
        raise ValueError(f"no risk-free rate given ({origin}); there is no default rate")
    return explicit_rate(value, origin=origin)


def cache_file() -> str:
    """Where the series is cached: ``fred_series.cache_path('DGS3MO')``."""
    from ba2_providers.macro import fred_series

    return fred_series.cache_path(SERIES_ID)


#: Parsed cache files, keyed on (path, mtime_ns, size): every GA trial builds its run's rate,
#: and re-parsing a 1 MB JSON per trial is waste. A rewritten file changes the key.
_PARSED: Dict[Tuple[str, int, int], Tuple[List[int], List[float], Optional[str]]] = {}


def _read_cached(path: str) -> Tuple[List[int], List[float], Optional[str]]:
    """``(ordinals, decimal values, fetched_at)`` from the cache file, ascending by date.
    Cache only: never touches the network."""
    try:
        st = os.stat(path)
    except OSError:
        st = None
    if st is not None:
        key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
        hit = _PARSED.get(key)
        if hit is None:
            hit = _parse_cached(path)
            _PARSED.clear()
            _PARSED[key] = hit
        return hit
    return _parse_cached(path)


def _parse_cached(path: str) -> Tuple[List[int], List[float], Optional[str]]:
    if not os.path.exists(path):
        raise RiskFreeRateUnavailable(
            f"FRED {SERIES_ID} is not in the cache ({path}). Warm it before an option "
            f"backtest: `python tools/refresh_fred_cache.py --series {SERIES_ID}` on a host with "
            f"the FRED key, then sync <CACHE_FOLDER>/fred/{SERIES_ID}.json to every backtest "
            f"host. A backtest reads this file only; it never fetches the rate.")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        rows = payload["observations"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RiskFreeRateUnavailable(f"FRED {SERIES_ID} cache {path} is unreadable: {e}") from e
    pairs: Dict[int, float] = {}
    for row in rows:
        raw = row.get("value") if isinstance(row, dict) else None
        if raw in (None, "", "."):
            continue          # FRED's "no observation" marker; the day is forward-filled
        try:
            o = date.fromisoformat(str(row["date"])[:10]).toordinal()
            v = float(raw) / 100.0
        except (KeyError, TypeError, ValueError) as e:
            raise RiskFreeRateUnavailable(
                f"FRED {SERIES_ID} cache {path} has a malformed row {row!r}: {e}") from e
        pairs[o] = v
    ords = sorted(pairs)
    return ords, [pairs[o] for o in ords], payload.get("fetched_at")


def fred_dgs3mo_rate(start: Any, end: Any, *, path: Optional[str] = None) -> RiskFreeRate:
    """The as-of DGS3MO rate for a run over ``[start, end]``, read from the FRED cache.

    Refuses (:class:`RiskFreeRateUnavailable`) unless every day of the window has an
    observation at most :data:`MAX_STALENESS_DAYS` old. Observations after ``end`` are
    dropped (see NO LOOKAHEAD in the module docstring). ``path`` overrides the cache file
    (tests, fixtures).
    """
    s = _as_date(start, "start")
    e = _as_date(end, "end")
    if e < s:
        raise ValueError(f"risk-free rate window ends ({e}) before it starts ({s})")
    file = path or cache_file()
    all_ords, all_values, fetched_at = _read_cached(file)
    cut = bisect_right(all_ords, e.toordinal())
    ords, values = all_ords[:cut], all_values[:cut]

    # COVERAGE: the day before each observation inside the window, and the window's end,
    # must all be within the staleness bound of the observation before them.
    j = bisect_right(ords, s.toordinal()) - 1
    if j < 0 or s.toordinal() - ords[j] > MAX_STALENESS_DAYS:
        first = date.fromordinal(all_ords[0]).isoformat() if all_ords else "none"
        raise RiskFreeRateUnavailable(
            f"FRED {SERIES_ID} cache {file} does not cover the start of the window {s} "
            f"(first observation {first}).")
    for a, b in zip(ords[j:], ords[j + 1:]):
        if b - a - 1 > MAX_STALENESS_DAYS:
            raise RiskFreeRateUnavailable(
                f"FRED {SERIES_ID} cache {file} has a {b - a}-day gap "
                f"{date.fromordinal(a)} -> {date.fromordinal(b)} inside the window {s}..{e}.")
    if e.toordinal() - ords[-1] > MAX_STALENESS_DAYS:
        raise RiskFreeRateUnavailable(
            f"FRED {SERIES_ID} cache {file} ends {date.fromordinal(ords[-1])}, more than "
            f"{MAX_STALENESS_DAYS} days before the window end {e}. Refresh it "
            f"(tools/refresh_fred_cache.py --series {SERIES_ID}) and re-sync.")

    in_window = values[j:]
    return RiskFreeRate(
        source=SOURCE_FRED, ords=ords, values=values,
        describe={
            "series": SERIES_ID, "window": [s.isoformat(), e.isoformat()],
            "cache_fetched_at": fetched_at,
            "min": min(in_window), "max": max(in_window),
            "mean": sum(in_window) / len(in_window),
        })
