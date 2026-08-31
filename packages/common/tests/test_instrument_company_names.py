"""``get_company_names``: an instrument with no stored name must stay ABSENT.

The tempting shape is ``{sym: inst.company_name or sym}`` -- every symbol present,
never a missing key for the caller to handle. It is the wrong shape here, and the
distinction is not academic: ``Instrument.company_name`` is written only by
``InstrumentAutoAdder``, which has a provider to ask. The two DB-only creation
helpers -- ``add_symbols_to_label`` and ``JobManager.ensure_instrument_exists`` --
insert a bare row, because neither can fetch a name without turning a database
write into a network call. On the live database that is 1,331 of 2,029 rows.

Echoing the ticker back as the name would tell the ⓘ tooltip that every one of
those instruments is named after itself, and the tooltip would print the symbol
twice instead of showing there is nothing more to say.
"""
from ba2_common.core.db import get_db
from ba2_common.core.models import Instrument
from ba2_common.core.utils import get_company_names


def _instrument(name, company_name):
    with get_db() as session:
        session.add(Instrument(name=name, company_name=company_name))
        session.commit()


def test_a_named_instrument_reports_its_name():
    _instrument("MSFT", "Microsoft Corporation")

    assert get_company_names(["MSFT"]) == {"MSFT": "Microsoft Corporation"}


def test_an_unnamed_instrument_is_absent_rather_than_named_after_its_ticker():
    """The whole point: the caller must be able to tell "no name on file" from a name."""
    _instrument("NONAME", None)

    result = get_company_names(["NONAME"])

    assert result == {}
    assert "NONAME" not in result, "an unnamed instrument was given a name"


def test_a_blank_name_is_treated_as_no_name():
    """`''` and NULL are the same fact and must not produce different tooltips."""
    _instrument("BLANK", "   ")

    assert get_company_names(["BLANK"]) == {}


def test_a_symbol_with_no_instrument_row_at_all_is_absent():
    assert get_company_names(["NEVERSEEN"]) == {}


def test_only_the_requested_symbols_come_back():
    """A managed label asks for its own symbols; the whole table must not arrive."""
    _instrument("AAA", "Alpha Inc.")
    _instrument("BBB", "Beta Inc.")

    assert get_company_names(["AAA"]) == {"AAA": "Alpha Inc."}


def test_the_request_is_normalised_like_every_other_symbol_helper():
    """Stored names are normalised by ``Instrument.__setattr__``, so a caller holding
    lowercase must still match -- the same contract ``get_symbols_by_label`` honours."""
    _instrument("TSLA", "Tesla, Inc.")

    assert get_company_names([" tsla "]) == {"TSLA": "Tesla, Inc."}


def test_no_symbols_asks_the_database_nothing():
    assert get_company_names([]) == {}
    assert get_company_names(None) == {}
