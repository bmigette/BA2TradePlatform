"""
Test script for evaluating rulesets against market analysis and recommendations.

This script allows you to:
- Test ruleset evaluation on recent or historical recommendations
- Run in dry-run mode (no orders created)
- Manually specify recommendations to test
- See detailed evaluation results

Usage:
    # Test latest recommendations for all experts
    python test_rulesets.py
    
    # Test specific expert
    python test_rulesets.py --expert-id 1
    
    # Test specific recommendations
    python test_rulesets.py --recommendation-ids 123,456,789
    
    # Include older recommendations (default is 24h)
    python test_rulesets.py --hours 168  # 7 days
    
    # Verbose output
    python test_rulesets.py --verbose
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.models import ExpertRecommendation, ExpertInstance, Ruleset, TradingOrder, AccountDefinition
from ba2_trade_platform.core.types import OrderRecommendation, OrderDirection, OrderType, OrderStatus, OrderOpenType, AnalysisUseCase
from ba2_trade_platform.core.db import get_instance, get_all_instances, add_instance, get_db
from ba2_trade_platform.core.TradeManager import TradeManager
from ba2_trade_platform.core.TradeActionEvaluator import TradeActionEvaluator
from ba2_trade_platform.core.utils import get_expert_instance_from_id
from ba2_trade_platform.modules.accounts import get_account_class
from sqlmodel import select, Session


class RulesetTester:
    """Test and evaluate rulesets against recommendations."""
    
    def __init__(self, dry_run: bool = True, verbose: bool = False):
        """
        Initialize the ruleset tester.
        
        Args:
            dry_run: If True, don't create orders (just evaluate)
            verbose: If True, show detailed evaluation info
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.trade_manager = TradeManager()
        self.results = []
    
    def _get_ruleset_with_relations(self, ruleset_id: int) -> Optional[Ruleset]:
        """
        Get a ruleset with its event_actions relationship eagerly loaded.
        
        Args:
            ruleset_id: The ID of the ruleset to fetch
            
        Returns:
            Ruleset with event_actions loaded, or None if not found
        """
        try:
            from sqlalchemy.orm import selectinload
            
            with get_db() as session:
                # Use selectinload to eagerly load the event_actions relationship
                statement = select(Ruleset).where(Ruleset.id == ruleset_id).options(
                    selectinload(Ruleset.event_actions)
                )
                ruleset = session.scalars(statement).first()
                
                if not ruleset:
                    logger.warning(f"Ruleset {ruleset_id} not found")
                    return None
                
                # Expunge to make it usable outside the session
                session.expunge(ruleset)
                
                return ruleset
                
        except Exception as e:
            logger.error(f"Error fetching ruleset {ruleset_id}: {e}", exc_info=True)
            return None
        
    def test_expert_recommendations(
        self, 
        expert_instance_id: int, 
        hours: int = 24,
        recommendation_ids: Optional[List[int]] = None
    ) -> Dict:
        """
        Test ruleset evaluation for an expert's recommendations.
        
        Args:
            expert_instance_id: The expert instance ID to test
            hours: How many hours back to look for recommendations
            recommendation_ids: Specific recommendation IDs to test (overrides hours)
            
        Returns:
            Dictionary with test results
        """
        print(f"\n{'='*80}")
        print(f"Testing Expert Instance ID: {expert_instance_id}")
        print(f"{'='*80}")
        
        # Get the expert instance
        expert = get_expert_instance_from_id(expert_instance_id)
        if not expert:
            print(f"❌ Expert instance {expert_instance_id} not found")
            return {'error': 'Expert not found'}
        
        expert_instance = get_instance(ExpertInstance, expert_instance_id)
        if not expert_instance:
            print(f"❌ Expert instance model {expert_instance_id} not found")
            return {'error': 'Expert instance not found'}
        
        # Display expert info
        print(f"\nExpert Type: {expert_instance.expert}")
        print(f"Enabled: {expert_instance.enabled}")
        print(f"Account ID: {expert_instance.account_id}")
        
        # Check automation settings
        allow_auto_open = expert.settings.get('allow_automated_trade_opening', False)
        allow_auto_modify = expert.settings.get('allow_automated_trade_modification', False)
        print(f"Allow Automated Trade Opening: {allow_auto_open}")
        print(f"Allow Automated Trade Modification: {allow_auto_modify}")
        
        # Display assigned rulesets
        enter_market_ruleset = None
        open_positions_ruleset = None
        
        if expert_instance.enter_market_ruleset_id:
            enter_market_ruleset = self._get_ruleset_with_relations(expert_instance.enter_market_ruleset_id)
            if enter_market_ruleset:
                print(f"\n📋 Enter Market Ruleset: {enter_market_ruleset.name}")
                if enter_market_ruleset.description:
                    print(f"   Description: {enter_market_ruleset.description}")
                print(f"   Event Actions: {len(enter_market_ruleset.event_actions)}")
            else:
                print(f"\n⚠️  Enter Market Ruleset ID {expert_instance.enter_market_ruleset_id} not found!")
        else:
            print(f"\n⚠️  No Enter Market Ruleset assigned")
        
        if expert_instance.open_positions_ruleset_id:
            open_positions_ruleset = self._get_ruleset_with_relations(expert_instance.open_positions_ruleset_id)
            if open_positions_ruleset:
                print(f"\n📋 Open Positions Ruleset: {open_positions_ruleset.name}")
                if open_positions_ruleset.description:
                    print(f"   Description: {open_positions_ruleset.description}")
                print(f"   Event Actions: {len(open_positions_ruleset.event_actions)}")
        
        # Get recommendations
        recommendations = self._get_recommendations(expert_instance_id, hours, recommendation_ids)
        
        if not recommendations:
            print(f"\n⚠️  No recommendations found")
            return {'expert_id': expert_instance_id, 'recommendations': 0, 'passed': 0, 'failed': 0}
        
        print(f"\n📊 Found {len(recommendations)} recommendations to evaluate")
        print(f"{'='*80}\n")
        
        # Evaluate each recommendation
        passed = 0
        failed = 0
        results = []
        
        for idx, recommendation in enumerate(recommendations, 1):
            result = self._evaluate_recommendation(
                recommendation, 
                expert_instance,
                enter_market_ruleset,
                idx,
                len(recommendations)
            )
            results.append(result)
            
            if result['passed']:
                passed += 1
            else:
                failed += 1
        
        # Summary
        print(f"\n{'='*80}")
        print(f"SUMMARY - Expert {expert_instance_id}")
        print(f"{'='*80}")
        print(f"Total Recommendations: {len(recommendations)}")
        print(f"✅ Passed Ruleset: {passed}")
        print(f"❌ Failed Ruleset: {failed}")
        print(f"Success Rate: {(passed/len(recommendations)*100):.1f}%")
        
        if not self.dry_run and passed > 0 and allow_auto_open and enter_market_ruleset:
            print(f"\n⚠️  DRY RUN DISABLED: {passed} orders would be created!")
        elif self.dry_run and passed > 0:
            print(f"\n✓ DRY RUN MODE: No orders created (would create {passed} orders)")
        
        return {
            'expert_id': expert_instance_id,
            'recommendations': len(recommendations),
            'passed': passed,
            'failed': failed,
            'results': results
        }
    
    def _get_recommendations(
        self, 
        expert_instance_id: int, 
        hours: int,
        recommendation_ids: Optional[List[int]] = None
    ) -> List[ExpertRecommendation]:
        """Get recommendations to test."""
        with Session(get_db().bind) as session:
            if recommendation_ids:
                # Get specific recommendations
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.id.in_(recommendation_ids)
                )
                print(f"Loading specific recommendations: {recommendation_ids}")
            else:
                # Get recommendations from last X hours
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
                statement = select(ExpertRecommendation).where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    ExpertRecommendation.created_at >= cutoff_time,
                    ExpertRecommendation.recommended_action != OrderRecommendation.HOLD
                ).order_by(ExpertRecommendation.expected_profit_percent.desc())
                print(f"Looking for recommendations from last {hours} hours")
            
            recommendations = session.exec(statement).all()
            return list(recommendations)
    
    def _evaluate_recommendation(
        self,
        recommendation: ExpertRecommendation,
        expert_instance: ExpertInstance,
        ruleset: Optional[Ruleset],
        index: int,
        total: int
    ) -> Dict:
        """Evaluate a single recommendation against the ruleset using TradeActionEvaluator."""
        
        print(f"[{index}/{total}] Testing Recommendation #{recommendation.id}")
        print(f"  Symbol: {recommendation.symbol}")
        print(f"  Action: {recommendation.recommended_action.value}")
        print(f"  Confidence: {recommendation.confidence}")
        print(f"  Risk Level: {recommendation.risk_level.value}")
        print(f"  Expected Profit: {recommendation.expected_profit_percent}%")
        print(f"  Time Horizon: {recommendation.time_horizon.value}")
        print(f"  Created: {recommendation.created_at}")
        
        # Evaluate against ruleset using TradeActionEvaluator
        if not ruleset:
            print(f"  ⚠️  No ruleset to evaluate against - would PASS by default")
            passed = True
            reason = "No ruleset configured"
            action_summaries = []
        else:
            print(f"  📋 Evaluating against ruleset: {ruleset.name}")
            
            try:
                # Get the account instance for this expert
                account_def = get_instance(AccountDefinition, expert_instance.account_id)
                if not account_def:
                    print(f"  ❌ Account definition {expert_instance.account_id} not found")
                    return {
                        'recommendation_id': recommendation.id,
                        'symbol': recommendation.symbol,
                        'action': recommendation.recommended_action.value,
                        'confidence': recommendation.confidence,
                        'risk_level': recommendation.risk_level.value,
                        'expected_profit': recommendation.expected_profit_percent,
                        'passed': False,
                        'reason': 'Account not found',
                        'order_created': False,
                        'order_id': None
                    }
                
                account_class = get_account_class(account_def.provider)
                if not account_class:
                    print(f"  ❌ Account provider {account_def.provider} not found")
                    return {
                        'recommendation_id': recommendation.id,
                        'symbol': recommendation.symbol,
                        'action': recommendation.recommended_action.value,
                        'confidence': recommendation.confidence,
                        'risk_level': recommendation.risk_level.value,
                        'expected_profit': recommendation.expected_profit_percent,
                        'passed': False,
                        'reason': 'Account provider not found',
                        'order_created': False,
                        'order_id': None
                    }
                
                account = account_class(account_def.id)
                
                # Create TradeActionEvaluator
                evaluator = TradeActionEvaluator(account)
                
                # Evaluate recommendation through the ruleset
                action_summaries = evaluator.evaluate(
                    instrument_name=recommendation.symbol,
                    expert_recommendation=recommendation,
                    ruleset_id=ruleset.id,
                    existing_order=None
                )
                
                # Check if evaluation produced any actions
                if not action_summaries:
                    passed = False
                    reason = "No actions produced - conditions not met"
                    print(f"  ❌ FAILED ruleset evaluation - no actions to execute")
                elif any('error' in summary for summary in action_summaries):
                    passed = False
                    errors = [s.get('error') for s in action_summaries if 'error' in s]
                    reason = f"Evaluation errors: {errors}"
                    print(f"  ❌ FAILED ruleset evaluation - errors: {errors}")
                else:
                    passed = True
                    reason = f"Passed - {len(action_summaries)} action(s) to execute"
                    print(f"  ✅ PASSED ruleset evaluation - {len(action_summaries)} action(s) ready")
                    
                    # Show evaluation details in verbose mode
                    if self.verbose and action_summaries:
                        eval_details = evaluator.get_evaluation_details()
                        print(f"  📊 Evaluation Details:")
                        print(f"     Conditions Evaluated: {eval_details['summary']['total_conditions']}")
                        print(f"     Conditions Passed: {eval_details['summary']['passed_conditions']}")
                        print(f"     Rules Evaluated: {eval_details['summary']['total_rules']}")
                        print(f"     Rules Executed: {eval_details['summary']['executed_rules']}")
                        
                        for action_summary in action_summaries:
                            print(f"     Action: {action_summary.get('description', 'Unknown')}")
                
            except Exception as e:
                logger.error(f"Error evaluating recommendation {recommendation.id}: {e}", exc_info=True)
                passed = False
                reason = f"Evaluation error: {str(e)}"
                action_summaries = []
                print(f"  ❌ ERROR during evaluation: {e}")
        
        # Show what would happen
        order_id = None
        if passed and not self.dry_run:
            print(f"  🔨 Creating orders via TradeActionEvaluator.execute()...")
            # Actually execute the actions (creates orders)
            try:
                # Get the account instance again for execution
                account_def = get_instance(AccountDefinition, expert_instance.account_id)
                account_class = get_account_class(account_def.provider)
                account = account_class(account_def.id)
                evaluator = TradeActionEvaluator(account)
                
                # Re-evaluate to set up actions for execution
                evaluator.evaluate(
                    instrument_name=recommendation.symbol,
                    expert_recommendation=recommendation,
                    ruleset_id=ruleset.id,
                    existing_order=None
                )
                
                # Execute the actions
                execution_results = evaluator.execute()
                
                for result in execution_results:
                    if result.get('success', False):
                        print(f"  ✓ Action executed: {result.get('description', 'Unknown')}")
                        
                        # Check if a TradingOrder was created
                        if result.get('data') and isinstance(result['data'], dict):
                            result_order_id = result['data'].get('order_id')
                            if result_order_id:
                                order_id = result_order_id
                                print(f"  ✓ Order {order_id} created")
                    else:
                        print(f"  ❌ Action failed: {result.get('message', 'Unknown error')}")
                
            except Exception as e:
                logger.error(f"Error executing actions for recommendation {recommendation.id}: {e}", exc_info=True)
                print(f"  ❌ Failed to execute actions: {e}")
                
        elif passed:
            print(f"  💭 DRY RUN: Would execute {len(action_summaries)} action(s) via TradeActionEvaluator")
            if self.verbose and action_summaries:
                for action_summary in action_summaries:
                    print(f"     - {action_summary.get('description', 'Unknown action')}")
        else:
            print(f"  ⏭️  Skipping - conditions not met")
        
        print()  # Blank line between recommendations
        
        return {
            'recommendation_id': recommendation.id,
            'symbol': recommendation.symbol,
            'action': recommendation.recommended_action.value,
            'confidence': recommendation.confidence,
            'risk_level': recommendation.risk_level.value,
            'expected_profit': recommendation.expected_profit_percent,
            'passed': passed,
            'reason': reason,
            'order_created': order_id is not None,
            'order_id': order_id
        }


