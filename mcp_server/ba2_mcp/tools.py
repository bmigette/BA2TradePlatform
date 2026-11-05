"""One MCP tool per /api/v1 operation. Each tool is declared with ``@api_tool(METHOD, path_template, ...)``; the
decorator turns the call into an HTTP request (path placeholders, query and body from the tool's own parameters)
and records ``(METHOD, path)`` so a parity test can compare the tool set with the API's OpenAPI schema.

Write tools carry ``confirm: bool = False``: unconfirmed they only preview (``?dry_run=true``); confirmed they
execute with a fresh ``Idempotency-Key``. Writes are never retried here."""
import functools
import inspect
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Literal, Optional, Tuple
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .client import ApiCallError, Ba2Client

mcp = FastMCP("ba2")

NOTHING_SENT = "Nothing was sent. Call again with confirm=true to execute."

#: Tool parameter name -> the API's query-string name.
_QUERY_NAMES = {"from_date": "from", "to_date": "to", "account_id": "account", "expert_id": "expert",
                "transaction_id": "transaction", "activity_type": "type"}

#: API routes deliberately without a tool, with the reason (the parity test accepts nothing else).
INTENTIONALLY_UNEXPOSED: Dict[Tuple[str, str], str] = {
    ("POST", "/run-schedule"): "legacy alias of POST /experts/{expert_id}/run (tool run_expert_analysis_now)",
}

#: Routes served by more than one tool (explicit-name tools over one PATCH body); nothing else may share a route.
SHARED_ROUTES = {("PATCH", "/experts/{expert_id}")}


@dataclass
class ToolSpec:
    name: str
    method: str
    path: str
    write: bool
    server_dry_run: bool
    func: Callable


REGISTRY: Dict[str, ToolSpec] = {}

_client: Optional[Ba2Client] = None


def set_client(client: Optional[Ba2Client]) -> None:
    global _client
    _client = client


def get_client() -> Ba2Client:
    global _client
    if _client is None:
        _client = Ba2Client.from_env()
    return _client


