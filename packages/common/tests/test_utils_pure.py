"""Task 5 keystone tests — `ba2_common.core.utils` must be the PURE subset.

Importing it must not pull any provider/expert/account/live-platform package, and
the three instance-factory functions (which depended on the live registries) must
be gone. Plus a couple of pure-helper contract checks reconciled to the real
source signatures.
"""
import subprocess
import sys
import textwrap

PY = sys.executable


def _assert_no_leak(import_stmt, forbidden):
    """Amendment A1 leak gate: in a subprocess, importing must not PULL a
    forbidden module into sys.modules (forbidden pkgs ARE installed in this venv,
    so 'not installed' is not a valid proxy)."""
    code = textwrap.dedent(f"""
        import sys
        {import_stmt}
        bad = [m for m in {forbidden!r}
               if any(k == m or k.startswith(m + '.') for k in sys.modules)]
        print('LEAK:' + ','.join(bad) if bad else 'CLEAN')
    """)
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "CLEAN", (
        f"{import_stmt!r} pulled {out.stdout.strip()} / stderr={out.stderr}"
    )


def test_utils_import_pulls_no_experts_accounts_or_live_platform():
    """The keystone: importing common utils must NOT pull any
    provider/expert/account/live-platform package (proven via sys.modules)."""
    _assert_no_leak(
        "import ba2_common.core.utils",
        ["ba2_providers", "ba2_experts", "ba2_trade_platform",
         "langchain", "langchain_core", "fmpsdk", "nicegui"],
    )


def test_instance_factory_funcs_moved_to_live_registry():
    """The three id->instance factories stayed in the live host; they must not
    be present on the pure ba2_common utils module."""
    import ba2_common.core.utils as u
    assert not hasattr(u, "get_expert_instance_from_id")
    assert not hasattr(u, "get_account_instance_from_id")
    assert not hasattr(u, "get_account_instance_from_transaction")


def test_pure_subset_functions_present():
    """All 24 pure-subset helpers named in the plan's split list are present."""
    import ba2_common.core.utils as u
    expected = [
        "get_labels_by_symbol", "get_all_instrument_labels",
        "add_label_to_instruments", "remove_label_from_instruments",
        "expert_uses_risk_manager", "expert_schedules_open_positions",
        "get_market_analysis_id_from_order_id",
        "has_existing_transactions_for_expert_and_symbol",
        "get_latest_recommendation_id_for_symbol",
        "get_account_id_for_recommendation", "calculate_transaction_pnl",
        "close_transaction_with_logging", "log_close_order_activity",
        "log_transaction_created_activity", "log_trade_action_activity",
        "get_risk_manager_mode", "get_order_status_color",
        "log_analysis_batch_start", "log_analysis_batch_end",
        "log_manual_analysis", "parse_fmp_amount_range",
        "calculate_fmp_trade_metrics", "get_setting_safe",
        "get_expert_options_for_ui",
    ]
    for name in expected:
        assert callable(getattr(u, name, None)), f"missing pure helper: {name}"


def test_parse_fmp_amount_range():
    """Reconciled to the REAL source: returns a single float — the midpoint for a
    range like '$1,001 - $15,000' ((1001+15000)/2 == 8000.5), the value for a
    single amount, and 0.0 for empty/invalid input (NOT a (lo, hi) tuple)."""
    from ba2_common.core.utils import parse_fmp_amount_range
    assert parse_fmp_amount_range("$1,001 - $15,000") == 8000.5
    assert parse_fmp_amount_range("$15,001 - $50,000") == 32500.5
    assert parse_fmp_amount_range("$1,000") == 1000.0
    assert parse_fmp_amount_range("") == 0.0
    assert parse_fmp_amount_range(None) == 0.0
    assert parse_fmp_amount_range("garbage") == 0.0


def test_calculate_fmp_trade_metrics_smoke():
    from ba2_common.core.utils import calculate_fmp_trade_metrics
    assert callable(calculate_fmp_trade_metrics)
    # Empty input returns the well-formed zero-metrics dict.
    empty = calculate_fmp_trade_metrics([])
    assert empty["num_trades"] == 0
    assert empty["total_money_spent"] == 0.0
    # Known ranges sum their midpoints.
    metrics = calculate_fmp_trade_metrics([
        {"amount": "$1,000,001 - $5,000,000", "transactionDate": "2025-10-30"},
        {"amount": "$500,001 - $1,000,000", "transactionDate": "2025-10-25"},
    ])
    assert metrics["num_trades"] == 2
    assert metrics["total_money_spent"] == 3_000_000 + 750_000


def test_get_setting_safe_pure():
    from ba2_common.core.utils import get_setting_safe
    assert get_setting_safe({"x": None}, "x", 30, int) == 30
    assert get_setting_safe({"x": "50"}, "x", 30, int) == 50
    assert get_setting_safe({}, "x", 7) == 7
