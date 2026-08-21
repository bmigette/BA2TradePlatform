"""No account adapter may override the market-hours TEMPLATE methods.

A source-level tripwire, deliberately in the ROOT suite because the thing it
guards lives in ``ba2_trade_platform/modules/accounts/`` while the contract it
enforces is declared in ``ba2_common``.

THE CONTRACT (ReadOnlyAccountInterface):

    get_market_hours(self, *, now=None)  -> CONCRETE, cached. Effectively FINAL.
    is_market_open(self, *, now=None)    -> CONCRETE. Effectively FINAL.
    _get_market_hours_impl(self, now)    -> THE override point, and the only one.

Two distinct failures this exists to stop, both already observed in review of the
Alpaca and TastyTrade chunks:

  1. Overriding ``get_market_hours`` drops the boundary-expiry cache, so a status
     fetched at 15:59:30 keeps reporting "open" past the bell -- the exact reason
     a plain elapsed-seconds TTL was rejected for this seam.
  2. Reaching the offline calendar with ``super().get_market_hours()`` instead of
     ``super()._get_market_hours_impl(now)`` is INFINITE RECURSION, because
     ``get_market_hours()`` is precisely what calls ``_get_market_hours_impl()``.
     It is silent: ``get_market_hours`` catches Exception, RecursionError is one,
     so several hundred re-entries collapse into a permanent "unavailable" and the
     allocation wizard refuses to submit forever without anything crashing. See
     packages/common/tests/test_account_seams.py
     ::test_calling_super_get_market_hours_from_an_override_re_enters_the_template.

Adapters are also forbidden their own market-hours cache, TTL or session-boundary
helper: the interface owns all three, and a second cache can disagree with the
banner the user is reading.
"""
import re
from pathlib import Path

ACCOUNTS_DIR = Path(__file__).resolve().parents[1] / "ba2_trade_platform" / "modules" / "accounts"

#: Methods an adapter must never define. ``_get_market_hours_impl`` is absent on
#: purpose -- that one is exactly what an adapter SHOULD define.
FORBIDDEN_OVERRIDES = ("get_market_hours", "is_market_open", "clear_market_hours_cache")


def _adapter_sources():
    """(path, source) for every broker adapter module. Read as TEXT, never imported."""
    return [(p, p.read_text(encoding="utf-8"))
            for p in sorted(ACCOUNTS_DIR.glob("*.py"))
            if p.name != "__init__.py"]


def test_there_are_adapters_to_scan():
    """A glob that silently matches nothing would make every test below vacuous."""
    names = [p.name for p, _ in _adapter_sources()]

    assert "AlpacaAccount.py" in names
    assert "TastyTradeAccount.py" in names


def test_no_adapter_overrides_a_market_hours_template_method():
    for path, source in _adapter_sources():
        for name in FORBIDDEN_OVERRIDES:
            assert not re.search(rf"^\s+def {name}\s*\(", source, re.MULTILINE), (
                f"{path.name} defines {name}(). That method is CONCRETE and "
                f"effectively FINAL on ReadOnlyAccountInterface -- overriding it "
                f"drops the session-boundary cache. Override "
                f"_get_market_hours_impl(self, now) instead.")


def test_no_adapter_reaches_the_fallback_through_the_template_method():
    """`super().get_market_hours()` inside an override is infinite recursion."""
    for path, source in _adapter_sources():
        assert "super().get_market_hours(" not in source, (
            f"{path.name} calls super().get_market_hours(). That is the method "
            f"which calls _get_market_hours_impl(), so this recurses until the "
            f"stack dies and is then swallowed into a permanent 'unavailable'. "
            f"Use super()._get_market_hours_impl(now).")


def test_no_adapter_keeps_its_own_market_hours_cache_or_ttl():
    """One cache, on the interface. A second one can disagree with the banner."""
    for path, source in _adapter_sources():
        for forbidden in ("_market_hours_cache", "_MARKET_HOURS_CACHE_TTL"):
            assert forbidden not in source, (
                f"{path.name} declares {forbidden}. The market-hours cache and its "
                f"boundary-expiry invariant live on ReadOnlyAccountInterface; an "
                f"adapter must not add a second one.")


def test_no_adapter_hardcodes_the_nyse_session_times():
    """ba2_common.core.market_calendar is the ONLY NYSE session-time source.

    A hardcoded 16:00 is wrong on the ~9 half days a year (they close at 13:00),
    and a hardcoded holiday list rots.

    COLON forms only. ``16.00`` and ``13.00`` are overwhelmingly money in a broker
    adapter, and a tripwire that cries wolf on a dollar amount is a tripwire the
    next chunk deletes.
    """
    session_time = re.compile(r"\b(?:0?9:30|16:00|13:00)\b")
    for path, source in _adapter_sources():
        hits = session_time.findall(source)

        assert not hits, (
            f"{path.name} appears to hardcode an NYSE session time {hits}. Session "
            f"times, holidays and half days come from "
            f"ba2_common.core.market_calendar, which is the only source in this "
            f"codebase and gets the 13:00 half-day close right.")