def main():
    """Main entry point for the test script."""
    parser = argparse.ArgumentParser(
        description='Test ruleset evaluation against market recommendations',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test latest recommendations for all experts
  python test_rulesets.py
  
  # Test specific expert
  python test_rulesets.py --expert-id 1
  
  # Test specific recommendations
  python test_rulesets.py --recommendation-ids 123,456,789
  
  # Include older recommendations (default is 24h)
  python test_rulesets.py --hours 168  # 7 days
  
  # Actually create orders (not dry-run)
  python test_rulesets.py --no-dry-run
  
  # Verbose output
  python test_rulesets.py --verbose
        """
    )
    
    parser.add_argument(
        '--expert-id',
        type=int,
        help='Specific expert instance ID to test'
    )
    
    parser.add_argument(
        '--recommendation-ids',
        type=str,
        help='Comma-separated list of recommendation IDs to test (e.g., "123,456,789")'
    )
    
    parser.add_argument(
        '--hours',
        type=int,
        default=24,
        help='How many hours back to look for recommendations (default: 24)'
    )
    
    parser.add_argument(
        '--no-dry-run',
        action='store_true',
        help='Actually create orders (default is dry-run mode)'
    )
    
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='Show detailed evaluation information'
    )
    
    args = parser.parse_args()
    
    # Parse recommendation IDs if provided
    recommendation_ids = None
    if args.recommendation_ids:
        try:
            recommendation_ids = [int(x.strip()) for x in args.recommendation_ids.split(',')]
        except ValueError:
            print("❌ Error: Invalid recommendation IDs format. Use comma-separated numbers.")
            return 1
    
    # Create tester
    dry_run = not args.no_dry_run
    tester = RulesetTester(dry_run=dry_run, verbose=args.verbose)
    
    print("╔" + "═"*78 + "╗")
    print("║" + " "*25 + "RULESET EVALUATION TEST" + " "*30 + "║")
    print("╚" + "═"*78 + "╝")
    print(f"\nMode: {'🔒 DRY RUN (no orders will be created)' if dry_run else '⚠️  LIVE MODE (orders will be created!)'}")
    
    # Get experts to test
    if args.expert_id:
        expert_ids = [args.expert_id]
    else:
        # Test all experts
        experts = get_all_instances(ExpertInstance)
        expert_ids = [e.id for e in experts if e.enabled]
        print(f"\nTesting all {len(expert_ids)} enabled experts")
    
    # Test each expert
    all_results = []
    for expert_id in expert_ids:
        try:
            result = tester.test_expert_recommendations(
                expert_id,
                hours=args.hours,
                recommendation_ids=recommendation_ids
            )
            all_results.append(result)
        except Exception as e:
            print(f"\n❌ Error testing expert {expert_id}: {e}")
            logger.error(f"Error testing expert {expert_id}: {e}", exc_info=True)
    
    # Overall summary
    if len(all_results) > 1:
        print(f"\n{'='*80}")
        print(f"OVERALL SUMMARY - {len(all_results)} Experts Tested")
        print(f"{'='*80}")
        
        total_recommendations = sum(r.get('recommendations', 0) for r in all_results)
        total_passed = sum(r.get('passed', 0) for r in all_results)
        total_failed = sum(r.get('failed', 0) for r in all_results)
        
        print(f"Total Recommendations: {total_recommendations}")
        print(f"✅ Total Passed: {total_passed}")
        print(f"❌ Total Failed: {total_failed}")
        
        if total_recommendations > 0:
            print(f"Overall Success Rate: {(total_passed/total_recommendations*100):.1f}%")
    
    print(f"\n{'='*80}")
    print("Test completed!")
    print(f"{'='*80}\n")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
