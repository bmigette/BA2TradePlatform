"""Profit-cap correctness for OPTION legs and MULTI-LEG structures (``build_results``).

Two defects, both in ``results._compute_metrics``' stage-1 basis cap:

1. **UNITS.** ``cost = entry_price * size`` is right for equity but 1/100th of the truth for
   an option leg: ``entry_price`` is premium PER SHARE and ``size`` is CONTRACTS, so the
   contract multiplier is missing — even though the P&L the cap is compared against DOES
   apply it (``backtest_account.get_round_trip_trades``: ``gross = (exit-entry)*size*dir*mult``).
   The default-on ``--profit-cap-pct 2000`` therefore capped an option trade's gain at 20% of
   deployed capital instead of 20x it.

2. **UNIT OF ACCOUNT.** Round-trips are recorded PER LEG (group key
   ``(transaction_id, contract_symbol)``), so a spread's winning leg was capped while its
   losing leg was not. An iron condor at MAXIMUM PROFIT therefore scored a NEGATIVE adjusted
   return. A multi-leg structure is one economic bet: the cap belongs on its NET P&L against
   its NET cost, keyed by ``transaction_id``.

Equity is deliberately untouched (its multiplier is 1 and it has no structure grouping):
``test_equity_cap_behaviour_is_pinned_unchanged`` pins today's exact numbers and must pass
BOTH before and after the fix.

All dates are fixed 2023 constants (no wall clock is read by this code path).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_profit_cap_option_structures.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.services.backtest.results import build_results

# Frozen run window (never "today").
D_START = datetime(2023, 1, 3)
D_MID = datetime(2023, 6, 30)
D_END = datetime(2023, 12, 29)

# The universal equity-option contract multiplier (premium is quoted per share).
MULT = 100.0


class _AccountStub:
    """Minimal stand-in: ``build_results`` only calls these two methods."""

    def __init__(self, snapshots, trades):
        self._snaps = snapshots
        self._trades = trades

    def get_balance_history(self):
        return self._snaps

    def get_filled_trades(self):
        return self._trades


def _snap(d, nlv):
    return {"date": d, "net_liquidating_value": nlv, "cash_balance": 0.0, "equity_value": nlv}


def _curve(initial, final):
    return [_snap(D_START, initial), _snap(D_MID, initial), _snap(D_END, final)]


def _opt(txn, contract, direction, entry, exit_px, size, pnl, pnl_pct, mult=MULT):
    """One OPTION LEG round-trip row, shaped exactly like
    ``BacktestAccount.get_round_trip_trades`` emits it."""
    return {
        "symbol": "AAPL",
        "entry_time": D_START,
        "exit_time": D_MID,
        "direction": direction,
        "entry_price": entry,
        "exit_price": exit_px,
        "size": size,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "bars_held": 20,
        "exit_reason": "exit",
        "contract_symbol": contract,
        "underlying_symbol": "AAPL",
        "transaction_id": txn,
        "multiplier": mult,
    }


def _eq(txn, symbol, direction, entry, exit_px, size, pnl, pnl_pct):
    """One EQUITY round-trip row (no contract_symbol, multiplier 1)."""
    return {
        "symbol": symbol,
        "entry_time": D_START,
        "exit_time": D_MID,
        "direction": direction,
        "entry_price": entry,
        "exit_price": exit_px,
        "size": size,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "bars_held": 20,
        "exit_reason": "exit",
        "contract_symbol": None,
        "underlying_symbol": None,
        "transaction_id": txn,
        "multiplier": 1,
    }


def _run(initial, final, trades, **cfg):
    return build_results(_AccountStub(_curve(initial, final), trades),
                         {"initial_capital": initial, **cfg})


# --------------------------------------------------------------------------- #
# 1. UNITS: a long call that triples is not truncated to 0.2x its premium
# --------------------------------------------------------------------------- #
def test_long_call_that_triples_is_not_truncated_to_a_fifth_of_its_premium():
    """Buy 10 calls @ $2.00, sell @ $6.00.

    Capital deployed = 2.00 x 10 contracts x 100 = **$2,000** (NOT $20). The default
    ``profit_cap_pct=2000`` allows a gain up to 20 x $2,000 = $40,000, so the real +$4,000
    gain passes the cap untouched and the adjusted return equals the raw +4.00%.

    Pre-fix the missing x100 made ``cost`` $20, the cap $400 — i.e. **0.2x the premium** —
    so $3,600 of a real $4,000 gain was deducted and the adjusted return was +0.40%.
    """
    r = _run(100_000.0, 104_000.0,
             [_opt(1, "AAPL231215C00180000", "buy", 2.00, 6.00, 10, 4_000.0, 4.0)],
             profit_cap_pct=2000.0)

    assert r["total_return"] == pytest.approx(4.0)
    assert r["adjusted_total_return"] == pytest.approx(4.0)
    # Nothing was deducted from final equity at all.
    assert r["final_equity"] == pytest.approx(104_000.0)
    assert r["adjusted_annualized_return"] == pytest.approx(r["annualized_return"])
    # The pre-fix answer, spelled out so a regression is unmistakable.
    assert r["adjusted_total_return"] != pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# 2. HEADLINE: an iron condor at MAXIMUM PROFIT must not score negative
# --------------------------------------------------------------------------- #
def test_iron_condor_at_maximum_profit_scores_a_positive_adjusted_return():
    """A 5-lot iron condor opened for $2.00 net credit that expires fully worthless.

    Max profit = the credit = 2.00 x 5 x 100 = **+$1,000**, the best outcome the structure
    can possibly have. Its adjusted return must be POSITIVE.

    Pre-fix the four legs were capped INDEPENDENTLY: the two SHORT (winning) legs were
    truncated to 20x their per-share premium ($150 and $140) while the two LONG (losing)
    legs kept their full -$250 / -$200, so the "capped" condor booked **-$160** and the
    adjusted total return came out at **-0.16%** on a maximum-profit trade.
    """
    condor = [
        # short put 100 @ 1.50 -> 0.00  => +750
        _opt(42, "AAPL231215P00100000", "sell", 1.50, 0.0, 5, 750.0, 0.75),
        # long put 95 @ 0.50 -> 0.00    => -250
        _opt(42, "AAPL231215P00095000", "buy", 0.50, 0.0, 5, -250.0, -0.25),
        # short call 120 @ 1.40 -> 0.00 => +700
        _opt(42, "AAPL231215C00120000", "sell", 1.40, 0.0, 5, 700.0, 0.70),
        # long call 125 @ 0.40 -> 0.00  => -200
        _opt(42, "AAPL231215C00125000", "buy", 0.40, 0.0, 5, -200.0, -0.20),
    ]
    r = _run(100_000.0, 101_000.0, condor, profit_cap_pct=2000.0)

    assert r["total_return"] == pytest.approx(1.0)
    assert r["adjusted_total_return"] > 0, (
        "an iron condor at MAXIMUM PROFIT scored a negative adjusted return"
    )
    assert r["adjusted_total_return"] == pytest.approx(1.0)
    assert r["adjusted_annualized_return"] > 0
    # The pre-fix answer.
    assert r["adjusted_total_return"] != pytest.approx(-0.16)


# --------------------------------------------------------------------------- #
# 3. EQUITY IS FROZEN: this test must pass BEFORE and AFTER the fix
# --------------------------------------------------------------------------- #
def test_equity_cap_behaviour_is_pinned_unchanged():
    """Pure-equity book with BOTH caps on — every adjusted number pinned to today's value.

    Unlike the other tests in this file this one is expected to PASS before the fix: it is
    the guard that the option fix moves no equity number. Multiplier 1 and no structure
    grouping mean the equity path must stay literally ``entry_price * size``, per trade.

      A: 1000 sh @ $2.00 -> basis $2,000, pnl +$8,000  (cap 200% -> $4,000)
      B:   20 sh @ $50   -> basis $1,000, pnl +$1,000  (uncapped)
      C:   10 sh @ $100  -> basis $1,000, pnl +$1,000  (uncapped)
      stage 1 net = 4000 + 1000 + 1000 = 6,000; 25% share cap -> $1,500
      A -> 1,500 (excess 6,500); final 110,000 - 6,500 = 103,500 -> +3.50%
    """
    trades = [
        _eq(1, "AAA", "buy", 2.0, 10.0, 1000, 8_000.0, 80.0),
        _eq(2, "BBB", "buy", 50.0, 100.0, 20, 1_000.0, 4.0),
        _eq(3, "CCC", "buy", 100.0, 200.0, 10, 1_000.0, 3.0),
    ]
    r = _run(100_000.0, 110_000.0, trades,
             profit_cap_pct=200.0, profit_share_cap_pct=25.0)

    assert r["total_return"] == pytest.approx(10.0)
    assert r["adjusted_total_return"] == pytest.approx(3.5)
    assert r["adjusted_expectancy"] == pytest.approx(7.33)
    assert r["adjusted_avg_trade"] == pytest.approx(7.33)
    assert r["adjusted_best_trade"] == pytest.approx(15.0)
    assert r["adjusted_worst_trade"] == pytest.approx(3.0)
    assert r["adjusted_profit_factor"] == pytest.approx(999.99)
    assert r["adjusted_sqn"] == pytest.approx(7.0)
    # raw metrics untouched by either cap
    assert r["expectancy"] == pytest.approx(29.0)
    assert r["best_trade"] == pytest.approx(80.0)


def test_short_equity_basis_cap_is_pinned_unchanged():
    """A SHORT equity round-trip keeps the unsigned ``entry_price * size`` basis.

    Signed cost is an OPTION-structure concept (a net credit has no deployed-capital
    denominator). Equity — long or short — must keep using the sale/purchase notional exactly
    as it always has, so this test also passes before and after the fix.

      short 100 sh @ $4.00 -> basis $400, pnl +$1,200; cap 200% -> $800 (excess $400)
    """
    r = _run(100_000.0, 101_200.0,
             [_eq(1, "SSS", "sell", 4.0, 1.0, 100, 1_200.0, 1.2)],
             profit_cap_pct=200.0)
    assert r["adjusted_total_return"] == pytest.approx(0.8)


# --------------------------------------------------------------------------- #
# 4. The cap must STILL BITE at the corrected scale
# --------------------------------------------------------------------------- #
def test_equity_basis_ignores_the_multiplier_column_entirely():
    """The multiplier is an OPTION concept. An equity row is capped on ``entry_price * size``
    even if something upstream stamps a non-1 multiplier on it — the equity branch must not
    consult the column at all, or a covered call's SHARES would be re-based by 100x.

      100 sh @ $1.00 -> basis $100, pnl +$5,000; cap 2000% -> $2,000 (excess $3,000)
      -> +2.00%.  Honouring a bogus multiplier of 100 would allow $200,000 and cap nothing.
    """
    row = _eq(1, "AAA", "buy", 1.00, 51.00, 100, 5_000.0, 5.0)
    row["multiplier"] = 100  # bogus for equity
    r = _run(100_000.0, 105_000.0, [row], profit_cap_pct=2000.0)
    assert r["adjusted_total_return"] == pytest.approx(2.0)


def test_a_forty_x_long_call_is_still_capped():
    """The reason the cap exists (``tools/run_options_matrix.py --profit-cap-pct`` help:
    "one 40x long call must not own the GA").

      X: 1 call @ $0.10 -> $10 deployed, exits @ $4.10 => +$400 = 40x. Cap 2000% -> $200.
      Y: 5 calls @ $2.00 -> $1,000 deployed, +$300. Cap $20,000 -> untouched.
      excess $200; final 20,700 - 200 = 20,500 -> +2.50% vs a raw +3.50%.
    """
    r = _run(20_000.0, 20_700.0,
             [_opt(1, "AAPL231215C00250000", "buy", 0.10, 4.10, 1, 400.0, 2.0),
              _opt(2, "AAPL231215C00180000", "buy", 2.00, 2.60, 5, 300.0, 1.5)],
             profit_cap_pct=2000.0)

    assert r["total_return"] == pytest.approx(3.5)
    assert r["adjusted_total_return"] == pytest.approx(2.5)
    assert r["adjusted_total_return"] < r["total_return"], "the cap stopped doing its job"


# --------------------------------------------------------------------------- #
# 5. Mixed book: structures and equity capped correctly AND independently
# --------------------------------------------------------------------------- #
def test_mixed_book_caps_structures_and_equity_independently():
    """One equity mega-winner + one option debit spread + one equity loser.

      E (equity): 200 sh @ $5 -> basis $1,000, pnl +$25,000; cap 2000% -> $20,000
                  => excess $5,000  (unchanged by this fix)
      S (spread, txn 7): long 10 @ $1.00 (+$1,000) / short 10 @ $0.20 (-$200)
                  => NET debit $800, net pnl +$1,900; cap 20 x 800 = $16,000 -> untouched
      F (equity): 100 sh @ $50, pnl -$500 -> losers are never capped

      total excess = $5,000 (all of it from equity); 126,400 - 5,000 = 121,400 -> +21.40%

    Pre-fix the spread's LONG leg alone was capped to 20 x (1.00 x 10) = $200, adding a
    bogus $1,800 of excess and dragging the book to +19.60%.
    """
    trades = [
        _eq(1, "EEE", "buy", 5.0, 130.0, 200, 25_000.0, 25.0),
        _opt(7, "AAPL231215C00180000", "buy", 1.00, 3.00, 10, 2_000.0, 2.0),
        _opt(7, "AAPL231215C00190000", "sell", 0.20, 0.30, 10, -100.0, -0.1),
        _eq(2, "FFF", "buy", 50.0, 45.0, 100, -500.0, -0.5),
    ]
    r = _run(100_000.0, 126_400.0, trades, profit_cap_pct=2000.0)

    assert r["total_return"] == pytest.approx(26.4)
    assert r["adjusted_total_return"] == pytest.approx(21.4)
    # ALL of the deducted excess came from the equity trade, exactly as before this fix.
    assert r["final_equity"] - 121_400.0 == pytest.approx(5_000.0)


# --------------------------------------------------------------------------- #
# 6. Stage 2 (portfolio-share cap) is ALSO per structure
# --------------------------------------------------------------------------- #
def test_share_cap_applies_per_structure_not_per_leg():
    """The 25% share cap bounds one *bet*, so it must see the structure's NET P&L.

      S (txn 1, 4 legs): +5000 +5000 -1000 -1000 = **+8,000**
      B, C (equity): +1,000 each. Net profit 10,000 -> share cap = $2,500.
      S -> 2,500 (excess 5,500); 110,000 - 5,500 = 104,500 -> +4.50%

    Pre-fix each WINNING leg was independently clipped to $2,500 (so the 4-leg structure was
    allowed 2 x 25% of the book's profit) and the excess came out at only $5,000 -> +5.00%.
    """
    trades = [
        _opt(1, "AAPL231215C00180000", "buy", 2.00, 7.00, 10, 5_000.0, 5.0),
        _opt(1, "AAPL231215C00185000", "buy", 2.00, 7.00, 10, 5_000.0, 5.0),
        _opt(1, "AAPL231215C00190000", "sell", 1.00, 2.00, 10, -1_000.0, -1.0),
        _opt(1, "AAPL231215C00195000", "sell", 1.00, 2.00, 10, -1_000.0, -1.0),
        _eq(2, "BBB", "buy", 50.0, 100.0, 20, 1_000.0, 1.0),
        _eq(3, "CCC", "buy", 100.0, 200.0, 10, 1_000.0, 1.0),
    ]
    r = _run(100_000.0, 110_000.0, trades, profit_share_cap_pct=25.0)

    assert r["total_return"] == pytest.approx(10.0)
    assert r["adjusted_total_return"] == pytest.approx(4.5)
    # The structure's capped P&L is spread back over its legs PRO-RATA (r = 2500/8000), so a
    # losing leg stays a losing leg. Percentages: 1.5625, 1.5625, -0.3125, -0.3125, 1.0, 1.0.
    assert r["adjusted_expectancy"] == pytest.approx(0.75)
    assert r["adjusted_best_trade"] == pytest.approx(1.56)
    assert r["adjusted_worst_trade"] == pytest.approx(-0.31)
    # ...and the dollar series the same way: 1562.5, 1562.5, -312.5, -312.5, 1000, 1000.
    # Splitting the capped $2,500 EVENLY over the four legs instead would erase both losing
    # legs, so profit_factor would jump to the all-winners cap and sqn would nearly double.
    assert r["adjusted_profit_factor"] == pytest.approx(8.2)   # 5125 / 625
    assert r["adjusted_sqn"] == pytest.approx(2.13)


def test_structure_excess_is_counted_exactly_once():
    """``excess`` is a per-STRUCTURE quantity: adding it once per LEG would quadruple it.

    Same book as above; the only assertion that matters is the arithmetic identity
    ``adjusted_final == final - excess`` with ``excess`` == 5,500 (one structure), not
    22,000 (5,500 counted for each of the 4 legs) and not 0 (lost entirely).
    """
    trades = [
        _opt(1, "AAPL231215C00180000", "buy", 2.00, 7.00, 10, 5_000.0, 5.0),
        _opt(1, "AAPL231215C00185000", "buy", 2.00, 7.00, 10, 5_000.0, 5.0),
        _opt(1, "AAPL231215C00190000", "sell", 1.00, 2.00, 10, -1_000.0, -1.0),
        _opt(1, "AAPL231215C00195000", "sell", 1.00, 2.00, 10, -1_000.0, -1.0),
        _eq(2, "BBB", "buy", 50.0, 100.0, 20, 1_000.0, 1.0),
        _eq(3, "CCC", "buy", 100.0, 200.0, 10, 1_000.0, 1.0),
    ]
    r = _run(100_000.0, 110_000.0, trades, profit_share_cap_pct=25.0)
    adj_final = 100_000.0 * (1.0 + r["adjusted_total_return"] / 100.0)
    assert r["final_equity"] - adj_final == pytest.approx(5_500.0, abs=1.0)


# --------------------------------------------------------------------------- #
# 7. A NET-CREDIT structure has no deployed-capital denominator
# --------------------------------------------------------------------------- #
def test_net_credit_structure_is_not_capped_against_the_absolute_credit():
    """Same iron condor, but with a deliberately tight 50% basis cap.

    The structure's signed cost is a **-$1,000 net CREDIT**: no capital was deployed, so
    there is no basis to express the gain as a multiple of, and the basis cap does not
    apply (a credit structure's gain is bounded by the credit itself; stage 2's share cap
    is what bounds its weight in the book). Taking ``abs()`` of the credit instead would
    invent a $1,000 "basis" and clip a maximum-profit condor to $500.
    """
    condor = [
        _opt(42, "AAPL231215P00100000", "sell", 1.50, 0.0, 5, 750.0, 0.75),
        _opt(42, "AAPL231215P00095000", "buy", 0.50, 0.0, 5, -250.0, -0.25),
        _opt(42, "AAPL231215C00120000", "sell", 1.40, 0.0, 5, 700.0, 0.70),
        _opt(42, "AAPL231215C00125000", "buy", 0.40, 0.0, 5, -200.0, -0.20),
    ]
    r = _run(100_000.0, 101_000.0, condor, profit_cap_pct=50.0)

    assert r["adjusted_total_return"] == pytest.approx(1.0)
    # abs(net credit) would give 0.5; summing |leg cost| ($1,900) would give 0.95.
    assert r["adjusted_total_return"] != pytest.approx(0.5)
    assert r["adjusted_total_return"] != pytest.approx(0.95)


# --------------------------------------------------------------------------- #
# 8. Grouping key: transaction_id, and ONLY option legs
# --------------------------------------------------------------------------- #
def test_two_structures_are_capped_independently_not_merged():
    """Two separate transactions must never be pooled into one structure.

      P (txn 1): net debit $100, net pnl +$5,000 (50x) -> capped to $2,000, excess $3,000
      Q (txn 2): net debit $10,000, net pnl -$1,000 -> a loser, never capped
      104,000 - 3,000 = 101,000 -> +1.00%

    Pooling them (e.g. grouping by underlying instead of transaction) would net to a
    $10,100 basis / +$4,000 gain, clear the cap entirely and report +4.00%.
    """
    trades = [
        _opt(1, "AAPL231215C00250000", "buy", 1.10, 53.10, 1, 5_200.0, 5.2),
        _opt(1, "AAPL231215C00260000", "sell", 0.10, 2.10, 1, -200.0, -0.2),
        _opt(2, "AAPL231215P00100000", "buy", 100.00, 90.00, 1, -1_000.0, -1.0),
    ]
    r = _run(100_000.0, 104_000.0, trades, profit_cap_pct=2000.0)

    assert r["total_return"] == pytest.approx(4.0)
    assert r["adjusted_total_return"] == pytest.approx(1.0)


def test_equity_leg_sharing_a_transaction_with_an_option_is_not_merged():
    """A covered call books the SHARES and the short CALL under one transaction. The equity
    row must still be capped on its own ``entry_price * size``, never folded into the
    option structure's signed net cost.

      shares: 100 @ $1.00 -> basis $100, pnl +$5,000 -> capped to $2,000 (excess $3,000)
      short call (txn 7): sold @ $3.00 x 1 x 100 -> a -$300 CREDIT, pnl +$250, uncapped
      105,250 - 3,000 = 102,250 -> +2.25%

    Merging them would net to a -$200 credit, cap nothing and report +5.25%.
    """
    trades = [
        _eq(7, "AAPL", "buy", 1.00, 51.00, 100, 5_000.0, 5.0),
        _opt(7, "AAPL231215C00180000", "sell", 3.00, 0.50, 1, 250.0, 0.25),
    ]
    r = _run(100_000.0, 105_250.0, trades, profit_cap_pct=2000.0)

    assert r["total_return"] == pytest.approx(5.25)
    assert r["adjusted_total_return"] == pytest.approx(2.25)


def test_no_cap_configured_leaves_adjusted_equal_to_raw_for_options():
    """With neither cap set the adjusted values must equal the raw ones exactly, options
    included (the documented invariant at the top of the cap block)."""
    condor = [
        _opt(42, "AAPL231215P00100000", "sell", 1.50, 0.0, 5, 750.0, 0.75),
        _opt(42, "AAPL231215C00120000", "sell", 1.40, 0.0, 5, 700.0, 0.70),
    ]
    r = _run(100_000.0, 101_450.0, condor)
    assert r["adjusted_total_return"] == pytest.approx(r["total_return"])
    assert r["adjusted_expectancy"] == pytest.approx(r["expectancy"])
    assert r["profit_cap_pct"] is None
    assert r["profit_share_cap_pct"] is None


# --------------------------------------------------------------------------- #
# 9. The multiplier must come off the trade row, not be assumed
# --------------------------------------------------------------------------- #
def test_option_leg_multiplier_is_read_from_the_trade_row():
    """``build_results`` must use the SAME multiplier the P&L was computed with, so a
    non-standard contract size (e.g. a 10-share mini) is capped on its real basis.

      1 mini call @ $2.00 x 10 = $20 deployed, +$400 gain. Cap 2000% -> $400 -> exactly
      at the cap, nothing deducted. Assuming 100 would allow $4,000 (too loose); assuming
      1 would allow $40 and deduct $360 (the original bug).
    """
    r = _run(100_000.0, 100_400.0,
             [_opt(1, "AAPL231215C00180000", "buy", 2.00, 42.00, 1, 400.0, 0.4, mult=10.0)],
             profit_cap_pct=2000.0)
    assert r["adjusted_total_return"] == pytest.approx(0.4)

    # Same trade at a 4x-tighter cap DOES bite: 20 x 5 = $100 allowed, $300 deducted.
    r2 = _run(100_000.0, 100_400.0,
              [_opt(1, "AAPL231215C00180000", "buy", 2.00, 42.00, 1, 400.0, 0.4, mult=10.0)],
              profit_cap_pct=500.0)
    assert r2["adjusted_total_return"] == pytest.approx(0.1)


def test_trade_rows_carry_transaction_id_and_multiplier():
    """``results._trade_row`` must pass the two new structure keys through — they are what
    the cap groups and scales by."""
    r = _run(100_000.0, 101_000.0,
             [_opt(42, "AAPL231215C00180000", "buy", 1.00, 3.00, 5, 1_000.0, 1.0)])
    t = r["trades"][0]
    assert t["transaction_id"] == 42
    assert t["multiplier"] == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# 10. THE MERGE INVARIANT: the contract multiplier hits the basis EXACTLY ONCE
# --------------------------------------------------------------------------- #
#
# Two independent lines of work fixed "the option profit-cap basis is missing its x100":
# ``_deployed_capital`` (kept) and a per-trade ``entry_price * size * multiplier`` written
# inline at the call site (dropped in the merge). Keeping BOTH would have multiplied by 100
# twice -- a basis 100x too LARGE, which does not throw, does not look wrong, and simply
# stops the cap from ever binding. Dropping both puts it 100x too SMALL and throttles every
# option genome. The number below is the whole point: it is wrong in a different, visible
# direction under each mistake.
def test_option_deployed_basis_applies_the_multiplier_exactly_once():
    """A 2-contract option at premium $2.00 has a deployed basis of **$400**.

    $400 = 2.00 premium/share x 2 contracts x 100 shares/contract.

    Not $4    (2.00 x 2 -- the multiplier never applied),
    not $40,000 (2.00 x 2 x 100 x 100 -- applied twice, once in ``_deployed_capital`` and
    again at the call site).
    """
    from app.services.backtest.results import _deployed_capital

    leg = _opt(1, "AAPL231215C00180000", "buy", 2.00, 7.00, 2, 1_000.0, 1.0)

    basis = _deployed_capital(leg)
    assert basis == pytest.approx(400.0), (
        f"deployed basis {basis} != 400.0 -- the contract multiplier is applied "
        f"{basis / 4.0:g} time(s) instead of exactly once"
    )
    # Pin the two failure modes explicitly so the assertion above cannot be "fixed" by
    # loosening it: the wrong answers are 100x out in either direction.
    assert basis != pytest.approx(4.0)       # multiplier dropped entirely
    assert basis != pytest.approx(40_000.0)  # multiplier applied twice


def test_profit_cap_binds_on_the_400_dollar_basis_end_to_end():
    """The same $400 basis, observed through ``build_results`` rather than the helper.

    2 contracts @ $2.00 -> $400 deployed; exit @ $7.00 -> gross (7-2) x 2 x 100 = +$1,000.
    At ``profit_cap_pct=100`` the allowed gain is exactly 1.0 x the basis, so the cap bites
    and keeps $400 of the $1,000: adjusted final = 100,000 + 400 -> **+0.40%**.

    The two mis-scalings are separated by an order of magnitude in the OUTPUT, not just the
    intermediate:
      * multiplier dropped   -> basis $4      -> allowed $4      -> +0.00%
      * multiplier twice     -> basis $40,000 -> $1,000 < cap    -> uncapped, +1.00%
    """
    r = _run(100_000.0, 101_000.0,
             [_opt(1, "AAPL231215C00180000", "buy", 2.00, 7.00, 2, 1_000.0, 1.0)],
             profit_cap_pct=100.0)

    assert r["total_return"] == pytest.approx(1.0)          # raw, uncapped
    assert r["adjusted_total_return"] == pytest.approx(0.4)  # capped at the $400 basis
    assert r["adjusted_total_return"] != pytest.approx(0.0)  # not the 100x-too-small basis
    assert r["adjusted_total_return"] != pytest.approx(1.0)  # not the 100x-too-large basis


def test_a_non_finite_multiplier_is_rejected_because_or_1_cannot_catch_it():
    """The multiplier column must be validated at the trade-row boundary, not merely
    defaulted at each use site.

    Every consumer guards it as ``float(row.get("multiplier") or 1.0)``. That idiom is safe
    for ``None`` and for ``0``, but **NaN is truthy in Python**, so ``NaN or 1.0`` evaluates
    to NaN -- the fallback is never reached. A NaN multiplier would then flow into
    ``_deployed_capital`` (basis -> NaN; the caller's ``cost > 0`` guard is False for NaN, so
    the structure is silently left UNCAPPED) and into ``monte_carlo.apply_spread_cost``
    (notional -> NaN; ``notional <= 0`` is likewise False, so every downstream pct is NaN).

    Both are silent: no exception, no obviously-wrong number, just a cap that stopped working.
    ``_finite`` is what makes it loud.
    """
    # Pin the language semantic the whole argument rests on.
    nan = float("nan")
    assert (nan or 1.0) is nan, "NaN is truthy; `or 1.0` cannot rescue a NaN multiplier"

    with pytest.raises(ValueError, match="trade.multiplier"):
        _run(100_000.0, 101_000.0,
             [_opt(1, "AAPL231215C00180000", "buy", 2.00, 7.00, 2, 1_000.0, 1.0, mult=nan)],
             profit_cap_pct=100.0)


def test_a_missing_multiplier_still_defaults_to_the_one_that_is_a_no_op():
    """``None`` is legitimate -- equities, per-FILL fallback rows and trade blobs persisted
    before the round-trip recorder published the column all lack it. It must become 1.0 (an
    exact no-op), NOT raise: only a value that is not a number at all is rejected."""
    leg = _opt(1, "AAPL231215C00180000", "buy", 2.00, 7.00, 2, 1_000.0, 1.0)
    leg["multiplier"] = None
    r = _run(100_000.0, 101_000.0, [leg])
    assert r["trades"][0]["multiplier"] == pytest.approx(1.0)

    # And an equity row (multiplier 1) is unchanged by the coercion.
    r_eq = _run(100_000.0, 101_000.0,
                [_eq(1, "AAPL", "buy", 100.0, 110.0, 100, 1_000.0, 1.0)])
    assert r_eq["trades"][0]["multiplier"] == pytest.approx(1.0)
