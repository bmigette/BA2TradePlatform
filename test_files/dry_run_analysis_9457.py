"""
Dry run rule evaluation for market analysis 9457.

This script evaluates trading rules for analysis 9457 to debug unexpected target price.
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db, get_instance
from ba2_trade_platform.core.models import MarketAnalysis, ExpertRecommendation, ExpertInstance
from ba2_trade_platform.core.utils import get_account_instance_from_id
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.logger import logger
from sqlmodel import select
from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel

console = Console()


def dry_run_analysis_9457():
    """Run dry run evaluation for market analysis 9457."""
    analysis_id = 9457
    
    console.print(Panel(f"[bold cyan]Dry Run Rule Evaluation for Market Analysis {analysis_id}[/bold cyan]", 
                       box=box.DOUBLE))
    
    with get_db() as session:
        # Get the market analysis
        analysis = get_instance(MarketAnalysis, analysis_id)
        if not analysis:
            console.print(f"[red]❌ Market analysis {analysis_id} not found[/red]")
            return
        
        console.print(f"\n[yellow]📊 Market Analysis {analysis_id}[/yellow]")
        console.print(f"  Symbol: {analysis.symbol}")
        console.print(f"  Created: {analysis.created_at}")
        console.print(f"  Expert Instance ID: {analysis.expert_instance_id}")
        console.print(f"  Subtype: {analysis.subtype}")
        console.print(f"  Status: {analysis.status}")
        
        # Get expert instance
        expert_instance = get_instance(ExpertInstance, analysis.expert_instance_id)
        if not expert_instance:
            console.print(f"[red]❌ Expert instance {analysis.expert_instance_id} not found[/red]")
            return
        
        console.print(f"\n[yellow]🤖 Expert Instance[/yellow]")
        console.print(f"  ID: {expert_instance.id}")
        console.print(f"  Expert: {expert_instance.expert}")
        console.print(f"  Alias: {expert_instance.alias}")
        console.print(f"  Account ID: {expert_instance.account_id}")
        
        # Get recommendations for this analysis
        stmt = select(ExpertRecommendation).where(
            ExpertRecommendation.market_analysis_id == analysis_id
        )
        recommendations = session.exec(stmt).all()
        
        if not recommendations:
            console.print(f"[red]❌ No recommendations found for analysis {analysis_id}[/red]")
            return
        
        console.print(f"\n[yellow]📋 Recommendations ({len(recommendations)})[/yellow]")
        
        # Create recommendations table
        rec_table = Table(title="Expert Recommendations", box=box.ROUNDED)
        rec_table.add_column("ID", style="cyan")
        rec_table.add_column("Symbol", style="green")
        rec_table.add_column("Action", style="bold")
        rec_table.add_column("Confidence", justify="right")
        rec_table.add_column("Expected Profit %", justify="right")
        rec_table.add_column("Price at Date", justify="right")
        rec_table.add_column("Risk", style="yellow")
        rec_table.add_column("Time Horizon", style="blue")
        
        for rec in recommendations:
            action_color = "green" if rec.recommended_action.value == "BUY" else "red" if rec.recommended_action.value == "SELL" else "yellow"
            rec_table.add_row(
                str(rec.id),
                rec.symbol,
                f"[{action_color}]{rec.recommended_action.value}[/{action_color}]",
                f"{rec.confidence:.1f}%" if rec.confidence else "N/A",
                f"{rec.expected_profit_percent:.2f}%" if rec.expected_profit_percent else "N/A",
                f"${rec.price_at_date:.2f}" if rec.price_at_date else "N/A",
                rec.risk_level.value if rec.risk_level else "N/A",
                rec.time_horizon.value.replace('_', ' ').title() if rec.time_horizon else "N/A"
            )
        
        console.print(rec_table)
        
        # Get account instance
        try:
            account = get_account_instance_from_id(expert_instance.account_id)
            console.print(f"\n[yellow]💼 Account Instance[/yellow]")
            console.print(f"  Account ID: {expert_instance.account_id}")
            console.print(f"  Account Type: {type(account).__name__}")
        except Exception as e:
            console.print(f"[red]❌ Error getting account instance: {e}[/red]")
            return
        
        # Now perform dry run evaluation for each recommendation
        console.print(f"\n[bold yellow]🔍 DRY RUN RULE EVALUATION[/bold yellow]")
        
        trade_manager = TradeManager(account)
        
        for rec in recommendations:
            console.print(f"\n[cyan]{'='*80}[/cyan]")
            console.print(f"[bold white]Recommendation {rec.id} - {rec.symbol}[/bold white]")
            console.print(f"[cyan]{'='*80}[/cyan]")
            
            # Evaluate rules for this recommendation
            try:
                results = trade_manager.evaluate_recommendations_for_instruments(
                    recommendations=[rec],
                    expert=expert_instance,
                    dry_run=True
                )
                
                if results:
                    for result in results:
                        console.print(f"\n[green]✓ Evaluation Result:[/green]")
                        console.print(f"  Recommendation ID: {result.expert_recommendation_id}")
                        console.print(f"  Action Taken: {result.action_taken}")
                        console.print(f"  Success: {result.success}")
                        console.print(f"  Message: {result.message}")
                        
                        if result.data:
                            console.print(f"\n[yellow]📊 Evaluation Details:[/yellow]")
                            
                            # Extract evaluation details
                            eval_details = result.data.get('evaluation_details', {})
                            if eval_details:
                                console.print(f"  Rules Evaluated: {eval_details.get('total_rules_evaluated', 0)}")
                                console.print(f"  Matching Rulesets: {len(eval_details.get('matching_rulesets', []))}")
                                
                                # Show matching rulesets
                                matching = eval_details.get('matching_rulesets', [])
                                if matching:
                                    console.print(f"\n[green]✓ Matching Rulesets:[/green]")
                                    for ruleset in matching:
                                        console.print(f"\n  [bold]Ruleset: {ruleset.get('ruleset_name', 'Unknown')}[/bold]")
                                        console.print(f"    Use Case: {ruleset.get('use_case', 'N/A')}")
                                        console.print(f"    Priority: {ruleset.get('priority', 'N/A')}")
                                        
                                        # Show actions
                                        actions = ruleset.get('actions', [])
                                        if actions:
                                            console.print(f"    Actions ({len(actions)}):")
                                            for action in actions:
                                                action_type = action.get('type', 'Unknown')
                                                console.print(f"      - {action_type}")
                                                
                                                # Extract order details for open position actions
                                                if action_type in ['OPEN_BUY_POSITION', 'OPEN_SELL_POSITION']:
                                                    order_config = action.get('order_config', {})
                                                    console.print(f"        Target Price: {order_config.get('target_price', 'N/A')}")
                                                    console.print(f"        Stop Loss: {order_config.get('stop_loss', 'N/A')}")
                                                    console.print(f"        Quantity: {order_config.get('quantity', 'N/A')}")
                                                    
                                                    # THIS IS THE KEY: Show where target price comes from
                                                    if 'target_price_source' in order_config:
                                                        console.print(f"        [bold yellow]Target Price Source: {order_config['target_price_source']}[/bold yellow]")
                                                    if 'target_price_calculation' in order_config:
                                                        console.print(f"        [bold yellow]Target Price Calculation: {order_config['target_price_calculation']}[/bold yellow]")
                                        
                                        # Show conditions
                                        conditions_str = ruleset.get('conditions_str', '')
                                        if conditions_str:
                                            console.print(f"    Conditions: {conditions_str}")
                                        
                                        # Show condition results
                                        condition_results = ruleset.get('condition_results', [])
                                        if condition_results:
                                            console.print(f"    Condition Evaluation:")
                                            for cond_result in condition_results:
                                                result_icon = "✓" if cond_result.get('result') else "✗"
                                                result_color = "green" if cond_result.get('result') else "red"
                                                console.print(f"      [{result_color}]{result_icon}[/{result_color}] {cond_result.get('description', 'N/A')}")
                                                if 'calculated_value' in cond_result and cond_result['calculated_value'] is not None:
                                                    console.print(f"         Calculated Value: {cond_result['calculated_value']}")
                                
                                # Show non-matching rulesets
                                non_matching = eval_details.get('non_matching_rulesets', [])
                                if non_matching:
                                    console.print(f"\n[red]✗ Non-Matching Rulesets:[/red]")
                                    for ruleset in non_matching:
                                        console.print(f"\n  [bold]Ruleset: {ruleset.get('ruleset_name', 'Unknown')}[/bold]")
                                        console.print(f"    Use Case: {ruleset.get('use_case', 'N/A')}")
                                        console.print(f"    Reason: {ruleset.get('reason', 'N/A')}")
                                        
                                        # Show condition results
                                        condition_results = ruleset.get('condition_results', [])
                                        if condition_results:
                                            console.print(f"    Condition Evaluation:")
                                            for cond_result in condition_results:
                                                result_icon = "✓" if cond_result.get('result') else "✗"
                                                result_color = "green" if cond_result.get('result') else "red"
                                                console.print(f"      [{result_color}]{result_icon}[/{result_color}] {cond_result.get('description', 'N/A')}")
                                                if 'calculated_value' in cond_result and cond_result['calculated_value'] is not None:
                                                    console.print(f"         Calculated Value: {cond_result['calculated_value']}")
                            else:
                                console.print("  [yellow]No evaluation details found in result data[/yellow]")
                        
                        # Show created orders
                        if result.trading_order_ids:
                            console.print(f"\n[green]📝 Created Orders: {result.trading_order_ids}[/green]")
                else:
                    console.print("[yellow]No evaluation results returned[/yellow]")
                    
            except Exception as e:
                console.print(f"[red]❌ Error evaluating recommendation {rec.id}: {e}[/red]")
                logger.error(f"Error evaluating recommendation {rec.id}: {e}", exc_info=True)
    
    console.print(f"\n[bold green]✓ Dry run evaluation complete for analysis {analysis_id}[/bold green]")


if __name__ == "__main__":
    dry_run_analysis_9457()
