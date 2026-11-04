"""Every /api/v1 route has a tool; every tool maps to a real route. A new API route without a tool fails here."""
import asyncio
import inspect
import re

import pytest

from tests import mcp_server_support  # noqa: F401

from ba2_mcp import tools  # noqa: E402
from ba2_trade_platform.api.app import create_v1_app  # noqa: E402  (TEST-ONLY import)

pytestmark = pytest.mark.api_db


def _routes():
    schema = create_v1_app().openapi()
    return {(m.upper(), p) for p, ops in schema["paths"].items() for m in ops
            if m in ("get", "post", "patch", "put", "delete")}


def test_every_route_has_a_tool_or_a_listed_reason():
    routes = _routes()
    covered = {(s.method, s.path) for s in tools.REGISTRY.values()}
    missing = routes - covered - set(tools.INTENTIONALLY_UNEXPOSED)
    assert not missing, f"API routes without an MCP tool: {sorted(missing)}"
    stale = set(tools.INTENTIONALLY_UNEXPOSED) - routes
    assert not stale, f"INTENTIONALLY_UNEXPOSED names routes that do not exist: {stale}"
    assert all(tools.INTENTIONALLY_UNEXPOSED.values())


def test_every_tool_maps_to_an_existing_route_and_only_listed_routes_are_shared():
    routes = _routes()
    for s in tools.REGISTRY.values():
        assert (s.method, s.path) in routes, f"{s.name} maps to a route that does not exist: {s.method} {s.path}"
    counts = {}
    for s in tools.REGISTRY.values():
        counts.setdefault((s.method, s.path), []).append(s.name)
    shared = {k for k, v in counts.items() if len(v) > 1}
    assert shared <= tools.SHARED_ROUTES, shared


def test_write_methods_have_confirm_and_reads_do_not():
    for s in tools.REGISTRY.values():
        has_confirm = "confirm" in inspect.signature(s.func).parameters
        assert has_confirm == (s.method != "GET"), s.name
        assert s.write == (s.method != "GET"), s.name


def test_path_placeholders_are_tool_parameters_and_tools_are_documented():
    for s in tools.REGISTRY.values():
        params = inspect.signature(s.func).parameters
        for ph in re.findall(r"{(\w+)}", s.path):
            assert ph in params, (s.name, ph)
        assert s.func.__doc__ and len(s.func.__doc__) > 40, s.name
    listed = {t.name for t in asyncio.run(tools.mcp.list_tools())}
    assert listed == set(tools.REGISTRY)


def test_requested_tool_names_exist():
    wanted = """list_accounts get_account get_account_positions get_broker_orders list_transactions get_transaction
    list_orders list_experts get_expert list_market_analyses get_market_analysis list_recommendations
    list_risk_manager_runs get_risk_manager_run list_activity get_overview_stats get_account_growth
    get_growth_by_label get_growth_by_position get_closed_pnl get_dividends get_portfolio list_portfolio_runs
    set_take_profit_and_stop_loss unlock_take_profit_and_stop_loss close_transaction cancel_order enable_expert
    disable_expert set_expert_virtual_equity_pct update_expert_settings run_expert_analysis_now
    reload_platform_caches""".split()
    assert not set(wanted) - set(tools.REGISTRY)


def test_mcp_server_imports_no_ba2_package():
    """The MCP server only speaks HTTP: no ba2_trade_platform / ba2_common / ba2_providers / ba2_experts import."""
    from pathlib import Path
    root = Path(mcp_server_support._MCP_DIR) / "ba2_mcp"
    pattern = re.compile(r"^\s*(from|import)\s+ba2_(trade_platform|common|providers|experts)\b", re.M)
    for f in root.glob("*.py"):
        assert not pattern.search(f.read_text(encoding="utf-8")), f