def api_tool(method: str, path: str, *, write: bool = False, body: Tuple[str, ...] = (),
             body_arg: Optional[str] = None, fixed_body: Optional[Dict[str, Any]] = None,
             server_dry_run: bool = True):
    """``body``: parameter names sent in the JSON body (None values dropped: the API refuses null).
    ``body_arg``: one dict parameter that IS the body. ``fixed_body``: constant body fields of this tool.
    Every other non-path parameter is a query parameter.
    ``server_dry_run=False``: the endpoint ignores ``?dry_run`` (so sending it would EXECUTE): unconfirmed calls
    send nothing and return a local description instead."""
    placeholders = re.findall(r"{(\w+)}", path)

    def decorate(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        if write and "confirm" not in sig.parameters:
            raise TypeError(f"{fn.__name__}: a write tool needs a confirm parameter")
        for ph in placeholders:
            if ph not in sig.parameters:
                raise TypeError(f"{fn.__name__}: path placeholder {ph} is not a parameter")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            values = dict(bound.arguments)
            fn(**values)                                  # tool-specific validation (raises ToolError), if any
            confirm = values.pop("confirm") if write else True
            url = path
            for ph in placeholders:
                url = url.replace("{" + ph + "}", quote(str(values.pop(ph)), safe=""))
            json_body = None
            if body_arg is not None:
                json_body = values.pop(body_arg)
            elif body or fixed_body:
                json_body = dict(fixed_body or {})
                for k in body:
                    v = values.pop(k)
                    if v is not None:
                        json_body[k] = v
            query = {(_QUERY_NAMES[k] if k in _QUERY_NAMES else k): v for k, v in values.items() if v is not None}
            try:
                client = get_client()
                if not write:
                    return client.request(method, url, query=query, body=json_body)
                if not confirm:
                    if not server_dry_run:
                        return {"dry_run": True, "endpoint_has_dry_run": False,
                                "preview": {"would_call": f"{method} {url}", "query": query, "body": json_body},
                                "note": NOTHING_SENT}
                    preview = client.request(method, url, query={**query, "dry_run": True}, body=json_body,
                                             write=True)
                    return {"dry_run": True, "preview": preview, "note": NOTHING_SENT}
                result = client.request(method, url, query=query, body=json_body, write=True,
                                        idempotency_key=str(uuid.uuid4()))
                return {"executed": True, "result": result}
            except ApiCallError as e:
                raise ToolError(str(e)) from None

        if "limit" in sig.parameters:
            wrapper.__doc__ = (fn.__doc__ or "") + _PAGING
        wrapper.__signature__ = sig.replace(return_annotation=Dict[str, Any])
        REGISTRY[fn.__name__] = ToolSpec(fn.__name__, method, path, write, server_dry_run, wrapper)
        mcp.tool()(wrapper)
        return wrapper
    return decorate


# Shared parameter note: every list tool pages with limit/offset.
_PAGING = " limit is 1-500 (default 50); offset skips that many rows. The response has items and total."


# ------------------------------------------------------------------ system
@api_tool("GET", "/ping")
def get_platform_status() -> Dict[str, Any]:
    """Liveness probe of the platform: returns {"status": "ok", "app_version": ...}. Use it to check that
    BA2_API_URL and the token work."""


# ------------------------------------------------------------------ accounts
@api_tool("GET", "/accounts")
def list_accounts(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List the broker account definitions with a live snapshot summary (balance, equity, buying power in
    account currency). Settings are masked; credentials are never returned."""


@api_tool("GET", "/accounts/{account_id}")
def get_account(account_id: int) -> Dict[str, Any]:
    """One account: definition, provider, masked settings and the live snapshot (balance, equity, cash, buying
    power, broker multiplier, margin factor, gross exposure, headroom, pending entry and pending close, all in
    account currency). Fails with 503 `unmeasurable` rather than reporting a missing figure as 0."""


@api_tool("GET", "/accounts/{account_id}/positions")
def get_account_positions(account_id: int) -> Dict[str, Any]:
    """Broker-side open positions of the account, from the broker cache (not the platform's transactions).
    Returns {items, as_of}; an empty items list means flat, an unreachable broker is an error."""


@api_tool("GET", "/accounts/{account_id}/orders")
def get_broker_orders(account_id: int, status: Optional[str] = None) -> Dict[str, Any]:
    """Broker-side orders of the account from the broker cache. status: optional broker filter such as
    "open", "closed" or "all". Returns {items, as_of}. For the platform's own order rows use list_orders."""


# ------------------------------------------------------------------ transactions and orders
@api_tool("GET", "/transactions")
def list_transactions(account_id: Optional[int] = None, expert_id: Optional[int] = None,
                      status: Optional[str] = None, symbol: Optional[str] = None,
                      from_date: Optional[str] = None, to_date: Optional[str] = None,
                      include_open_pnl: bool = True, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List the platform's trades (transactions) with realized P&L for closed ones and floating P&L for open
    ones (account currency). status: WAITING, OPENED, CLOSING, CLOSED or FAILED. from_date/to_date: ISO date or
    datetime filtering on the open date. include_open_pnl=false skips the live price reads (faster)."""


@api_tool("GET", "/transactions/{transaction_id}")
def get_transaction(transaction_id: int, include_open_pnl: bool = True) -> Dict[str, Any]:
    """One trade: its orders, take-profit / stop-loss values, the manual override locks and its activity log.
    Check this before and after any write on the trade."""


@api_tool("GET", "/orders")
def list_orders(account_id: Optional[int] = None, status: Optional[str] = None,
                transaction_id: Optional[int] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List the platform's own order rows (TradingOrder), including pending and WAITING_TRIGGER ones.
    status is an OrderStatus value such as "open" or "new"."""


@api_tool("POST", "/transactions/{transaction_id}/tp-sl", write=True, body=("take_profit", "stop_loss", "lock"))
def set_take_profit_and_stop_loss(transaction_id: int, take_profit: Optional[float] = None,
                                  stop_loss: Optional[float] = None, lock: bool = False,
                                  confirm: bool = False) -> Dict[str, Any]:
    """WRITE (live money). Set the take-profit and/or stop-loss price of an open trade, the same path as the Live
    Trades dialog. Prices are absolute instrument prices (not percentages). Give at least one; an omitted side
    keeps its stored value (removing a leg is not offered). A price on the wrong side of the current price is
    refused (422); a side locked by a manual override is refused (409) unless lock=true. lock=true makes the
    change a manual override that automation will not overwrite. Replaces the standing exit orders at the broker.
    confirm=false (default) previews the planned broker calls and sends nothing; confirm=true executes and
    returns the result including the new broker leg ids."""
    if take_profit is None and stop_loss is None:
        raise ToolError("Give take_profit and/or stop_loss (absolute prices).")


@api_tool("POST", "/transactions/{transaction_id}/tp-sl/unlock", write=True)
def unlock_take_profit_and_stop_loss(transaction_id: int, confirm: bool = False) -> Dict[str, Any]:
    """WRITE. Clear the manual override locks on a trade's take-profit and stop-loss so automation may adjust them
    again. Does not change any price. confirm=false (default) previews only; confirm=true executes."""


@api_tool("POST", "/transactions/{transaction_id}/close", write=True)
def close_transaction(transaction_id: int, confirm: bool = False) -> Dict[str, Any]:
    """WRITE (live money). Close a trade at the broker: cancels its standing exit orders and submits a closing
    order for the whole position. Refused with 409 when the trade is already CLOSING, CLOSED or FAILED.
    confirm=false (default) previews the orders it would cancel and place and sends nothing; confirm=true executes.
    After a timeout do not retry blindly: check get_transaction first."""


@api_tool("POST", "/orders/{order_id}/cancel", write=True)
def cancel_order(order_id: int, confirm: bool = False) -> Dict[str, Any]:
    """WRITE. Cancel a pending platform order (order_id is the platform's TradingOrder id, see list_orders).
    Refused with 409 for an order already in a terminal state. confirm=false (default) previews only;
    confirm=true executes."""


# ------------------------------------------------------------------ experts
@api_tool("GET", "/experts")
def list_experts(account_id: Optional[int] = None, enabled: Optional[bool] = None, limit: int = 50,
                 offset: int = 0) -> Dict[str, Any]:
    """List expert instances with their last analysis, without touching the broker. enabled filters on the
    instance's enabled flag."""


@api_tool("GET", "/experts/{expert_id}")
def get_expert(expert_id: int) -> Dict[str, Any]:
    """One expert instance: masked settings, schedule, virtual_equity_pct (percent of the account's tradable
    balance, 0-1000), virtual and available balance and reserve (account currency), and the last run."""


@api_tool("PATCH", "/experts/{expert_id}", write=True, fixed_body={"enabled": True})
def enable_expert(expert_id: int, confirm: bool = False) -> Dict[str, Any]:
    """WRITE. Enable an expert instance so its schedule runs and it may open trades (subject to the account's
    automated-trading setting). confirm=false (default) previews only; confirm=true executes."""


@api_tool("PATCH", "/experts/{expert_id}", write=True, fixed_body={"enabled": False})
def disable_expert(expert_id: int, confirm: bool = False) -> Dict[str, Any]:
    """WRITE. Disable an expert instance: its schedule stops firing and it opens no new trades. Open trades and
    their broker exit orders are left alone. confirm=false (default) previews only; confirm=true executes."""


@api_tool("PATCH", "/experts/{expert_id}", write=True, body=("virtual_equity_pct",))
def set_expert_virtual_equity_pct(expert_id: int, virtual_equity_pct: float,
                                  confirm: bool = False) -> Dict[str, Any]:
    """WRITE (changes position sizing). Set the expert's virtual equity as a PERCENT of the account's tradable
    balance, from 0 to 1000 (100 = the whole balance; 10 = ten percent). confirm=false (default) previews the
    resulting balances; confirm=true executes."""


@api_tool("PATCH", "/experts/{expert_id}/settings", write=True, body_arg="settings")
def update_expert_settings(expert_id: int, settings: Dict[str, Any], confirm: bool = False) -> Dict[str, Any]:
    """WRITE. Change expert settings: `settings` is an object of {setting_key: new_value}, validated against the
    expert's settings definitions (unknown keys or wrong types are refused with 422). Takes effect immediately
    (instance cache and schedule reload). Secret values are masked in every response. confirm=false (default)
    previews only; confirm=true executes."""


@api_tool("POST", "/experts/{expert_id}/run", write=True, body=("subtype",))
def run_expert_analysis_now(expert_id: int, subtype: Literal["enter_market", "open_positions"],
                            confirm: bool = False) -> Dict[str, Any]:
    """WRITE (may open trades). Fire the expert's registered schedule for `subtype` right now, like the
    Scheduled Jobs page's Run Now. subtype "enter_market" analyses and may open positions; "open_positions"
    reviews open ones. confirm=false (default) previews only; confirm=true executes."""


# ------------------------------------------------------------------ analysis, recommendations, risk manager, activity
@api_tool("GET", "/analyses")
def list_market_analyses(expert_id: Optional[int] = None, symbol: Optional[str] = None,
                         status: Optional[str] = None, subtype: Optional[str] = None,
                         from_date: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List market analyses with their status and skip reasons. status: a MarketAnalysisStatus value such as
    COMPLETED or FAILED; subtype: "enter_market" or "open_positions"; from_date: ISO datetime."""


@api_tool("GET", "/analyses/{analysis_id}")
def get_market_analysis(analysis_id: int, include_text: bool = False, include_state: bool = False) -> Dict[str, Any]:
    """One analysis with its outputs and recommendation. include_text adds the full output texts and
    include_state the raw expert state; both can be large, so they are off by default."""


@api_tool("GET", "/recommendations")
def list_recommendations(expert_id: Optional[int] = None, symbol: Optional[str] = None,
                         from_date: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List expert recommendations (action, confidence on a 1-100 scale, expected profit percent).
    from_date: ISO datetime."""


@api_tool("GET", "/risk-manager/runs")
def list_risk_manager_runs(expert_id: Optional[int] = None, kind: Optional[Literal["rm", "smart"]] = None,
                           limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List risk-manager runs, newest first: kind "rm" = classic RiskManagerRun, "smart" = SmartRiskManagerJob.
    Each row has a `ref` ("rm-12" / "smart-3") to pass to get_risk_manager_run."""


@api_tool("GET", "/risk-manager/runs/{ref}")
def get_risk_manager_run(ref: str) -> Dict[str, Any]:
    """One risk-manager run by ref ("rm-<id>" or "smart-<id>", from list_risk_manager_runs) with its decisions."""


@api_tool("GET", "/activity")
def list_activity(activity_type: Optional[str] = None, severity: Optional[str] = None,
                  expert_id: Optional[int] = None, account_id: Optional[int] = None,
                  from_date: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """The platform's activity log, newest first (writes through this API appear here with source `api`).
    activity_type / severity are the ActivityLogType / ActivityLogSeverity values; from_date: ISO datetime."""


# ------------------------------------------------------------------ stats and account figures
@api_tool("GET", "/stats/overview")
def get_overview_stats(account_id: Optional[int] = None) -> Dict[str, Any]:
    """The Overview page's figures: P&L by period, per-expert performance, allocation and exposure, for one
    account or (without account_id) all. Amounts in account currency."""


@api_tool("GET", "/accounts/{account_id}/growth")
def get_account_growth(account_id: int, from_date: Optional[str] = None,
                       to_date: Optional[str] = None) -> Dict[str, Any]:
    """Balance history and total growth of the account (the Overview growth tab). from_date / to_date: ISO dates
    (YYYY-MM-DD). Returns the series plus total growth; a part that cannot be measured is flagged partial with a
    reason, never zero."""


@api_tool("GET", "/accounts/{account_id}/growth/labels")
def get_growth_by_label(account_id: int, labels: Optional[str] = None, from_date: Optional[str] = None,
                        to_date: Optional[str] = None) -> Dict[str, Any]:
    """Growth of the account split by instrument label. labels: optional comma-separated list of labels to
    include (default all). from_date / to_date: ISO dates."""


@api_tool("GET", "/accounts/{account_id}/growth/labels/{label}/positions")
def get_growth_by_position(account_id: int, label: str, from_date: Optional[str] = None,
                           to_date: Optional[str] = None) -> Dict[str, Any]:
    """Growth by position inside one label of the account. from_date / to_date: ISO dates."""


@api_tool("GET", "/accounts/{account_id}/pnl/closed")
def get_closed_pnl(account_id: int, from_date: Optional[str] = None, to_date: Optional[str] = None,
                   group_by: Literal["day", "month", "expert", "symbol"] = "day", limit: int = 50,
                   offset: int = 0) -> Dict[str, Any]:
    """Closed (realized) P&L of the account in account currency, grouped by day, month, expert or symbol.
    from_date / to_date: ISO datetimes."""


@api_tool("GET", "/accounts/{account_id}/dividends")
def get_dividends(account_id: int, from_date: Optional[str] = None, to_date: Optional[str] = None,
                  symbol: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Cash dividends received by the account plus the income ledger (portfolio income events and the open
    income total). from_date / to_date: ISO dates."""


# ------------------------------------------------------------------ portfolio
@api_tool("GET", "/accounts/{account_id}/portfolio")
def get_portfolio(account_id: int) -> Dict[str, Any]:
    """The allocation view: per label target versus current weight (percent), invested amount, floating P&L with
    and without dividends, symbol counts, and per-symbol rows. 404 when the account has no allocation config."""


@api_tool("GET", "/accounts/{account_id}/portfolio/runs")
def list_portfolio_runs(account_id: int, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Allocation runs of the account and their fill status."""


# ------------------------------------------------------------------ maintenance
@api_tool("POST", "/reload", write=True, body=("expert_instance_id", "account_id"), server_dry_run=False)
def reload_platform_caches(expert_instance_id: Optional[int] = None, account_id: Optional[int] = None,
                           confirm: bool = False) -> Dict[str, Any]:
    """WRITE. Drop the platform's cached expert and account instances (and their settings) so the next access
    re-reads the database, and refresh the schedules. Use it after an out-of-band database edit. With no ids it
    reloads everything; give expert_instance_id and/or account_id to scope it. This endpoint has no server-side
    preview: confirm=false (default) sends nothing and only describes the call."""


@api_tool("POST", "/process-recommendations", write=True, body=("expert_instance_id", "lookback_days"),
          server_dry_run=False)
def process_recommendations(expert_instance_id: int, lookback_days: int = 1, confirm: bool = False) -> Dict[str, Any]:
    """WRITE (live money: can open trades). Run the classic risk-manager pass over an expert's EXISTING
    recommendations from the last lookback_days days (default 1), as the Trade Recommendations card's
    "Process Recommendations" button does. HOLDs and symbols with an open or waiting trade are skipped, and
    automated trading must be enabled. This endpoint has no server-side preview: confirm=false (default) sends
    nothing and only describes the call."""
