"""Oversubscribed sleeves must not invent capital in the Balance Usage footer.

THE DEFECT (seen live 2026-09-06). Six experts were deployed on ONE $1,711.72 account at
60% each. Every bar was right -- each sleeve really is $1,027 -- but the footer summed the
six sleeves and printed "Total: $6,162.18", with "Available: $6,162.18" beside it. Neither
dollar figure exists: they are the same account counted six times.

Oversubscription is deliberate here (when several experts want cash at the same 09:30 the
losers' orders are simply discarded), so the fix is not to clamp the allocation -- it is to
stop calling it capital. ``summarize_capital`` counts each ACCOUNT's equity once and reports
the sleeve total separately, as a percentage of the money that backs it.

These drive the pure summary function; the chart's own rows come from
calculate_expert_balance_data, covered in test_virtual_equity_zero_pct.py.
"""
import pytest

from ba2_trade_platform.ui.components.BalanceUsagePerExpertChart import (
    BalanceUsagePerExpertChart,
)


def _row(account_id, account_total, pct, filled=0.0, pending=0.0):
    """One chart row, shaped exactly as calculate_expert_balance_data builds it."""
    total = account_total * (pct / 100.0)
    return {
        'filled': filled,
        'pending': pending,
        'available': max(0.0, total - filled - pending),
        'total': total,
        'account_id': account_id,
        'account_total': account_total,
    }


def _summarize(rows):
    return BalanceUsagePerExpertChart.summarize_capital(rows)


ACCOUNT_EQUITY = 1_711.72


class TestOneAccountBehindManySleeves:
    """The live shape: six 60% sleeves on a single account."""

    def _six_at_sixty(self, **kw):
        return {f"expert-{i}": _row(1, ACCOUNT_EQUITY, 60.0, **kw) for i in range(6)}

    def test_capital_counts_the_account_once_not_once_per_expert(self):
        s = _summarize(self._six_at_sixty())
        assert s['capital'] == pytest.approx(ACCOUNT_EQUITY)

    def test_the_sleeve_total_is_still_reported_as_allocation(self):
        """It is not wrong, it is just not capital -- the operator needs to see it."""
        s = _summarize(self._six_at_sixty())
        assert s['allocated'] == pytest.approx(ACCOUNT_EQUITY * 3.6)
        assert s['allocated_pct'] == pytest.approx(360.0)

    def test_available_can_never_exceed_the_real_account(self):
        """THE HEADLINE BUG: available was $6,162 on a $1,712 account."""
        s = _summarize(self._six_at_sixty())
        assert s['available'] == pytest.approx(ACCOUNT_EQUITY)
        assert s['available'] <= s['capital']

    def test_committed_money_is_subtracted_from_the_ACCOUNT_not_from_each_sleeve(self):
        """$200 filled in each of six sleeves is $1,200 of real positions, so $511.72 is
        free. Deriving availability per sleeve and summing would have said $4,962."""
        s = _summarize(self._six_at_sixty(filled=200.0))
        assert s['filled'] == pytest.approx(1_200.0)
        assert s['available'] == pytest.approx(ACCOUNT_EQUITY - 1_200.0)

    def test_available_floors_at_zero_when_positions_exceed_equity(self):
        """Marked-to-market positions can outrun equity on a margin account; the footer
        must not print a negative cash figure."""
        s = _summarize(self._six_at_sixty(filled=500.0))
        assert s['available'] == 0.0


class TestTheOrdinaryCases:
    """The inverses -- the everyday footer must not move."""

    def test_a_single_fully_allocated_expert_is_unchanged(self):
        s = _summarize({"solo": _row(1, 10_000.0, 100.0)})
        assert s['capital'] == pytest.approx(10_000.0)
        assert s['allocated'] == pytest.approx(10_000.0)
        assert s['allocated_pct'] == pytest.approx(100.0)
        assert s['available'] == pytest.approx(10_000.0)

    def test_undersubscribed_sleeves_report_below_one_hundred_percent(self):
        s = _summarize({"a": _row(1, 10_000.0, 25.0), "b": _row(1, 10_000.0, 25.0)})
        assert s['allocated'] == pytest.approx(5_000.0)
        assert s['allocated_pct'] == pytest.approx(50.0)
        assert s['capital'] == pytest.approx(10_000.0)

    def test_separate_accounts_DO_add_up(self):
        """The other half of counting once per account: two accounts are two piles of
        money, and capital must not collapse to one of them."""
        s = _summarize({"a": _row(1, 10_000.0, 50.0), "b": _row(2, 4_000.0, 50.0)})
        assert s['capital'] == pytest.approx(14_000.0)
        assert s['allocated'] == pytest.approx(7_000.0)

    def test_pending_and_filled_are_summed_across_experts(self):
        """A transaction belongs to exactly one expert, so these never double-count."""
        s = _summarize({
            "a": _row(1, 10_000.0, 50.0, filled=1_000.0, pending=250.0),
            "b": _row(1, 10_000.0, 50.0, filled=500.0, pending=125.0),
        })
        assert s['filled'] == pytest.approx(1_500.0)
        assert s['pending'] == pytest.approx(375.0)
        assert s['available'] == pytest.approx(10_000.0 - 1_875.0)


class TestDegenerateInputs:
    def test_no_experts_is_all_zeroes_and_no_percentage(self):
        s = _summarize({})
        assert s['capital'] == 0.0 and s['allocated'] == 0.0 and s['available'] == 0.0
        assert s['allocated_pct'] is None, "0/0 is not a percentage"

    def test_a_zero_balance_account_reports_no_percentage_rather_than_dividing(self):
        """A measured $0 account is charted (test_virtual_equity_zero_pct pins that); the
        footer must survive it without a ZeroDivisionError."""
        s = _summarize({"a": _row(1, 0.0, 60.0)})
        assert s['capital'] == 0.0
        assert s['allocated'] == 0.0
        assert s['allocated_pct'] is None
