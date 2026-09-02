"""Filling in an ``Instrument``'s company name, sector and TYPE. Live (network + DB).

THE DEFECT THIS EXISTS FOR
--------------------------
Three code paths enriched instruments and NONE of them ever wrote
``instrument_type``: ``settings.fetch_info`` and ``settings.fetch_missing_info``
(near-identical copies of one another, via yahooquery) and
``InstrumentAutoAdder._fetch_instrument_data`` (via yfinance). On the live database
that left 1,021 of 2,029 rows with a NULL type -- including 35 that carried a company
name AND a sector, i.e. rows the fetch had already "successfully" updated. The Type
column was blank for half the table and no button on the page could fill it, because
"Fetch Missing" decided what was missing by looking at the company name and the
categories only.

A fourth gap fed the same table: adding a symbol to a label from the Portfolio
Allocation page goes through ``add_symbols_to_label``, a pure DB helper that creates a
bare row -- it has no provider to ask and must not turn a database write into a network
call. So every symbol added that way arrived with no name, no sector and no type, and
stayed that way until someone pressed a button on a different page.

This module is the ONE enrichment path. The mapping itself is pure and lives in
``ba2_common.core.instrument_info``; what is here is the network and the write.

WHY TWO PROVIDERS
-----------------
yahooquery answers in ONE batched request for hundreds of symbols, which is what makes
"Fetch Info" over the whole table tolerable. It also silently returns nothing for
symbols it does not cover. FMP is per-symbol and slower, so it runs only over what
yahooquery could not name -- a fallback, not a second pass.

NOTHING IS OVERWRITTEN WITH A BLANK. A provider that does not answer leaves the column
exactly as it was. "The provider said nothing" and "the provider said empty" arrive
identically over HTTP, and treating them the same is how a good name already on file
gets replaced by "".
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ba2_common.core.instrument_info import (
    instrument_info_from_provider,
    needs_instrument_info,
)
from ba2_common.core.models import Instrument
from ba2_common.core.db import get_db
from sqlmodel import select

from ..logger import logger

#: yahooquery is asked for this many symbols at a time. Its endpoint takes hundreds,
#: but a failure is all-or-nothing per request, so a whole-table fetch that trips on
#: one bad ticker should not lose every other symbol's answer with it.
_BATCH = 200


def _yahoo_info(symbols: Sequence[str]) -> Dict[str, dict]:
    """``{SYMBOL: {company_name, sector, instrument_type}}`` from yahooquery.

    Returns whatever it managed. A batch that raises is logged and skipped rather than
    aborting the rest -- the caller's job is to enrich as much as it can, and one dead
    request must not cost the other 1,800 symbols their update.
    """
    from yahooquery import Ticker

    out: Dict[str, dict] = {}
    wanted = [s for s in symbols if s]
    for start in range(0, len(wanted), _BATCH):
        chunk = wanted[start:start + _BATCH]
        try:
            ticker = Ticker(chunk)
            prices = ticker.price or {}
            profiles = ticker.asset_profile or {}
        except Exception as e:  # noqa: BLE001 -- one bad batch must not sink the rest
            logger.warning(f"yahooquery batch of {len(chunk)} failed: {e}")
            continue
        for symbol in chunk:
            key = symbol.upper()
            price = prices.get(key)
            profile = profiles.get(key)
            # yahooquery returns a STRING (an error sentence) where it would otherwise
            # return the dict, for a symbol it does not know. `instrument_info_from_
            # provider` already ignores a non-Mapping, so this needs no special case.
            info = instrument_info_from_provider(price, profile)
            if any(info.values()):
                out[key] = info
    return out


def _fmp_names(symbols: Sequence[str]) -> Dict[str, str]:
    """``{SYMBOL: company_name}`` from FMP, for symbols yahooquery could not name.

    NAMES ONLY. FMP's profile also carries a sector and an ``isEtf`` flag, but this is
    the fallback path and the one thing it is here to recover is the name; widening it
    would mean a second mapping to keep in step with the pure one.

    Per symbol, and every failure is swallowed to a debug line: this runs over the
    symbols another provider already declined, so individual misses are the expected
    case and not worth a warning each.
    """
    if not symbols:
        return {}
    try:
        from datetime import datetime
        import ba2_providers
        provider = ba2_providers.get_provider("company_overview", "fmp")
    except Exception as e:  # noqa: BLE001 -- no FMP configured is a normal state
        logger.debug(f"FMP fallback unavailable: {e}")
        return {}

    out: Dict[str, str] = {}
    for symbol in symbols:
        try:
            data = provider.get_company_overview(
                symbol, as_of_date=datetime.now(), format_type="dict")
            name = (data or {}).get("company_name")
            if isinstance(name, str) and name.strip():
                out[symbol.upper()] = name.strip()
        except Exception as e:  # noqa: BLE001 -- expected for a symbol FMP lacks
            logger.debug(f"FMP had no profile for {symbol}: {e}")
    return out


def fetch_instrument_info(symbols: Sequence[str]) -> Dict[str, dict]:
    """``{SYMBOL: {company_name, sector, instrument_type}}``, yahooquery then FMP.

    FMP is asked ONLY about symbols yahooquery left without a name, and contributes
    only that name. A symbol neither provider knows is simply absent from the result.
    """
    wanted = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
    if not wanted:
        return {}
    info = _yahoo_info(wanted)
    unnamed = [s for s in wanted if not (info.get(s) or {}).get("company_name")]
    for symbol, name in _fmp_names(unnamed).items():
        entry = info.setdefault(symbol, {"company_name": None, "sector": None,
                                         "instrument_type": None})
        entry["company_name"] = name
    return info


def enrich_instruments(symbols: Sequence[str], *,
                       only_missing: bool = True) -> Tuple[int, int]:
    """Fetch and store info for ``symbols``. Returns ``(updated, errors)``. BLOCKING.

    ``only_missing`` skips rows that already have a name, a sector AND a type -- the
    "Fetch Missing" semantics, now including the type (see ``needs_instrument_info``,
    whose omission of it is why the blank column was permanent).

    Each field is written independently and ONLY when the provider supplied it, so a
    symbol whose sector is unknown still gets its name and its type. Rows are committed
    one at a time: a whole-table fetch that dies half way should keep the half it did.
    """
    wanted = [s.strip().upper() for s in (symbols or []) if s and s.strip()]
    if not wanted:
        return 0, 0

    with get_db() as session:
        rows = session.exec(select(Instrument)
                            .where(Instrument.name.in_(wanted))).all()
        targets = [(r.id, r.name) for r in rows
                   if not only_missing or needs_instrument_info(
                       company_name=r.company_name, categories=r.categories,
                       instrument_type=r.instrument_type)]
    if not targets:
        return 0, 0

    info = fetch_instrument_info([name for _id, name in targets])
    updated = errors = 0
    for instrument_id, name in targets:
        found = info.get(name)
        if not found:
            continue
        try:
            with get_db() as session:
                row = session.get(Instrument, instrument_id)
                if row is None:
                    continue
                changed = False
                if found.get("company_name") and not row.company_name:
                    row.company_name = found["company_name"]
                    changed = True
                if found.get("instrument_type") and row.instrument_type is None:
                    row.instrument_type = found["instrument_type"]
                    changed = True
                sector = found.get("sector")
                if sector and sector not in (row.categories or []):
                    row.categories = list(row.categories or []) + [sector]
                    changed = True
                if changed:
                    session.add(row)
                    session.commit()
                    updated += 1
        except Exception as e:  # noqa: BLE001 -- one row must not stop the batch
            logger.error(f"Storing instrument info for {name} failed: {e}",
                         exc_info=True)
            errors += 1
    logger.info(f"Instrument enrichment: {updated} updated, {errors} error(s), "
                f"{len(targets)} considered")
    return updated, errors
