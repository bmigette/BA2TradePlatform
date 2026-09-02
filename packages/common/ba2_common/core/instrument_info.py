"""Turning a market-data provider's answer into ``Instrument`` fields. Pure.

WHY THIS EXISTS
---------------
Three separate code paths enriched instruments -- ``settings.fetch_info``,
``settings.fetch_missing_info`` (near-identical copies of each other, via yahooquery)
and ``InstrumentAutoAdder._fetch_instrument_data`` (via yfinance) -- and NONE of them
ever wrote ``instrument_type``. On the live database that left 1,021 of 2,029 rows with
a NULL type, including rows that had been fetched successfully and carried both a
company name and a sector. The Type column was simply blank for half the table and no
button on the page could fill it.

The mapping is the part worth pinning, so it lives here: no network, no DB, no UI.

UNKNOWN IS NEVER "STOCK"
------------------------
A quote type this does not recognise returns ``None``, and the caller must leave the
column NULL. Defaulting to STOCK is the tempting shape -- most instruments are stocks --
and it is how an ETF, a mutual fund or an index quietly becomes a stock in a table
people read to decide what they are holding. A blank cell says "nobody knows"; "stock"
says something false with the same confidence as the 1,008 rows that are true.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from ba2_common.core.types import InstrumentType

#: Provider quote type -> our enum. Keyed UPPERCASE; the caller need not normalise.
#:
#: Only the three we can represent. ``MUTUALFUND``, ``INDEX``, ``FUTURE``, ``OPTION``
#: and ``CURRENCY`` are deliberately ABSENT rather than mapped to the nearest member:
#: ``InstrumentType`` has exactly STOCK / ETF / CRYPTO, and an index is not a stock.
#: They fall through to ``None`` and the row keeps an honest blank.
QUOTE_TYPE_TO_INSTRUMENT_TYPE = {
    "EQUITY": InstrumentType.STOCK,
    "ETF": InstrumentType.ETF,
    "CRYPTOCURRENCY": InstrumentType.CRYPTO,
}


def instrument_type_from_quote_type(quote_type: Any) -> Optional[InstrumentType]:
    """``InstrumentType`` for a provider's ``quoteType``, or ``None`` if unrepresentable.

    ``None`` for a missing, blank, non-string or unrecognised value -- every one of
    those is "we were not told", and they must not be distinguishable from each other
    by the row they produce.
    """
    if not isinstance(quote_type, str):
        return None
    return QUOTE_TYPE_TO_INSTRUMENT_TYPE.get(quote_type.strip().upper())


def instrument_info_from_provider(price: Optional[Mapping[str, Any]],
                                  profile: Optional[Mapping[str, Any]]) -> dict:
    """``{company_name, sector, instrument_type}`` from one symbol's provider payloads.

    Every key is present in the result and every value may be ``None``. A ``None`` means
    the provider did not say, and the caller must leave the corresponding column ALONE
    rather than writing the None over a value some other source already found -- see
    ``needs_instrument_info``.

    ``price`` is yahooquery's ``price`` module (``longName``, ``quoteType``) and
    ``profile`` its ``asset_profile`` (``sector``). Either may be missing entirely for a
    symbol the provider does not know.
    """
    price = price if isinstance(price, Mapping) else {}
    profile = profile if isinstance(profile, Mapping) else {}
    name = price.get("longName") or price.get("shortName")
    sector = profile.get("sector")
    return {
        "company_name": (name.strip() or None) if isinstance(name, str) else None,
        "sector": (sector.strip() or None) if isinstance(sector, str) else None,
        "instrument_type": instrument_type_from_quote_type(price.get("quoteType")),
    }


def needs_instrument_info(*, company_name: Any, categories: Any,
                          instrument_type: Any) -> bool:
    """Is this row missing anything the "Fetch Missing" button could fill?

    THE TYPE COUNTS. The predicate used to ask only about the company name and the
    categories, so a row that had both but no type -- 35 of them on the live database,
    and every row the fetch itself had "successfully" updated -- was reported as
    complete and could never be selected for a re-fetch. The blank Type cell was
    therefore permanent: the only button that would have filled it did not consider it
    missing.
    """
    if not company_name:
        return True
    if not categories:
        return True
    return instrument_type is None
