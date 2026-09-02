"""``instrument_info``: the Type column was blank for half the table, permanently.

Three enrichment paths existed and none wrote ``instrument_type``, so 1,021 of 2,029
live rows had NULL there. Worse, "Fetch Missing" decided what was missing from the
company name and the categories ONLY -- so a row that had both but no type reported as
complete and could never be selected. The one button that would have filled the column
did not consider it missing.

Both halves are pinned here: the mapping, and the predicate.
"""
import pytest

from ba2_common.core.instrument_info import (
    instrument_info_from_provider,
    instrument_type_from_quote_type,
    needs_instrument_info,
)
from ba2_common.core.types import InstrumentType


# ---------------------------------------------------------------------------
# quoteType -> InstrumentType
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quote_type,expected", [
    ("EQUITY", InstrumentType.STOCK),
    ("ETF", InstrumentType.ETF),
    ("CRYPTOCURRENCY", InstrumentType.CRYPTO),
])
def test_the_three_types_we_can_represent(quote_type, expected):
    assert instrument_type_from_quote_type(quote_type) is expected


def test_the_match_is_case_and_whitespace_insensitive():
    assert instrument_type_from_quote_type(" etf ") is InstrumentType.ETF


@pytest.mark.parametrize("quote_type", ["MUTUALFUND", "INDEX", "FUTURE", "CURRENCY",
                                        "", "   ", None, 7, object()])
def test_anything_we_cannot_represent_is_None_and_never_STOCK(quote_type):
    """Defaulting to STOCK is the tempting shape -- most instruments ARE stocks -- and
    it is how an ETF or an index quietly becomes a stock in a table people read to
    decide what they hold. A blank cell says "nobody knows"; "stock" says something
    false with the same confidence as the rows that are true."""
    assert instrument_type_from_quote_type(quote_type) is None


# ---------------------------------------------------------------------------
# provider payloads -> fields
# ---------------------------------------------------------------------------

def test_a_full_payload_yields_all_three_fields():
    info = instrument_info_from_provider(
        {"longName": "Applied Optoelectronics, Inc.", "quoteType": "EQUITY"},
        {"sector": "Technology"})

    assert info == {"company_name": "Applied Optoelectronics, Inc.",
                    "sector": "Technology",
                    "instrument_type": InstrumentType.STOCK}


def test_an_etf_with_no_sector_still_gets_its_name_and_type():
    """The AAOI/REIT shape: each field is independent, so a missing sector must not
    cost the row its type."""
    info = instrument_info_from_provider(
        {"longName": "ALPS Active REIT ETF", "quoteType": "ETF"}, {})

    assert info["company_name"] == "ALPS Active REIT ETF"
    assert info["instrument_type"] is InstrumentType.ETF
    assert info["sector"] is None


def test_shortName_is_accepted_when_there_is_no_longName():
    info = instrument_info_from_provider({"shortName": "Hyatt Hotels"}, {})

    assert info["company_name"] == "Hyatt Hotels"


def test_a_symbol_the_provider_does_not_know_yields_all_None():
    """yahooquery returns a STRING (an error sentence) in place of the dict for a
    symbol it lacks, so a non-Mapping must not raise or be read as data."""
    assert instrument_info_from_provider(None, None) == {
        "company_name": None, "sector": None, "instrument_type": None}
    assert instrument_info_from_provider("Quote not found", "No fundamentals") == {
        "company_name": None, "sector": None, "instrument_type": None}


def test_a_blank_name_or_sector_is_None_not_an_empty_string():
    """So the writer can tell "not supplied" from "supplied as empty" and leave a good
    value already on file alone."""
    info = instrument_info_from_provider({"longName": "   "}, {"sector": ""})

    assert info["company_name"] is None
    assert info["sector"] is None


# ---------------------------------------------------------------------------
# what "missing" means
# ---------------------------------------------------------------------------

def test_a_row_with_no_type_is_missing_even_with_a_name_and_a_sector():
    """THE defect: 35 live rows in exactly this state reported as complete, so the
    only button that could have filled their Type never selected them."""
    assert needs_instrument_info(company_name="Applied Optoelectronics, Inc.",
                                 categories=["Technology"],
                                 instrument_type=None) is True


def test_a_complete_row_is_not_missing():
    assert needs_instrument_info(company_name="Apple Inc.",
                                 categories=["Technology"],
                                 instrument_type=InstrumentType.STOCK) is False


@pytest.mark.parametrize("name,cats", [(None, ["Tech"]), ("", ["Tech"]),
                                       ("Apple Inc.", None), ("Apple Inc.", [])])
def test_a_missing_name_or_sector_still_counts_as_missing(name, cats):
    assert needs_instrument_info(company_name=name, categories=cats,
                                 instrument_type=InstrumentType.STOCK) is True
