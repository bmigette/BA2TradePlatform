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


# ---------------------------------------------------------------------------
# The page -> wizard glue. Structural: importing the page module pulls the whole
# expert/LLM stack, which does not finish inside five minutes.
# ---------------------------------------------------------------------------

def _page_source() -> str:
    return PAGE_PY.read_text(encoding="utf-8")


def test_the_page_defines_every_wizard_handler():
    """Without these the wizard has no caller at all: no Allocate button, no dry
    run, no submit, and the market banner is never built."""
    names = _toplevel_function_names(_page_source())
    assert {"_open_allocation_flow", "_load_flow_inputs", "_solve_plan",
            "_submit_plan", "_load_income_panel"} <= names


def test_the_page_opens_the_steps_dialog_and_the_wizard():
    source = _page_source()
    assert "open_allocation_steps(" in source
    assert "open_allocation_wizard(" in source
    assert "render_income_panel(" in source
    assert "render_outcomes(" in source


def test_the_allocate_button_exists_and_calls_the_flow():
    """The entry point. Everything else in this chunk is unreachable without it."""
    source = _page_source()
    assert "'Allocate'" in source
    assert "_open_allocation_flow(" in source


def test_the_page_builds_the_market_gate_from_the_service_seam():
    source = _page_source()
    assert "fetch_market_hours(" in source
    assert "evaluate_market_gate(" in source
    # The contract's caller mapping, verbatim: an UNAVAILABLE answer is UNKNOWN, not
    # closed, and it must not be read off is_open alone.
    assert "hours is None or not hours.is_known" in source


def test_the_page_passes_an_explicit_now_to_the_pure_gate():
    """The pure function has no clock of its own, deliberately."""
    assert "datetime.now(timezone.utc)" in _page_source()


