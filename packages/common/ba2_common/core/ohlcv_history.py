"""Which OHLCV HISTORY a backtest (or, later, a live account) reads: ``fmp`` or ``alpaca``.

One mapping, used by every site that used to hard-code the FMP history provider (plan
``docs/plans/2026-10-20-alpaca-history-source.md`` Task 6; the data itself:
``docs/REPRODUCE-BACKTESTS.md`` §4.0).

- ``fmp``: ``FMPOHLCVProvider`` (registry key ``fmp``), the vendor's split-adjusted cache.
- ``alpaca``: ``AlpacaHistoryOHLCVProvider`` (registry key ``alpaca_hist``), built by
  ``tools/prepare_history.py --source alpaca`` from raw SIP bars and our verified split table.

A NEW job is stamped with ``DEFAULT_BT_HISTORY_SOURCE`` in its config (key
``OHLCV_HISTORY_SOURCE_KEY``, also part of its name digest). A stored job or row WITHOUT the stamp ran
before the switch and therefore on ``LEGACY_HISTORY_SOURCE``. Any other value refuses: a typo must never
silently select a price history.

Pure names only: ``ba2_common`` never imports ``ba2_providers``. Hosts build the provider with
``get_provider("ohlcv", history_registry_key(source))``.
"""
from __future__ import annotations

from typing import Mapping, Tuple

#: source -> (provider registry key, cache folder name == provider class name)
HISTORY_SOURCES: Mapping[str, Tuple[str, str]] = {
    "fmp": ("fmp", "FMPOHLCVProvider"),
    "alpaca": ("alpaca_hist", "AlpacaHistoryOHLCVProvider"),
}

#: What a new backtest / GA job is stamped with (owner 2026-10-20: backtests move to Alpaca history).
DEFAULT_BT_HISTORY_SOURCE = "alpaca"

#: What a job or stored row WITHOUT a stamp ran on (everything before the switch).
LEGACY_HISTORY_SOURCE = "fmp"

#: The job-config key carrying the stamp.
OHLCV_HISTORY_SOURCE_KEY = "ohlcv_history_source"


class UnknownHistorySource(ValueError):
    """A history-source name that is not in ``HISTORY_SOURCES``."""


def check_history_source(source: str) -> str:
    """``source`` if it is a known history source, else ``UnknownHistorySource``."""
    if source not in HISTORY_SOURCES:
        raise UnknownHistorySource(
            f"unknown OHLCV history source {source!r}; known: {sorted(HISTORY_SOURCES)}")
    return source


def resolve_job_history_source(job_config: Mapping) -> str:
    """The history source a job (or stored row) runs on: its stamp, else ``LEGACY_HISTORY_SOURCE``.

    A present but unknown or empty stamp refuses (``UnknownHistorySource``)."""
    if OHLCV_HISTORY_SOURCE_KEY not in job_config:
        return LEGACY_HISTORY_SOURCE
    return check_history_source(job_config[OHLCV_HISTORY_SOURCE_KEY])


def history_registry_key(source: str) -> str:
    """The ``get_provider("ohlcv", ...)`` key for ``source``."""
    return HISTORY_SOURCES[check_history_source(source)][0]


def history_cache_dirname(source: str) -> str:
    """The cache folder (under ``CACHE_FOLDER``) holding ``source``'s bars."""
    return HISTORY_SOURCES[check_history_source(source)][1]


def other_history_cache_dirnames(source: str) -> Tuple[str, ...]:
    """The cache folders of every source except ``source`` (what a push for a ``source`` job leaves out)."""
    check_history_source(source)
    return tuple(sorted(folder for name, (_key, folder) in HISTORY_SOURCES.items() if name != source))


#: The account setting that selects the live history source of that account's experts / transactions.
ACCOUNT_HISTORY_SOURCE_KEY = "ohlcv_history_source"

#: What an account with no (or a cleared) setting reads: the only place that spells this default
#: (owner 2026-10-20: live stays FMP until an account is switched).
ACCOUNT_DEFAULT_HISTORY_SOURCE = "fmp"


def account_history_source(settings: Mapping) -> str:
    """The history source of an account from its settings dict: absent / ``None`` -> the declared default;
    a string -> its trimmed lower-case spelling, which must be known; anything else refuses
    (``UnknownHistorySource``). An empty string refuses (the settings loader maps a stored empty to ``None``)."""
    raw = settings[ACCOUNT_HISTORY_SOURCE_KEY] if ACCOUNT_HISTORY_SOURCE_KEY in settings else None
    if raw is None:
        return ACCOUNT_DEFAULT_HISTORY_SOURCE
    if not isinstance(raw, str):
        raise UnknownHistorySource(
            f"{ACCOUNT_HISTORY_SOURCE_KEY} must be a string, got {type(raw).__name__} {raw!r}")
    return check_history_source(raw.strip().lower())
