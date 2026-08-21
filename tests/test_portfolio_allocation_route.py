"""Portfolio Allocation is reachable: a sidebar entry and a '/portfolioallocation' route.

`ba2_trade_platform.ui.main` and `ba2_trade_platform.ui.pages` are deliberately NOT
imported: both pull every page module (and through them the expert/LLM stack), an
import that does not complete in minutes. `ui.menus` is cheap (nicegui + svg only)
and is imported for real; the route and the page's entry point are asserted
structurally by parsing the source with `ast`.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "ba2_trade_platform" / "ui" / "main.py"
PAGE_PY = REPO_ROOT / "ba2_trade_platform" / "ui" / "pages" / "portfolio_allocation.py"


def _decorated_routes(source: str):
    """[(route_path, function_name)] for every @ui.page('...')-decorated function."""
    routes = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "page"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                routes.append((dec.args[0].value, node.name))
    return routes


def _toplevel_function_names(source: str):
    tree = ast.parse(source)
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_sidebar_menu_has_portfolio_allocation_entry():
    from ba2_trade_platform.ui.menus import MENU_ITEMS
    entry = next((i for i in MENU_ITEMS if i["route"] == "/portfolioallocation"), None)
    assert entry is not None, f"routes present: {[i['route'] for i in MENU_ITEMS]}"
    assert entry["icon"] == "pie_chart"
    assert entry["label"] == "Portfolio Allocation"


def test_sidebar_menu_keeps_every_pre_existing_entry():
    from ba2_trade_platform.ui.menus import MENU_ITEMS
    routes = {i["route"] for i in MENU_ITEMS}
    assert {"/", "/marketanalysis", "/activitymonitor",
            "/livetrades", "/tools", "/settings"} <= routes


def test_main_registers_the_portfolio_allocation_route():
    routes = dict(_decorated_routes(MAIN_PY.read_text(encoding="utf-8")))
    assert "/portfolioallocation" in routes, f"registered routes: {sorted(routes)}"


def test_portfolio_allocation_page_module_exposes_content():
    names = _toplevel_function_names(PAGE_PY.read_text(encoding="utf-8"))
    assert "content" in names, f"top-level functions: {sorted(names)}"
