"""Behavioural fakes for the ATM-IV history provider tests.

``FakeWorld`` prices every call contract with Black-Scholes from a KNOWN IV curve, so the
provider's inversion must round-trip to ~1e-6 and a test can compare a stored row to the truth.
No network, no Alpaca import.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from ba2_common.core import market_calendar
from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H
from ba2_trade_platform.modules.dataproviders.options.bs_inversion import bs_price

#: Monday 2026-03-09 14:00 UTC (10:00 ET) -> last completed session is Fri 2026-03-06
NOW = datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)


def occ_symbol(root: str, expiry: date, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}C{int(round(strike * 1000)):08d}"


def fridays(first: date, last: date, mwf: bool = True) -> List[date]:
    """Weekly expiries; ``mwf`` lists Mon/Wed/Fri like the big names (3 per week)."""
    out = []
    d = first
    while d <= last:
        if d.weekday() in ((0, 2, 4) if mwf else (4,)):
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeRate:
    def __init__(self, r: float = 0.04):
        self.r = r

    def rate_on(self, day) -> float:
        return self.r


class FakeSpots:
    def __init__(self, world: "FakeWorld"):
        self.world = world
        self.calls = 0

    def spots(self, symbol, start, end):
        self.calls += 1
        return {d: self.world.spot(d) for d in self.world.sessions if start <= d <= end
                and d not in self.world.no_spot_days}


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, msg="429 Too Many Requests", retry_after=None):
        super().__init__(msg)
        if retry_after is not None:
            self.response = type("R", (), {"status_code": 429, "headers": {"Retry-After": str(retry_after)}})()


class ServerError(Exception):
    status_code = 503


class FakeWorld:
    """One underlying 'TEST': strikes 50..150 step 1, weekly Friday expiries, known IV curve."""

    def __init__(self, root: str = "TEST", first_session: date = date(2025, 1, 2),
                 last_session: date = date(2026, 3, 6), mwf: bool = False):
        self.root = root
        self.sessions = market_calendar.regular_session_dates(first_session, last_session)
        self.expiries = fridays(date(2024, 12, 1), date(2026, 12, 31), mwf)
        self.strikes = [float(k) for k in range(50, 151)]
        self.no_bar_days: set = set()
        self.no_spot_days: set = set()
        self.volume_by_day: Dict[date, int] = {}
        self.default_volume = 100
        self.spot_scale = 1.0        # as-traded spot multiplier vs "FMP" (split-basis tests)

    # truth ------------------------------------------------------------------------------
    def spot(self, d: date) -> float:
        return 100.0 + 8.0 * ((d.toordinal() % 11) / 11.0) - 4.0

    def true_iv(self, d: date, K: float, e: date) -> float:
        return 0.22 + 0.0006 * (d.toordinal() - date(2025, 1, 1).toordinal()) / 10.0 + 0.0004 * (K - 100) / 10.0

    def close(self, d: date, K: float, e: date) -> float:
        T = (e - d).days / 365.0
        return bs_price(self.spot(d) * self.spot_scale, K, T, FakeRate().r, self.true_iv(d, K, e), True)

    # listing ----------------------------------------------------------------------------
    def listing_rows(self, status: str, gte: Optional[date], lte: Optional[date], today: date):
        rows = []
        for e in self.expiries:
            if gte is not None and e < gte:
                continue
            if lte is not None and e > lte:
                continue
            is_inactive = e < today
            if (status == "inactive") != is_inactive:
                continue
            for K in self.strikes:
                rows.append({"occ": occ_symbol(self.root, e, K), "expiry": e, "strike": K,
                             "size": 100, "root": self.root})
        return rows


class FakeLister:
    def __init__(self, world: FakeWorld, today: date = NOW.date()):
        self.world = world
        self.today = today
        self.calls: List[tuple] = []

    def list_page(self, underlying, status, gte, lte, page_token=None):
        import time as _t
        self.calls.append((status, gte, lte))
        self.times = getattr(self, "times", [])
        self.times.append(_t.monotonic())
        return self.world.listing_rows(status, gte, lte, self.today), None


class FakeBars:
    """Serves bars priced from the world. ``fail`` = exception factory per call number."""

    def __init__(self, world: FakeWorld):
        self.world = world
        self.calls: List[tuple] = []
        self.fail_first = 0
        self.fail_exc = RateLimitError
        self.fail_always = False
        self.empty_first = 0                  # first N bars calls answer {} (transient empty)
        self.delay = 0.0
        self.times: List[float] = []
        self.gate: Optional[threading.Event] = None     # block inside fetch until set
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def fetch(self, symbols: Sequence[str], start: date, end: date):
        import time as _t
        with self._lock:
            self.calls.append((len(symbols), start, end))
            self.times.append(_t.monotonic())
            n = len(self.calls)
        self.entered.set()
        if self.delay:
            _t.sleep(self.delay)
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.fail_always or n <= self.fail_first:
            raise self.fail_exc()
        if n <= self.empty_first:
            return {}
        out: Dict[str, List[H.BarRec]] = {}
        w = self.world
        for occ in symbols:
            e = date(2000 + int(occ[len(w.root):len(w.root) + 2]), int(occ[len(w.root) + 2:len(w.root) + 4]),
                     int(occ[len(w.root) + 4:len(w.root) + 6]))
            K = int(occ[-8:]) / 1000.0
            recs = []
            for d in w.sessions:
                if start <= d <= end and d not in w.no_bar_days and (e - d).days >= 0:
                    recs.append(H.BarRec(d, w.close(d, K, e), w.volume_by_day.get(d, w.default_volume)))
            if recs:
                out[occ] = recs
        return out


def make_provider(tmp_path, world: Optional[FakeWorld] = None, *, now=NOW,
                  rate_source=None, spot_source=None, bucket=None, sleep=None):
    world = world or FakeWorld()
    bars = FakeBars(world)
    lister = FakeLister(world, today=now.date())
    vclock = [0.0]                    # virtual time: sleeping advances it, so bucket debts drain instantly
    user_sleep = sleep

    def vsleep(sec):
        vclock[0] += sec
        if user_sleep:
            user_sleep(sec)
    prov = H.AtmIvHistoryProvider(
        cache_dir=str(tmp_path / "AtmIvHistory"), bars_client=bars, contract_lister=lister,
        spot_source=spot_source or FakeSpots(world),
        rate_source=rate_source or (lambda a, b: FakeRate()),
        now=lambda: now, sleep=vsleep,
        bucket=bucket or H.TokenBucket(per_minute=6e6, burst=1e6, clock=lambda: vclock[0], sleep=vsleep),
        jitter=lambda: 0.0)
    return prov, world, bars, lister


def reset_module_state():
    with H._FILL_LOCK:
        H._SYMBOL_LOCKS.clear()
        H._BREAKER.reset()
        H._ABANDONED.clear()
        H._WARNED.clear()
        H._SUSPECT_SEEN.clear()


class MultiBars:
    """Routes fetch/list calls to per-root fakes (several symbols, one shared bucket)."""

    def __init__(self, bars_by_root, listers_by_root):
        self.b = bars_by_root
        self.l = listers_by_root

    def fetch(self, symbols, start, end):
        return self.b[symbols[0][:-15]].fetch(symbols, start, end)

    def list_page(self, underlying, status, gte, lte, page_token=None):
        return self.l[underlying].list_page(underlying, status, gte, lte, page_token)


class MultiSpots:
    def __init__(self, spots_by_root):
        self.s = spots_by_root

    def spots(self, symbol, start, end):
        return self.s[symbol].spots(symbol, start, end)
