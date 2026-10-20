"""The Alpaca-history ``WarmupSource``: split calendar from the cache's own build manifest, NEVER a fetch.

The Alpaca cache (``<CACHE>/AlpacaHistoryOHLCVProvider``) is built and split-adjusted only by
``tools/prepare_history.py`` (steps ``split-table`` + ``build``). Its ``_meta/build_manifest.json`` records,
per symbol, the split events the build APPLIED (``symbols.<SYM>.events.splits`` = ``[iso date, factor]``,
factor = new shares / old shares, the same convention as ``CalendarSplit.ratio``).

``split_calendar`` returns exactly those events, so the warmup's split-basis preflight
(``split_basis.check_split_basis``) answers the question that matters for this cache: is every split the
build claims to have applied really absorbed in the stored series (no residual overnight jump of the
split ratio, no mixed basis)? A symbol with no events returns ``[]``; a cache without a build manifest
is a configuration error (the preflight must not pass vacuously).

``fetch_daily`` / ``force_full_refetch`` always raise: a warmup must never create Alpaca history, and a
FMP fetch into this tree would be the silent vendor mix the history-source switch exists to prevent.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Dict, List, Optional

from ba2_common.core.market_condition_source import SOURCE_PROFILE_ALPACA_DAILY, provider_dir_for_source_profile
from ba2_common.core.split_basis import CalendarSplit

BUILD_MANIFEST_NAME = "build_manifest.json"


class AlpacaHistoryWarmupSource:
    calls = 0
    bytes = 0

    def __init__(self, cache_root: str):
        from ba2_providers.market_conditions.warmup import WarmupConfigError
        self.cache_root = cache_root
        self.provider_dir = os.path.join(cache_root, provider_dir_for_source_profile(SOURCE_PROFILE_ALPACA_DAILY))
        self.manifest_path = os.path.join(self.provider_dir, "_meta", BUILD_MANIFEST_NAME)
        if not os.path.isfile(self.manifest_path):
            raise WarmupConfigError(
                f"{self.manifest_path} not found: the Alpaca history cache is built only by "
                f"`python tools/prepare_history.py --source alpaca --steps split-table,build`")
        self._events: Optional[Dict[str, List[CalendarSplit]]] = None

    def _load(self) -> Dict[str, List[CalendarSplit]]:
        if self._events is None:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                symbols = json.load(f)["symbols"]
            self._events = {
                sym.upper(): [CalendarSplit(date.fromisoformat(d), float(r)) for d, r in entry["events"]["splits"]]
                for sym, entry in symbols.items()}
        return self._events

    def split_calendar(self, symbol: str) -> List[CalendarSplit]:
        events = self._load()
        sym = symbol.upper()
        return list(events[sym]) if sym in events else []

    def fetch_daily(self, symbol: str, start: date, end: date) -> None:
        raise RuntimeError(f"refusing to fetch {symbol}: the Alpaca history cache is built only by "
                           f"tools/prepare_history.py, never by a warmup")

    def force_full_refetch(self, symbol: str) -> None:
        raise RuntimeError(f"refusing to re-fetch {symbol}: the Alpaca history cache is built only by "
                           f"tools/prepare_history.py, never by a warmup")
