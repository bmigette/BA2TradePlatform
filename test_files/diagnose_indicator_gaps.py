"""
Comprehensive Diagnostic Script for Indicator Data Gaps

This script investigates why technical indicators show gaps in the UI charts by:
1. Examining the database for missing data points
2. Analyzing the stored indicator data structure
3. Comparing with price data to identify mismatches
4. Testing indicator calculation to identify generation issues
5. Providing detailed report on findings

Usage:
    python test_files/diagnose_indicator_gaps.py <analysis_id>
"""

import sys
import os
import json
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, AnalysisOutput, ExpertRecommendation
from ba2_trade_platform.logger import logger
from sqlmodel import select


class IndicatorGapDiagnostic:
    """Diagnostic tool for investigating indicator data gaps."""
    
    def __init__(self, analysis_id: int):
        self.analysis_id = analysis_id
        self.market_analysis: Optional[MarketAnalysis] = None
        self.price_data: Optional[pd.DataFrame] = None
        self.indicators_data: Dict[str, pd.DataFrame] = {}
        self.analysis_outputs: List[AnalysisOutput] = []
        self.issues: List[Dict[str, Any]] = []
        
    def run_diagnostics(self) -> Dict[str, Any]:
        """Run full diagnostic suite."""
        print(f"\n{'='*80}")
        print(f"INDICATOR GAP DIAGNOSTIC - Analysis #{self.analysis_id}")
        print(f"{'='*80}\n")
        
        # Step 1: Load analysis from database
        if not self._load_analysis():
            return {"error": "Could not load analysis"}
        
        # Step 2: Load price data
        self._load_price_data()
        
        # Step 3: Load indicator data from database
        self._load_indicators_from_db()
        
        # Step 4: Analyze data completeness
        self._analyze_data_completeness()
        
        # Step 5: Check for date mismatches
        self._check_date_alignment()
        
        # Step 6: Test live recalculation
        self._test_live_recalculation()
        
        # Step 7: Generate report
        return self._generate_report()
    
    def _load_analysis(self) -> bool:
        """Load the MarketAnalysis from database."""
        print("📋 STEP 1: Loading MarketAnalysis from database...")
        
        session = get_db()
        try:
            self.market_analysis = session.get(MarketAnalysis, self.analysis_id)
            if not self.market_analysis:
                print(f"❌ ERROR: Analysis {self.analysis_id} not found in database")
                return False
            
            print(f"✅ Loaded Analysis #{self.analysis_id}")
            print(f"   Symbol: {self.market_analysis.symbol}")
            print(f"   Status: {self.market_analysis.status}")
            print(f"   Created: {self.market_analysis.created_at}")
            
            # Load all analysis outputs
            statement = select(AnalysisOutput).where(
                AnalysisOutput.market_analysis_id == self.analysis_id
            )
            self.analysis_outputs = list(session.exec(statement).all())
            print(f"   Total outputs: {len(self.analysis_outputs)}")
            
            return True
        finally:
            session.close()
    
    def _load_price_data(self):
        """Load OHLCV price data from database."""
        print("\n📈 STEP 2: Loading price data from database...")
        
        # Look for OHLCV data output
        ohlcv_outputs = [
            o for o in self.analysis_outputs 
            if 'ohlcv' in o.name.lower() and o.name.endswith('_json')
        ]
        
        if not ohlcv_outputs:
            print("❌ No OHLCV data found in database")
            self.issues.append({
                "severity": "critical",
                "type": "missing_price_data",
                "message": "No OHLCV price data found in database"
            })
            return
        
        print(f"✅ Found {len(ohlcv_outputs)} OHLCV output(s)")
        
        # Use the first one
        ohlcv_output = ohlcv_outputs[0]
        print(f"   Output name: {ohlcv_output.name}")
        
        try:
            data = json.loads(ohlcv_output.text)
            
            # Check data structure - could be list format or dict format
            ohlcv_data = data.get('data', {})
            
            # Handle nested data (from format_type="dict")
            if isinstance(ohlcv_data, dict) and 'data' in ohlcv_data:
                ohlcv_data = ohlcv_data['data']
            
            # Convert to DataFrame
            if isinstance(ohlcv_data, list) and len(ohlcv_data) > 0:
                # List format: [{"date": ..., "open": ..., ...}, ...]
                if isinstance(ohlcv_data[0], dict) and 'date' in ohlcv_data[0]:
                    df = pd.DataFrame(ohlcv_data)
                    df['date'] = pd.to_datetime(df['date'])
                    df.set_index('date', inplace=True)
                    df.index.name = 'Date'
                    self.price_data = df[['open', 'high', 'low', 'close', 'volume']].copy()
                    self.price_data.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                else:
                    print("❌ Invalid list format (missing 'date' key)")
                    self.issues.append({
                        "severity": "critical",
                        "type": "invalid_price_data",
                        "message": "OHLCV list format invalid"
                    })
                    return
            elif isinstance(ohlcv_data, dict) and 'dates' in ohlcv_data and 'open' in ohlcv_data:
                # Dict format: {"dates": [...], "open": [...], ...}
                dates = pd.to_datetime(ohlcv_data['dates'])
                self.price_data = pd.DataFrame({
                    'Open': ohlcv_data.get('open', []),
                    'High': ohlcv_data.get('high', []),
                    'Low': ohlcv_data.get('low', []),
                    'Close': ohlcv_data.get('close', []),
                    'Volume': ohlcv_data.get('volume', [])
                }, index=dates)
                self.price_data.index.name = 'Date'
            else:
                print(f"❌ Unrecognized OHLCV data format: {type(ohlcv_data)}")
                self.issues.append({
                    "severity": "critical",
                    "type": "invalid_price_data",
                    "message": f"OHLCV data format not recognized: {type(ohlcv_data)}"
                })
                return
            
            # Successfully loaded price data
            print(f"✅ Loaded price data: {len(self.price_data)} rows")
            print(f"   Date range: {self.price_data.index.min()} to {self.price_data.index.max()}")
            print(f"   Columns: {list(self.price_data.columns)}")
            
            # Check for missing dates (weekends/holidays excluded)
            date_diffs = self.price_data.index.to_series().diff()
            large_gaps = date_diffs[date_diffs > pd.Timedelta(days=4)]  # More than 4 days
            if len(large_gaps) > 0:
                print(f"⚠️  Found {len(large_gaps)} large gaps in price data:")
                for gap_date, gap_size in large_gaps.items():
                    print(f"      {gap_date}: {gap_size}")
                self.issues.append({
                    "severity": "warning",
                    "type": "price_data_gaps",
                    "message": f"Found {len(large_gaps)} large gaps in price data",
                    "details": {str(k): str(v) for k, v in large_gaps.items()}
                })
        except Exception as e:
            print(f"❌ Error loading price data: {e}")
            logger.error(f"Error loading price data: {e}")
            self.issues.append({
                "severity": "critical",
                "type": "price_data_error",
                "message": f"Error loading price data: {e}"
            })
    
    def _load_indicators_from_db(self):
        """Load indicator data from database."""
        print("\n📊 STEP 3: Loading indicator data from database...")
        
        # Look for indicator outputs
        indicator_outputs = [
            o for o in self.analysis_outputs
            if ('indicator_data' in o.name.lower() or 'stockstats' in o.name.lower()) 
            and o.name.endswith('_json')
        ]
        
        if not indicator_outputs:
            print("❌ No indicator data found in database")
            self.issues.append({
                "severity": "critical",
                "type": "missing_indicators",
                "message": "No indicator data found in database"
            })
            return
        
        print(f"✅ Found {len(indicator_outputs)} indicator output(s)")
        
        for output in indicator_outputs:
            print(f"\n   Processing: {output.name}")
            
            try:
                params = json.loads(output.text)
                
                # Check tool type
                tool_type = params.get('tool', 'unknown')
                print(f"      Tool type: {tool_type}")
                
                if tool_type == 'get_indicator_data':
                    indicator_name = params.get('indicator', 'Unknown')
                    indicator_data = params.get('data', {})
                    
                    print(f"      Indicator: {indicator_name}")
                    print(f"      Data type: {type(indicator_data)}")
                    
                    if isinstance(indicator_data, dict):
                        if 'dates' in indicator_data and 'values' in indicator_data:
                            dates = pd.to_datetime(indicator_data['dates'])
                            # Make timezone-aware to match price data
                            if dates.tz is None:
                                dates = dates.tz_localize('UTC')
                            display_name = indicator_name.replace('_', ' ').title()
                            
                            indicator_df = pd.DataFrame({
                                display_name: indicator_data['values']
                            }, index=dates)
                            indicator_df.index.name = 'Date'
                            
                            self.indicators_data[display_name] = indicator_df
                            
                            print(f"      ✅ Loaded {len(indicator_df)} data points")
                            print(f"         Date range: {indicator_df.index.min()} to {indicator_df.index.max()}")
                            
                            # Check for gaps in this indicator
                            self._check_indicator_gaps(display_name, indicator_df)
                        else:
                            print(f"      ❌ Invalid data structure: {list(indicator_data.keys())}")
                            self.issues.append({
                                "severity": "error",
                                "type": "invalid_indicator_structure",
                                "indicator": indicator_name,
                                "message": f"Indicator data missing 'dates' or 'values' keys",
                                "details": {"keys": list(indicator_data.keys())}
                            })
                    else:
                        print(f"      ❌ Data is not a dict: {type(indicator_data)}")
                        self.issues.append({
                            "severity": "error",
                            "type": "invalid_indicator_type",
                            "indicator": indicator_name,
                            "message": f"Indicator data is {type(indicator_data)}, expected dict"
                        })
                
                elif tool_type == 'get_stock_stats_indicators_window':
                    indicator_name = params.get('indicator', 'Unknown')
                    print(f"      Indicator: {indicator_name} (old format)")
                    print(f"      ℹ️  Old format detected - would need live recalculation")
                    self.issues.append({
                        "severity": "info",
                        "type": "old_format_indicator",
                        "indicator": indicator_name,
                        "message": "Indicator using old format - requires live recalculation"
                    })
                else:
                    print(f"      ⚠️  Unknown tool type: {tool_type}")
                    
            except json.JSONDecodeError as e:
                print(f"      ❌ JSON decode error: {e}")
                self.issues.append({
                    "severity": "error",
                    "type": "json_decode_error",
                    "output_name": output.name,
                    "message": f"Could not parse JSON: {e}"
                })
            except Exception as e:
                print(f"      ❌ Error: {e}")
                logger.error(f"Error loading indicator {output.name}: {e}")
                self.issues.append({
                    "severity": "error",
                    "type": "indicator_load_error",
                    "output_name": output.name,
                    "message": f"Error loading indicator: {e}"
                })
        
        print(f"\n   Total indicators loaded: {len(self.indicators_data)}")
    
    def _check_indicator_gaps(self, indicator_name: str, indicator_df: pd.DataFrame):
        """Check for gaps/NaN values in indicator data."""
        # Check for NaN values
        nan_count = indicator_df[indicator_name].isna().sum()
        total_count = len(indicator_df)
        
        if nan_count > 0:
            print(f"         ⚠️  {nan_count}/{total_count} NaN values ({nan_count/total_count*100:.1f}%)")
            self.issues.append({
                "severity": "warning",
                "type": "indicator_nan_values",
                "indicator": indicator_name,
                "message": f"{nan_count}/{total_count} NaN values in indicator data",
                "nan_percentage": nan_count/total_count*100
            })
            
            # Find consecutive NaN groups
            is_nan = indicator_df[indicator_name].isna()
            nan_groups = is_nan.ne(is_nan.shift()).cumsum()[is_nan]
            nan_group_sizes = nan_groups.value_counts().sort_index()
            
            if len(nan_group_sizes) > 0:
                print(f"         NaN groups: {len(nan_group_sizes)} gaps")
                for group_id, size in nan_group_sizes.head(5).items():
                    nan_indices = indicator_df[nan_groups == group_id].index
                    print(f"           - Gap of {size} values starting at {nan_indices.min()}")
        
        # Check for date gaps (missing dates compared to expected sequence)
        date_diffs = indicator_df.index.to_series().diff()
        large_gaps = date_diffs[date_diffs > pd.Timedelta(days=4)]  # More than 4 days
        
        if len(large_gaps) > 0:
            print(f"         ⚠️  {len(large_gaps)} large date gaps")
            self.issues.append({
                "severity": "warning",
                "type": "indicator_date_gaps",
                "indicator": indicator_name,
                "message": f"{len(large_gaps)} large gaps in date sequence",
                "details": {str(k): str(v) for k, v in large_gaps.items()}
            })
    
    def _analyze_data_completeness(self):
        """Analyze overall data completeness."""
        print("\n🔍 STEP 4: Analyzing data completeness...")
        
        if self.price_data is None:
            print("⏭️  Skipping (no price data)")
            return
        
        if not self.indicators_data:
            print("⏭️  Skipping (no indicator data)")
            return
        
        # Compare date ranges
        price_start = self.price_data.index.min()
        price_end = self.price_data.index.max()
        price_count = len(self.price_data)
        
        print(f"\n   Price Data:")
        print(f"      Start: {price_start}")
        print(f"      End: {price_end}")
        print(f"      Count: {price_count}")
        
        for indicator_name, indicator_df in self.indicators_data.items():
            ind_start = indicator_df.index.min()
            ind_end = indicator_df.index.max()
            ind_count = len(indicator_df)
            
            print(f"\n   {indicator_name}:")
            print(f"      Start: {ind_start}")
            print(f"      End: {ind_end}")
            print(f"      Count: {ind_count}")
            
            # Check alignment
            if ind_start != price_start:
                print(f"      ⚠️  Start date mismatch: {(ind_start - price_start).days} days difference")
                self.issues.append({
                    "severity": "warning",
                    "type": "start_date_mismatch",
                    "indicator": indicator_name,
                    "message": f"Start date differs from price data by {(ind_start - price_start).days} days"
                })
            
            if ind_end != price_end:
                print(f"      ⚠️  End date mismatch: {(ind_end - price_end).days} days difference")
                self.issues.append({
                    "severity": "warning",
                    "type": "end_date_mismatch",
                    "indicator": indicator_name,
                    "message": f"End date differs from price data by {(ind_end - price_end).days} days"
                })
            
            if ind_count != price_count:
                print(f"      ⚠️  Count mismatch: {ind_count} vs {price_count} (diff: {ind_count - price_count})")
                self.issues.append({
                    "severity": "warning",
                    "type": "count_mismatch",
                    "indicator": indicator_name,
                    "message": f"Indicator has {ind_count} points vs {price_count} price points"
                })
    
    def _check_date_alignment(self):
        """Check if indicator dates align with price dates."""
        print("\n📅 STEP 5: Checking date alignment...")
        
        if self.price_data is None or not self.indicators_data:
            print("⏭️  Skipping (insufficient data)")
            return
        
        price_dates = set(self.price_data.index)
        
        for indicator_name, indicator_df in self.indicators_data.items():
            indicator_dates = set(indicator_df.index)
            
            # Find dates in price data but not in indicator
            missing_in_indicator = price_dates - indicator_dates
            # Find dates in indicator but not in price data
            extra_in_indicator = indicator_dates - price_dates
            
            if missing_in_indicator:
                print(f"\n   {indicator_name}:")
                print(f"      ⚠️  {len(missing_in_indicator)} dates in price data but not in indicator")
                print(f"         Examples: {sorted(list(missing_in_indicator))[:5]}")
                self.issues.append({
                    "severity": "warning",
                    "type": "missing_indicator_dates",
                    "indicator": indicator_name,
                    "message": f"{len(missing_in_indicator)} price dates missing from indicator",
                    "count": len(missing_in_indicator)
                })
            
            if extra_in_indicator:
                print(f"      ⚠️  {len(extra_in_indicator)} dates in indicator but not in price data")
                print(f"         Examples: {sorted(list(extra_in_indicator))[:5]}")
                self.issues.append({
                    "severity": "info",
                    "type": "extra_indicator_dates",
                    "indicator": indicator_name,
                    "message": f"{len(extra_in_indicator)} indicator dates not in price data",
                    "count": len(extra_in_indicator)
                })
            
            if not missing_in_indicator and not extra_in_indicator:
                print(f"\n   {indicator_name}: ✅ Perfect alignment")
    
    def _test_live_recalculation(self):
        """Test live indicator recalculation."""
        print("\n🔄 STEP 6: Testing live indicator recalculation...")
        
        if not self.market_analysis:
            print("⏭️  Skipping (no analysis loaded)")
            return
        
        # Only test if we have stored indicators
        if not self.indicators_data:
            print("⏭️  Skipping (no stored indicators to compare against)")
            return
        
        try:
            from ba2_trade_platform.modules.dataproviders.indicators.PandasIndicatorCalc import PandasIndicatorCalc
            from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider
            
            # PandasIndicatorCalc requires an OHLCV provider
            ohlcv_provider = YFinanceDataProvider()
            calculator = PandasIndicatorCalc(ohlcv_provider)
            
            # Test one indicator
            test_indicator = list(self.indicators_data.keys())[0]
            stored_df = self.indicators_data[test_indicator]
            
            print(f"\n   Testing: {test_indicator}")
            print(f"   Stored data: {len(stored_df)} points")
            
            # Get date range from stored data
            start_date = stored_df.index.min().strftime('%Y-%m-%d')
            end_date = stored_df.index.max().strftime('%Y-%m-%d')
            
            # Convert display name back to indicator name
            indicator_key = test_indicator.lower().replace(' ', '_')
            
            print(f"   Recalculating {indicator_key} for {start_date} to {end_date}...")
            
            result = calculator.get_indicator_data(
                symbol=self.market_analysis.symbol,
                indicator=indicator_key,
                start_date=start_date,
                end_date=end_date,
                interval='1d',
                format_type="dict"
            )
            
            if isinstance(result, dict) and 'dates' in result and 'values' in result:
                dates = pd.to_datetime(result['dates'])
                recalc_df = pd.DataFrame({
                    test_indicator: result['values']
                }, index=dates)
                
                print(f"   ✅ Recalculated: {len(recalc_df)} points")
                
                # Compare
                common_dates = stored_df.index.intersection(recalc_df.index)
                print(f"   Common dates: {len(common_dates)}")
                
                if len(common_dates) > 0:
                    stored_values = stored_df.loc[common_dates, test_indicator]
                    recalc_values = recalc_df.loc[common_dates, test_indicator]
                    
                    # Check for differences
                    diff = (stored_values - recalc_values).abs()
                    max_diff = diff.max()
                    mean_diff = diff.mean()
                    
                    print(f"   Value comparison:")
                    print(f"      Max difference: {max_diff}")
                    print(f"      Mean difference: {mean_diff}")
                    
                    if max_diff > 0.01:  # Significant difference
                        print(f"      ⚠️  Significant differences detected!")
                        self.issues.append({
                            "severity": "warning",
                            "type": "recalc_mismatch",
                            "indicator": test_indicator,
                            "message": "Live recalculation differs from stored values",
                            "max_diff": float(max_diff),
                            "mean_diff": float(mean_diff)
                        })
                    else:
                        print(f"      ✅ Values match (within 0.01 tolerance)")
                
                # Check for gaps in recalculated data
                nan_count = recalc_df[test_indicator].isna().sum()
                if nan_count > 0:
                    print(f"      ⚠️  Recalculated data has {nan_count} NaN values!")
                    self.issues.append({
                        "severity": "error",
                        "type": "recalc_has_gaps",
                        "indicator": test_indicator,
                        "message": f"Live recalculation produced {nan_count} NaN values",
                        "nan_count": nan_count
                    })
                else:
                    print(f"      ✅ No NaN values in recalculated data")
            else:
                print(f"   ❌ Invalid result format from calculator")
                self.issues.append({
                    "severity": "error",
                    "type": "recalc_invalid_format",
                    "indicator": test_indicator,
                    "message": "Calculator returned invalid format"
                })
                
        except Exception as e:
            print(f"   ❌ Error during recalculation: {e}")
            logger.error(f"Live recalculation test failed: {e}")
            self.issues.append({
                "severity": "error",
                "type": "recalc_error",
                "message": f"Live recalculation failed: {e}"
            })
    
    def _generate_report(self) -> Dict[str, Any]:
        """Generate final diagnostic report."""
        print(f"\n{'='*80}")
        print("DIAGNOSTIC REPORT")
        print(f"{'='*80}\n")
        
        # Count issues by severity
        critical = [i for i in self.issues if i['severity'] == 'critical']
        errors = [i for i in self.issues if i['severity'] == 'error']
        warnings = [i for i in self.issues if i['severity'] == 'warning']
        info = [i for i in self.issues if i['severity'] == 'info']
        
        print(f"Summary:")
        print(f"   🔴 Critical: {len(critical)}")
        print(f"   ❌ Errors: {len(errors)}")
        print(f"   ⚠️  Warnings: {len(warnings)}")
        print(f"   ℹ️  Info: {len(info)}")
        
        if critical:
            print(f"\n🔴 CRITICAL ISSUES:")
            for issue in critical:
                print(f"   - {issue['message']}")
        
        if errors:
            print(f"\n❌ ERRORS:")
            for issue in errors:
                indicator = f" ({issue['indicator']})" if 'indicator' in issue else ""
                print(f"   - {issue['message']}{indicator}")
        
        if warnings:
            print(f"\n⚠️  WARNINGS:")
            for issue in warnings:
                indicator = f" ({issue['indicator']})" if 'indicator' in issue else ""
                print(f"   - {issue['message']}{indicator}")
        
        # Root cause analysis
        print(f"\n{'='*80}")
        print("ROOT CAUSE ANALYSIS")
        print(f"{'='*80}\n")
        
        has_nan_values = any(i['type'] == 'indicator_nan_values' for i in self.issues)
        has_date_gaps = any(i['type'] == 'indicator_date_gaps' for i in self.issues)
        has_missing_dates = any(i['type'] == 'missing_indicator_dates' for i in self.issues)
        has_recalc_gaps = any(i['type'] == 'recalc_has_gaps' for i in self.issues)
        
        if has_nan_values:
            print("📌 FINDING: Indicator data contains NaN values")
            print("   This is the PRIMARY cause of gaps in the chart.")
            print("   NaN values appear as missing lines/points in the visualization.")
            
            if has_recalc_gaps:
                print("\n   💡 CRITICAL: Live recalculation ALSO produces NaN values!")
                print("      → This is a calculation/data source issue, NOT a storage issue")
                print("      → The indicator calculator is producing incomplete data")
                print("      → Need to investigate indicator calculation logic")
            else:
                print("\n   💡 Live recalculation does NOT have NaN values")
                print("      → This suggests stored data may be corrupted or incomplete")
                print("      → Recommendation: Use live recalculation or re-run analysis")
        
        if has_missing_dates:
            print("\n📌 FINDING: Indicator missing dates that exist in price data")
            print("   This causes visual gaps where price data exists but indicator doesn't.")
            print("   → Indicator calculation may require minimum data points (warmup period)")
            print("   → Check indicator parameters (e.g., RSI needs 14+ periods)")
        
        if has_date_gaps:
            print("\n📌 FINDING: Large gaps in indicator date sequence")
            print("   This is likely due to market holidays/weekends being handled inconsistently.")
        
        if not has_nan_values and not has_missing_dates and not has_date_gaps:
            print("✅ No significant gaps detected in stored data!")
            print("   If you're seeing gaps in the UI, it may be a rendering issue.")
        
        # Recommendations
        print(f"\n{'='*80}")
        print("RECOMMENDATIONS")
        print(f"{'='*80}\n")
        
        if has_recalc_gaps:
            print("1. 🔍 Investigate indicator calculation logic:")
            print("   - Check PandasIndicatorCalc implementation")
            print("   - Verify data provider returns complete OHLCV data")
            print("   - Check if indicator warmup periods are handled correctly")
        
        if has_nan_values and not has_recalc_gaps:
            print("2. 🔄 Re-run the analysis to regenerate indicator data")
            print("   - Current stored data appears corrupted/incomplete")
            print("   - Live recalculation produces clean data")
        
        if has_missing_dates:
            print("3. ⚙️ Review indicator parameters:")
            print("   - Some indicators need minimum periods (RSI: 14, MACD: 26)")
            print("   - Initial NaN values are expected during warmup")
            print("   - Consider using .fillna(method='ffill') for visualization")
        
        print("\n" + "="*80)
        
        return {
            "analysis_id": self.analysis_id,
            "symbol": self.market_analysis.symbol if self.market_analysis else None,
            "price_data_points": len(self.price_data) if self.price_data is not None else 0,
            "indicators_count": len(self.indicators_data),
            "issues": self.issues,
            "has_critical": len(critical) > 0,
            "has_errors": len(errors) > 0,
            "has_warnings": len(warnings) > 0
        }


def main():
    if len(sys.argv) < 2:
        print("Usage: python diagnose_indicator_gaps.py <analysis_id>")
        print("\nExample: python diagnose_indicator_gaps.py 9710")
        sys.exit(1)
    
    try:
        analysis_id = int(sys.argv[1])
    except ValueError:
        print(f"Error: Invalid analysis ID '{sys.argv[1]}'. Must be an integer.")
        sys.exit(1)
    
    diagnostic = IndicatorGapDiagnostic(analysis_id)
    report = diagnostic.run_diagnostics()
    
    # Save report to file
    output_file = f"test_files/diagnostic_report_{analysis_id}.json"
    with open(output_file, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    print(f"\n📄 Full report saved to: {output_file}\n")


if __name__ == "__main__":
    main()
