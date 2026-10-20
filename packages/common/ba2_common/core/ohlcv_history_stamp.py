"""Derived stores record WHICH OHLCV history they were built from; a job refuses the other one.

A panel, metric store or market-condition manifest built from the FMP cache holds numbers the Alpaca
cache would not reproduce (and vice versa), so a job running on one history must never read a store
built from the other (plan ``docs/plans/2026-10-20-alpaca-history-source.md`` Task 8).

The stamp is the same key a job config carries (``OHLCV_HISTORY_SOURCE_KEY``), so one rule reads both:
a record WITHOUT it was built before the switch, i.e. from ``LEGACY_HISTORY_SOURCE``
(``ohlcv_history.resolve_job_history_source``). Pure; no I/O.
"""
from __future__ import annotations

from typing import Mapping

from ba2_common.core.ohlcv_history import (
    OHLCV_HISTORY_SOURCE_KEY, check_history_source, resolve_job_history_source)

__all__ = ["HistorySourceMismatch", "stamp_history_source", "stored_history_source",
           "require_matching_history_source"]


class HistorySourceMismatch(ValueError):
    """A derived store was built from another OHLCV history than the job (or build) runs on."""


def stamp_history_source(source: str) -> dict:
    """``{OHLCV_HISTORY_SOURCE_KEY: source}`` for ``source`` (validated), to merge into a manifest."""
    return {OHLCV_HISTORY_SOURCE_KEY: check_history_source(source)}


def stored_history_source(record: Mapping) -> str:
    """The history a manifest was built from: its stamp, else the legacy source (FMP)."""
    return resolve_job_history_source(record)


def require_matching_history_source(record: Mapping, job_source: str, *, what: str) -> str:
    """``job_source`` if ``record`` (a manifest) was built from it, else ``HistorySourceMismatch``."""
    check_history_source(job_source)
    stored = stored_history_source(record)
    if stored != job_source:
        stamped = "stamped" if OHLCV_HISTORY_SOURCE_KEY in record else "unstamped (legacy)"
        raise HistorySourceMismatch(
            f"{what} was built from the {stored!r} OHLCV history ({stamped}), the job runs on {job_source!r}: "
            f"a job never reads a store built from the other history (rebuild it with "
            f"--ohlcv-history-source {job_source}, or run the job on {stored!r})")
    return job_source
