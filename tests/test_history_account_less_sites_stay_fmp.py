"""Account-less OHLCV sites stay on FMP (plan 2026-10-24-history-source-task11, Task 9; owner decision).

These sites have no account in scope (UI pages, the once-a-day market-regime read), so the per-account
``ohlcv_history_source`` does not apply to them. Pinned at source level so a later "resolve everywhere" refactor has to
decide on purpose: change the expected call here WITH the reason.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def calls_naming(tree, func, first_args):
    """Call nodes ``func(*first_args)`` with string-constant leading arguments."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name == func and len(node.args) >= len(first_args) and all(
                    isinstance(a, ast.Constant) and a.value == v for a, v in zip(node.args, first_args)):
                out.append(node)
    return out


def test_symbol360_reads_fmp_history():
    # Symbol 360 is an operator page, not an account's decision: FMP, whatever any account is set to.
    tree = ast.parse(source("ba2_trade_platform/ui/pages/symbol360.py"))
    assert len(calls_naming(tree, "get_provider", ["ohlcv", "fmp"])) >= 2
    assert not calls_naming(tree, "get_provider", ["ohlcv", "alpaca_hist"])


def test_option_structure_chart_reads_fmp_history():
    tree = ast.parse(source("ba2_trade_platform/ui/components/option_structure_chart.py"))
    assert len(calls_naming(tree, "get_provider", ["ohlcv", "fmp"])) == 1


def test_trading_agents_ui_reads_fmp_history():
    text = source("ba2_trade_platform/modules/experts/TradingAgentsUI.py")
    assert re.search(r"return FMPOHLCVProvider\(\), None", text)
    assert "alpaca_hist" not in text


def test_market_regime_refresh_reads_fmp_spy():
    # Global, once per day per process, no account in scope; the SPY regime gate is FMP by owner decision 2026-10-24
    # (same source as the stock price). The history-source setting is a different setting and does not apply.
    tree = ast.parse(source("ba2_trade_platform/core/TradeManager.py"))
    assert calls_naming(tree, "get_provider", ["ohlcv", "fmp"]), "the SPY read moved off fmp: decide on purpose"
    assert not calls_naming(tree, "get_provider", ["ohlcv", "alpaca_hist"])


def test_spy_regime_gate_branch_still_names_fmp_if_merged():
    # feat/spy-regime-gate adds TradeManager._fetch_benchmark_daily; once merged it must still read fmp.
    from ba2_trade_platform.core import TradeManager as TM
    if not hasattr(TM.TradeManager, "_fetch_benchmark_daily") and not hasattr(TM, "_fetch_benchmark_daily"):
        pytest.skip("feat/spy-regime-gate is not merged here")
    import inspect
    fn = getattr(TM.TradeManager, "_fetch_benchmark_daily", None) or TM._fetch_benchmark_daily
    tree = ast.parse(inspect.getsource(fn).replace("\n    ", "\n").lstrip())
    assert calls_naming(tree, "get_provider", ["ohlcv", "fmp"])


def test_the_per_account_resolver_is_not_used_by_any_account_less_site():
    for rel in ("ba2_trade_platform/ui/pages/symbol360.py", "ba2_trade_platform/ui/components/option_structure_chart.py",
                "ba2_trade_platform/modules/experts/TradingAgentsUI.py", "ba2_trade_platform/core/TradeManager.py"):
        assert "account_history" not in source(rel), rel
