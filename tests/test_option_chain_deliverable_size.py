"""OPT-L7 — the LIVE option chain must drop non-standard deliverables.

Every money site in the platform hardcodes 100 shares per contract:
``quantity = floor(held / 100.0)`` sizes a covered call, ``strike * 100`` reserves a CSP,
``put_assignment_cost`` prices delivery. That is correct for an ordinary listed contract
and WRONG for one adjusted by a corporate action, which can deliver 150 shares, or shares
plus cash, or a different security entirely.

``AlpacaAccount.get_option_chain`` read the contract metadata and DROPPED both fields that
say so — ``meta.size`` (shares per contract) and ``meta.root_symbol`` — and ``OptionContract``
has nowhere to put them. So an adjusted contract entered the chain looking exactly like an
ordinary one and every downstream calculation quietly applied the wrong multiplier.

The cleanest consequence is on the covered-call side: 1 contract written against 100 held
shares when the contract obliges 150 is a 50-share naked short that no gate can see.

BOTH HISTORICAL PROVIDERS ALREADY DO THIS. ``ba2_providers/options/alpaca.py`` filters on a
standard-OCC-root regex, ``tastytrade.py`` filters on ``shares-per-contract != 100`` AND on
an adjusted root. The LIVE chain was the single un-plugged hole; this ports the same rule.

REFUSAL, NOT ADJUSTMENT. Dropping the row is what the two historical providers do and it is
what the strategies expect — a %OTM/delta selection never wants an adjusted contract. Making
the whole stack multiplier-aware is a much larger change, and until it exists a contract we
cannot price at 100 must not be selectable.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core.types import OptionRight
from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

EXP = date(2026, 1, 16)
STANDARD = "AAPL260116C00150000"
ADJUSTED = "AAPL1260116C00150000"       # corporate-action adjusted root


def _snapshot():
    return SimpleNamespace(
        latest_quote=SimpleNamespace(bid_price=5.0, ask_price=5.4),
        latest_trade=SimpleNamespace(price=5.2),
        implied_volatility=0.32,
        greeks=SimpleNamespace(delta=0.55, gamma=0.02, theta=-0.04, vega=0.1),
    )


def _meta(symbol, *, size="100", root="AAPL", underlying="AAPL"):
    return SimpleNamespace(
        symbol=symbol, underlying_symbol=underlying, root_symbol=root,
        type=SimpleNamespace(value="call"), strike_price=150.0,
        expiration_date=EXP, open_interest="1200", size=size)


def _adjusted_no_root():
    """An adjusted OCC symbol with no ``root_symbol`` published — pattern check only."""
    meta = _meta(ADJUSTED)
    del meta.root_symbol
    return meta


def _account(monkeypatch, metas):
    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = 1
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    snapshots = {m.symbol: _snapshot() for m in metas}

    class FakeOptClient:
        def get_option_chain(self, req):
            return snapshots

    acct._option_data_client = FakeOptClient()
    monkeypatch.setattr(acct, "_get_option_contracts_meta",
                        lambda *a, **k: {m.symbol: m for m in metas}, raising=False)
    return acct


def _chain(acct):
    return acct.get_option_chain("AAPL", date(2026, 1, 1), date(2026, 3, 1),
                                 OptionRight.CALL)


def _capture_warnings(monkeypatch):
    module_globals = AlpacaAccount.get_option_chain.__globals__
    real = module_globals["logger"]
    messages = []

    class _Tee:
        def __getattr__(self, name):
            return getattr(real, name)

        def warning(self, msg, *a, **k):
            messages.append(str(msg))

    monkeypatch.setitem(module_globals, "logger", _Tee())
    return messages


# ---------------------------------------------------------------------------

def test_a_standard_contract_is_kept(monkeypatch):
    """The control: refusal must not become "return nothing"."""
    acct = _account(monkeypatch, [_meta(STANDARD)])
    chain = _chain(acct)
    assert [c.symbol for c in chain] == [STANDARD]


def test_a_contract_delivering_more_than_100_shares_is_dropped(monkeypatch):
    """``meta.size`` is the authoritative deliverable. 150 shares != 1 covered lot."""
    acct = _account(monkeypatch, [_meta(STANDARD, size="150")])
    assert _chain(acct) == [], (
        "a contract obliging 150 shares entered the chain and every money site will "
        "price it at 100 — a covered call on 100 shares would be 50 shares naked")


def test_a_contract_delivering_fewer_than_100_shares_is_dropped(monkeypatch):
    acct = _account(monkeypatch, [_meta(STANDARD, size="10")])
    assert _chain(acct) == []


def test_an_adjusted_occ_root_is_dropped_on_the_SYMBOL_alone(monkeypatch):
    """The shape both historical providers already filter: ``AAPL1...`` / ``1SPY...``.

    ``root_symbol`` is deliberately ABSENT here, so the OCC pattern is the only thing that
    can catch it — the realistic case, since a broker that publishes the adjusted root
    would already have been caught by the check below. The first version of this test set
    ``root_symbol='AAPL1'`` as well, so deleting the pattern check outright left it green.

    Caught even though ``size`` reads a plain 100, because an adjusted contract's
    deliverable is not always expressible as a share count at all — shares plus cash, or a
    different security.
    """
    meta = _meta(ADJUSTED)
    del meta.root_symbol
    acct = _account(monkeypatch, [meta])
    assert _chain(acct) == []


def test_a_root_that_is_not_the_underlying_is_dropped_even_on_a_STANDARD_symbol(
        monkeypatch):
    """The other rail, isolated: metadata whose root contradicts the underlying.

    ``tastytrade.py`` applies exactly this comparison. A standard-looking OCC string whose
    root belongs to another name is a metadata error, and pricing it at 100 shares of the
    requested underlying would be a guess.
    """
    acct = _account(monkeypatch, [_meta(STANDARD, root="AAPL1")])
    assert _chain(acct) == []


def test_an_unreadable_deliverable_size_is_dropped_not_assumed_to_be_100(monkeypatch):
    """UNKNOWN is not 100. Assuming the standard size is the whole defect, one field in."""
    for bad in ("", "n/a", float("nan")):
        acct = _account(monkeypatch, [_meta(STANDARD, size=bad)])
        assert _chain(acct) == [], f"size={bad!r} was treated as a standard contract"


def test_a_missing_deliverable_size_falls_back_to_the_root_check(monkeypatch):
    """Alpaca may omit ``size``. A standard root is still tradeable at 100; do not refuse
    the whole live chain because one optional field is absent."""
    acct = _account(monkeypatch, [_meta(STANDARD, size=None)])
    assert [c.symbol for c in _chain(acct)] == [STANDARD]


def test_a_missing_size_on_an_ADJUSTED_root_is_still_dropped(monkeypatch):
    meta = _meta(ADJUSTED, size=None)
    del meta.root_symbol
    acct = _account(monkeypatch, [meta])
    assert _chain(acct) == []


def test_the_drop_is_logged_and_names_the_contract(monkeypatch):
    """Silently shrinking a chain looks like thin liquidity; an operator has to be able
    to tell the difference."""
    warnings = _capture_warnings(monkeypatch)
    acct = _account(monkeypatch, [_meta(STANDARD, size="150")])
    _chain(acct)
    assert any(STANDARD in m and "150" in m for m in warnings), warnings


def test_a_mixed_chain_keeps_only_the_standard_rows(monkeypatch):
    acct = _account(monkeypatch, [
        _meta(STANDARD),
        _meta("AAPL260116C00160000", size="150"),
        _adjusted_no_root(),
    ])
    assert [c.symbol for c in _chain(acct)] == [STANDARD]
