"""
Compare bid/ask prices for positions across different accounts.

This script:
1. Gets all positions from account #1
2. Fetches current prices for those symbols from both accounts
3. Compares bid/ask prices and calculates differences
4. Displays results in a formatted table
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.core.interfaces import AccountInterface
from ba2_trade_platform.core.models import AccountDefinition
from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.logger import logger
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text


def get_account_prices(account: AccountInterface, symbol: str) -> dict:
    """
    Get bid/ask prices for a symbol from an account.
    
    Fetches both bid and ask prices separately to ensure accurate spread calculation.
    
    Returns:
        dict with keys: bid, ask, mid, spread, spread_pct, error
    """
    try:
        # Fetch bid and ask prices separately using the price_type parameter
        bid = account.get_instrument_current_price(symbol, price_type='bid')
        ask = account.get_instrument_current_price(symbol, price_type='ask')
        
        if bid is None and ask is None:
            return {"error": "Both bid and ask prices are None"}
        
        # Handle missing bid or ask
        if bid is None:
            bid = ask  # Fallback to ask if bid missing
        if ask is None:
            ask = bid  # Fallback to bid if ask missing
        
        # Calculate mid, spread, and spread percentage
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = (spread / mid) * 100 if mid > 0 else 0
        
        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct
        }
    
    except Exception as e:
        return {"error": str(e)}


def compare_account_prices(account1_id: int = 1, account2_id: int = 2):
    """
    Compare prices for all symbols with positions on account1 against account2.
    """
    console = Console()
    
    try:
        # Load accounts
        console.print(f"\n[bold cyan]Loading accounts...[/bold cyan]")
        account1 = get_account_instance_from_id(account1_id)
        account2 = get_account_instance_from_id(account2_id)
        
        if not account1:
            console.print(f"[red]Error: Account {account1_id} not found[/red]")
            return
        
        if not account2:
            console.print(f"[red]Error: Account {account2_id} not found[/red]")
            return
        
        # Get account names from database
        account1_def = get_instance(AccountDefinition, account1_id)
        account2_def = get_instance(AccountDefinition, account2_id)
        
        console.print(f"Account 1: [green]{account1_def.name}[/green] (ID: {account1_id}, Provider: {account1_def.provider})")
        console.print(f"Account 2: [green]{account2_def.name}[/green] (ID: {account2_id}, Provider: {account2_def.provider})")
        
        # Get positions from account 1
        console.print(f"\n[bold cyan]Fetching positions from account 1...[/bold cyan]")
        positions = account1.get_positions()
        
        if not positions:
            console.print("[yellow]No positions found on account 1[/yellow]")
            return
        
        console.print(f"Found [green]{len(positions)}[/green] positions")
        
        # Get unique symbols
        symbols = list(set(pos.symbol for pos in positions))
        symbols.sort()
        
        console.print(f"Unique symbols: [green]{len(symbols)}[/green]")
        console.print(f"Symbols: {', '.join(symbols)}\n")
        
        # Fetch prices from both accounts
        console.print("[bold cyan]Fetching prices from both accounts...[/bold cyan]\n")
        
        results = []
        for symbol in symbols:
            console.print(f"Fetching {symbol}...", end=" ")
            
            prices1 = get_account_prices(account1, symbol)
            prices2 = get_account_prices(account2, symbol)
            
            results.append({
                "symbol": symbol,
                "account1": prices1,
                "account2": prices2
            })
            
            # Quick status
            if "error" in prices1 or "error" in prices2:
                console.print("[red]ERROR[/red]")
            else:
                console.print("[green]OK[/green]")
        
        # Display results in table
        console.print("\n" + "="*120)
        console.print("[bold cyan]PRICE COMPARISON RESULTS[/bold cyan]")
        console.print("="*120 + "\n")
        
        # Sort results by spread percentage (highest first)
        # Use Account 1's spread for sorting (or Account 2 if Account 1 has error)
        def get_sort_key(result):
            p1 = result["account1"]
            p2 = result["account2"]
            
            # Prioritize errors last (use -1 so they sort to bottom)
            if "error" in p1 and "error" in p2:
                return -999.0  # Both errors - sort to bottom
            elif "error" in p1:
                return p2.get("spread_pct", -999.0)  # Use account2 spread
            elif "error" in p2:
                return p1.get("spread_pct", -999.0)  # Use account1 spread
            else:
                # Use max of both accounts' spreads
                return max(p1.get("spread_pct", 0.0), p2.get("spread_pct", 0.0))
        
        results.sort(key=get_sort_key, reverse=True)
        
        table = Table(title="Bid/Ask Price Comparison (Sorted by Spread)", show_header=True, header_style="bold magenta")
        table.add_column("Symbol", style="cyan", width=8)
        table.add_column("Account 1\nBid", justify="right", width=12)
        table.add_column("Account 1\nAsk", justify="right", width=12)
        table.add_column("Account 1\nSpread %", justify="right", width=12)
        table.add_column("Account 2\nBid", justify="right", width=12)
        table.add_column("Account 2\nAsk", justify="right", width=12)
        table.add_column("Account 2\nSpread %", justify="right", width=12)
        table.add_column("Bid\nDiff %", justify="right", width=10)
        table.add_column("Ask\nDiff %", justify="right", width=10)
        table.add_column("Notes", width=20)
        
        for result in results:
            symbol = result["symbol"]
            p1 = result["account1"]
            p2 = result["account2"]
            
            # Handle errors
            if "error" in p1:
                table.add_row(
                    symbol,
                    "[red]ERROR[/red]", "", "",
                    "", "", "", "", "",
                    f"Acct1: {p1['error']}"
                )
                continue
            
            if "error" in p2:
                table.add_row(
                    symbol,
                    f"${p1['bid']:.4f}", f"${p1['ask']:.4f}", f"{p1['spread_pct']:.3f}%",
                    "[red]ERROR[/red]", "", "", "", "",
                    f"Acct2: {p2['error']}"
                )
                continue
            
            # Calculate differences
            bid_diff_pct = ((p2['bid'] - p1['bid']) / p1['bid'] * 100) if p1['bid'] > 0 else 0
            ask_diff_pct = ((p2['ask'] - p1['ask']) / p1['ask'] * 100) if p1['ask'] > 0 else 0
            
            # Color code differences (red if >0.1% difference)
            bid_diff_color = "red" if abs(bid_diff_pct) > 0.1 else "green"
            ask_diff_color = "red" if abs(ask_diff_pct) > 0.1 else "green"
            
            # Notes
            notes = []
            if p1.get('note'):
                notes.append(f"A1: {p1['note']}")
            if p2.get('note'):
                notes.append(f"A2: {p2['note']}")
            note_text = "; ".join(notes) if notes else ""
            
            table.add_row(
                symbol,
                f"${p1['bid']:.4f}",
                f"${p1['ask']:.4f}",
                f"{p1['spread_pct']:.3f}%",
                f"${p2['bid']:.4f}",
                f"${p2['ask']:.4f}",
                f"{p2['spread_pct']:.3f}%",
                f"[{bid_diff_color}]{bid_diff_pct:+.3f}%[/{bid_diff_color}]",
                f"[{ask_diff_color}]{ask_diff_pct:+.3f}%[/{ask_diff_color}]",
                note_text
            )
        
        console.print(table)
        
        # Summary statistics
        console.print("\n[bold cyan]SUMMARY[/bold cyan]")
        
        valid_results = [r for r in results if "error" not in r["account1"] and "error" not in r["account2"]]
        
        if valid_results:
            bid_diffs = [
                abs((r["account2"]["bid"] - r["account1"]["bid"]) / r["account1"]["bid"] * 100)
                for r in valid_results if r["account1"]["bid"] > 0
            ]
            ask_diffs = [
                abs((r["account2"]["ask"] - r["account1"]["ask"]) / r["account1"]["ask"] * 100)
                for r in valid_results if r["account1"]["ask"] > 0
            ]
            
            console.print(f"Valid comparisons: [green]{len(valid_results)}/{len(results)}[/green]")
            
            if bid_diffs:
                console.print(f"Bid differences - Avg: [cyan]{sum(bid_diffs)/len(bid_diffs):.3f}%[/cyan], Max: [cyan]{max(bid_diffs):.3f}%[/cyan]")
            
            if ask_diffs:
                console.print(f"Ask differences - Avg: [cyan]{sum(ask_diffs)/len(ask_diffs):.3f}%[/cyan], Max: [cyan]{max(ask_diffs):.3f}%[/cyan]")
            
            # Flag significant differences
            significant = [
                r for r in valid_results
                if abs((r["account2"]["bid"] - r["account1"]["bid"]) / r["account1"]["bid"] * 100) > 0.1
                or abs((r["account2"]["ask"] - r["account1"]["ask"]) / r["account1"]["ask"] * 100) > 0.1
            ]
            
            if significant:
                console.print(f"\n[yellow]⚠ Symbols with >0.1% price difference: {len(significant)}[/yellow]")
                for r in significant:
                    bid_diff = ((r["account2"]["bid"] - r["account1"]["bid"]) / r["account1"]["bid"] * 100)
                    ask_diff = ((r["account2"]["ask"] - r["account1"]["ask"]) / r["account1"]["ask"] * 100)
                    console.print(f"  • {r['symbol']}: Bid {bid_diff:+.3f}%, Ask {ask_diff:+.3f}%")
            else:
                console.print(f"\n[green]✓ All prices within 0.1% tolerance[/green]")
        
        console.print()
        
    except Exception as e:
        logger.error(f"Error comparing prices: {e}", exc_info=True)
        console.print(f"[red]Error: {e}[/red]")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Compare bid/ask prices across accounts")
    parser.add_argument("--account1", type=int, default=1, help="First account ID (default: 1)")
    parser.add_argument("--account2", type=int, default=2, help="Second account ID (default: 2)")
    
    args = parser.parse_args()
    
    compare_account_prices(args.account1, args.account2)
