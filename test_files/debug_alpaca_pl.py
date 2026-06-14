"""
Diagnostic script to inspect Alpaca balance history profit_loss field
around deposit/withdrawal dates (Oct 29, Nov 25, Dec 18 2025).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from sqlmodel import select
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.utils import get_account_instance_from_id


# Dates of interest (deposits/withdrawals)
DATES_OF_INTEREST = [
    datetime(2025, 10, 29, tzinfo=timezone.utc),
    datetime(2025, 11, 25, tzinfo=timezone.utc),
    datetime(2025, 12, 18, tzinfo=timezone.utc),
]

# How many days around each date to show
WINDOW_DAYS = 3


def print_table(rows):
    """Print a formatted table of balance history entries."""
    header = f"{'Date':<14} {'Net Liq Value':>16} {'Cash Balance':>14} {'Equity Value':>14} {'Profit/Loss':>14}"
    print(header)
    print("-" * len(header))
    for row in rows:
        dt = row['date']
        date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt)
        print(
            f"{date_str:<14} "
            f"{row.get('net_liquidating_value', 0):>16,.2f} "
            f"{row.get('cash_balance', 0):>14,.2f} "
            f"{row.get('equity_value', 0):>14,.2f} "
            f"{row.get('profit_loss', 0):>14,.2f}"
        )


def main():
    session = get_db()
    try:
        accounts = session.exec(select(AccountDefinition)).all()
        print(f"Found {len(accounts)} account(s) in database\n")

        for account_def in accounts:
            print(f"{'=' * 80}")
            print(f"Account #{account_def.id}: {account_def.name} (provider: {account_def.provider})")
            print(f"{'=' * 80}")

            account = get_account_instance_from_id(account_def.id, session=session)
            if not account:
                print("  -> Could not instantiate account, skipping.\n")
                continue

            # Fetch balance history covering Oct-Dec 2025
            start = datetime(2025, 10, 1, tzinfo=timezone.utc)
            end = datetime(2025, 12, 31, tzinfo=timezone.utc)

            print(f"\nFetching balance history from {start.date()} to {end.date()}...")
            history = account.get_balance_history(start_date=start, end_date=end)

            if not history:
                print("  -> No balance history returned.\n")
                continue

            print(f"  -> Got {len(history)} entries total\n")

            # Show full Oct-Dec history
            print("--- FULL HISTORY (Oct-Dec 2025) ---\n")
            print_table(history)

            # Zoom in around each date of interest
            for doi in DATES_OF_INTEREST:
                print(f"\n--- AROUND {doi.strftime('%Y-%m-%d')} (+/- {WINDOW_DAYS} days) ---\n")
                nearby = []
                for entry in history:
                    entry_date = entry['date']
                    # Normalize to date for comparison
                    if hasattr(entry_date, 'date'):
                        entry_d = entry_date.date()
                    elif isinstance(entry_date, datetime):
                        entry_d = entry_date.date()
                    else:
                        # It's already a date object
                        entry_d = entry_date
                    diff = abs((entry_d - doi.date()).days)
                    if diff <= WINDOW_DAYS:
                        nearby.append(entry)

                if nearby:
                    print_table(nearby)
                else:
                    print("  (no entries within window)")

            # Also print raw profit_loss values for quick scanning
            print(f"\n--- PROFIT/LOSS SUMMARY ---\n")
            print(f"{'Date':<14} {'P/L':>14} {'Day-over-Day Equity Change':>28}")
            print("-" * 60)
            prev_equity = None
            for entry in history:
                dt = entry['date']
                date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, 'strftime') else str(dt)
                pnl = entry.get('profit_loss', 0)
                equity = entry.get('net_liquidating_value', 0)
                if prev_equity is not None:
                    equity_change = equity - prev_equity
                    diff_str = f"{equity_change:>28,.2f}"
                else:
                    diff_str = f"{'N/A':>28}"
                print(f"{date_str:<14} {pnl:>14,.2f} {diff_str}")
                prev_equity = equity

            print()

    finally:
        session.close()


if __name__ == "__main__":
    main()