def _function_source(source: str, name: str) -> str:
    """The source of ONE function, nested ones included, located by name.

    File-wide substring assertions are the weak point of a structural test: the
    phrase they look for usually also appears in a docstring or in a neighbouring
    handler, so deleting the call they exist to protect leaves them green. Every
    assertion below is scoped to the function that has to contain the call.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"no function named {name!r}")


def test_the_page_remembers_the_fractional_choice_on_every_dry_run():
    """Persisted on the DRY RUN, not only on Submit: a user who plans, closes the
    dialog and comes back tomorrow keeps the switch they chose.

    Scoped to ``_persist_choices`` since W0 gave the switch a companion -- the
    label targets and symbol weights -- and moved both off the event loop into one
    thread hop. ``_run_dry_run`` calling it is what keeps this on the CONTINUE
    path; that half is pinned separately below.
    """
    assert "remember_fractional_choice(" in _function_source(_page_source(),
                                                             "_persist_choices")


def test_the_page_persists_the_targets_on_every_dry_run_too():
    """W0. Until this the wizard was write-only-to-memory: every ``target_pct``
    stayed at the 0.0 the label picker created it with, so there could never be a
    "last" to load. Same trigger as the fractional switch -- Continue, not Submit --
    because "last" means "what I chose to allocate with"."""
    assert "save_allocation_targets(" in _function_source(_page_source(),
                                                          "_persist_choices")


def test_the_dry_run_path_actually_calls_the_persister():
    """The two tests above only prove the CALL exists in a helper. This is the one
    that proves the helper is on the Continue path at all -- without it both would
    stay green against a helper nothing invokes."""
    assert "_save_choices(" in _function_source(_page_source(), "_run_dry_run")
    assert "_run_dry_run" in _function_source(_page_source(), "_on_dry_run")


def test_the_page_also_remembers_the_choice_when_the_switch_is_toggled():
    """The wizard's fractional switch re-solves through on_refresh, which is the
    other place the user changes their mind."""
    assert "remember_fractional_choice(" in _function_source(_page_source(), "_on_refresh")


def _to_thread_targets(source: str):
    """The first argument of every ``asyncio.to_thread(...)`` call, by name."""
    targets = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread"
                and node.args
                and isinstance(node.args[0], ast.Name)):
            targets.add(node.args[0].id)
    return targets


def test_the_page_never_blocks_the_event_loop_on_broker_io():
    """Every one of these does broker or DB IO; on the event loop they freeze the
    app for every connected client.

    Parsed rather than substring-matched: these calls wrap across lines, so
    ``"asyncio.to_thread(_load_flow_inputs" in source`` is false for correct code
    and would have to be satisfied by reformatting rather than by fixing anything.
    """
    targets = _to_thread_targets(_page_source())
    assert {"_load_flow_inputs", "_solve_plan", "_submit_plan",
            "_load_income_panel"} <= targets, sorted(targets)


def _caught_exception_names(source: str, name: str):
    """Exceptions caught DIRECTLY by one function, nested functions excluded.

    Scoping by source segment is not enough: ``_run_dry_run`` lives inside
    ``_open_allocation_flow``, so the outer function's text contains the inner
    one's handlers and deleting the outer handler leaves a text scan green.
    """
    tree = ast.parse(source)
    target = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == name)
    caught, stack = set(), list(target.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue                       # a nested function owns its own handlers
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name):
            caught.add(node.type.id)
        stack.extend(ast.iter_child_nodes(node))
    return caught


def test_the_page_reports_a_failed_position_fetch_instead_of_planning_on_a_guess():
    """``get_positions() -> None`` is a FAILED fetch, not a flat account. BOTH
    entry points have to tell them apart: opening the wizard, and every re-solve
    behind it."""
    source = _page_source()
    for handler in ("_open_allocation_flow", "_run_dry_run"):
        assert "PositionFetchFailed" in _caught_exception_names(source, handler), handler


def test_the_page_wires_the_unconsumed_run_reconcile_hook():
    """D3: with a quarter of the book on whole shares, runs will regularly finalise
    unsettled. The recovery pass has to actually RUN somewhere -- being named in a
    docstring is not wiring.

    It runs inside ``svc.sync_income_events``, which is the panel's Refresh handler
    AND the page's load call, so this asserts the panel loader really syncs. The
    drain itself is pinned service-side by
    ``test_syncing_income_also_drains_the_deferred_runs``."""
    body = _function_source(_page_source(), "_load_income_panel")
    assert "svc.sync_income_events(" in body


def _render_income_panel_note_argument(source: str):
    """The NAME bound to ``render_income_panel(working_note=...)``, or None.

    Returns None when the keyword is missing, and the literal's repr when it is a
    constant -- both of which are the failure this pins.
    """
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "render_income_panel"):
            for keyword in node.keywords:
                if keyword.arg == "working_note":
                    return (keyword.value.id if isinstance(keyword.value, ast.Name)
                            else ast.dump(keyword.value))
            return None
    raise AssertionError("the page never calls render_income_panel")


def test_the_page_asks_what_is_still_outstanding_and_tells_the_panel():
    """The other half of D3: draining is invisible unless the panel SAYS the income
    has not been consumed yet. A panel rendered without the note shows an
    unallocated figure that never goes down and never explains itself."""
    source = _page_source()
    body = _function_source(source, "_load_income_panel")
    assert "svc.describe_unconsumed_runs(" in body
    assert "unconsumed_income_notice(" in body
    # ...and the render call must be handed the COMPUTED note. `working_note=None`
    # satisfies a substring check while dropping the whole point of the task, so
    # the keyword's value is read off the AST and has to be a variable.
    assert _render_income_panel_note_argument(source) == "working_note"


def test_the_page_shows_the_service_reason_when_a_submit_is_blocked():
    """The service re-checks the gate on a fresh read: the dialog can sit open
    across 16:00, so its banner is stale by the time Submit lands."""
    source = _page_source()
    assert "result['blocked']" in source or 'result["blocked"]' in source
    assert "blocked_reason" in source


def test_the_page_never_uses_the_invalid_notify_type():
    """Valid types are positive | negative | warning | info."""
    assert "type='error'" not in _page_source()
    assert 'type="error"' not in _page_source()


def test_the_page_module_actually_imports_with_its_new_wizard_wiring():
    """The one test here that is NOT structural, and the only thing in either suite
    that would catch a bad import in this page.

    The ``ast`` scans above prove the glue is WRITTEN; they cannot prove it RUNS.
    This task adds a module-scope ``from .portfolio_allocation_wizard import ...``,
    and a wrong relative import or a name that has drifted from the pure engine
    fails here rather than as a blank page.

    Cost: ~1.3s inside this suite, because conftest has already paid for the
    expert/LLM stack that ``ui/pages/__init__`` drags in. In a cold process the same
    import is ~4.6s and pulls torch -- which is why every other test in this file
    parses the source instead.
    """
    import ba2_trade_platform.ui.pages.portfolio_allocation as page

    for name in ("content", "_open_allocation_flow", "_open_invest_flow",
                 "_market_gate_for", "_load_flow_inputs", "_solve_plan",
                 "_submit_plan", "_load_income_panel"):
        assert callable(getattr(page, name)), name


# ---------------------------------------------------------------------------
# The one piece of glue that is pure enough to run: the caller mapping the
# contract fixes. Everything else here needs a browser.
# ---------------------------------------------------------------------------

def _hours(**kw):
    from ba2_common.core.account_types import MarketHours
    return MarketHours(**kw)


def test_the_market_gate_mapping_calls_an_open_market_open():
    from datetime import datetime, timezone

    import ba2_trade_platform.ui.pages.portfolio_allocation as page
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_OPEN, MARKET_HOURS_SOURCE_BROKER,
    )

    gate = page._market_gate_for(_hours(
        is_open=True, source=MARKET_HOURS_SOURCE_BROKER,
        as_of=datetime(2026, 8, 20, 17, 0, tzinfo=timezone.utc)))
    assert gate.allowed is True
    assert gate.reason_code == MARKET_GATE_OPEN


def test_the_market_gate_mapping_calls_a_shut_market_closed_with_its_next_open():
    from datetime import datetime, timezone

    import ba2_trade_platform.ui.pages.portfolio_allocation as page
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_CLOSED, MARKET_HOURS_SOURCE_BROKER,
    )

    gate = page._market_gate_for(_hours(
        is_open=False, source=MARKET_HOURS_SOURCE_BROKER,
        as_of=datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc),
        next_open=datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)))
    assert gate.allowed is False
    assert gate.reason_code == MARKET_GATE_CLOSED
    assert gate.next_open_text == "Fri 21 Aug 2026 09:30 ET"


def test_the_market_gate_mapping_calls_an_UNAVAILABLE_answer_unknown_not_closed():
    """The whole reason the mapping is spelled out in the contract. An UNAVAILABLE
    ``MarketHours`` carries ``is_open=False`` so the money path fails closed --
    reading that field alone would tell the user the market is SHUT, which is a
    different problem with a different fix."""
    from datetime import datetime, timezone

    import ba2_trade_platform.ui.pages.portfolio_allocation as page
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
        MARKET_GATE_UNKNOWN, MARKET_HOURS_SOURCE_UNAVAILABLE,
    )

    hours = _hours(is_open=False, source=MARKET_HOURS_SOURCE_UNAVAILABLE,
                   as_of=datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc))
    assert hours.is_open is False and hours.is_known is False
    gate = page._market_gate_for(hours)
    assert gate.allowed is False
    assert gate.reason_code == MARKET_GATE_UNKNOWN


def test_the_market_gate_mapping_treats_no_answer_at_all_as_unknown():
    import ba2_trade_platform.ui.pages.portfolio_allocation as page
    from ba2_trade_platform.ui.utils.portfolio_allocation_view import MARKET_GATE_UNKNOWN

    gate = page._market_gate_for(None)
    assert gate.allowed is False
    assert gate.reason_code == MARKET_GATE_UNKNOWN


def test_the_market_gate_mapping_does_not_fabricate_a_provenance_when_nothing_answered():
    """With no ``MarketHours`` at all there is no source to report, so the mapping
    must pass UNAVAILABLE rather than a plausible-looking default -- otherwise the
    banner would cite the broker for an answer the broker never gave."""
    import ba2_trade_platform.ui.pages.portfolio_allocation as page

    assert page._market_gate_for(None).from_fallback is True
