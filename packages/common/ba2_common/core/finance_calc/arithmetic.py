"""
Vendored from FinanceHarness (https://github.com/Yijia-Xiao/FinanceHarness),
Apache-2.0 license. Modified for BA2TradePlatform: ToolSpec/ToolResponse layer
removed, ToolError replaced with ValueError, imports localized.

The `calc` tool — exact arithmetic so the model never computes in its head.

A safe AST evaluator over numbers, the standard operators, and a small whitelist
of functions. LLMs are reliable at reasoning and unreliable at arithmetic (a
recurring faithfulness failure: "$37B is a 123% increase from $13B"); offloading
every growth rate, ratio, and percentage to this tool keeps the numbers exact and
auditable.
"""

from __future__ import annotations

import ast
import math
import operator

from pydantic import BaseModel, Field

_DESCRIPTION = (
    "Evaluate an arithmetic expression exactly and return the result — use it for "
    "growth rates, ratios, percentages, and sums so the figures are exact and "
    "auditable, e.g. '(37-13)/13*100' for a percent change. Supports + - * "
    "/ ** % // , parentheses, and abs/round/min/max/sqrt/log/exp."
)

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
}


def _eval(node: ast.AST) -> float:
    """Recursively evaluate a parsed expression, allowing only safe nodes."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
    ):
        return _FUNCS[node.func.id](*(_eval(a) for a in node.args))
    raise ValueError("unsupported expression — numbers, + - * / ** % //, () and "
                     "abs/round/min/max/sqrt/log/exp only")


def safe_eval(expression: str) -> float:
    """Evaluate an arithmetic expression safely (no names, calls, or attribute access
    beyond the whitelisted functions). Raises ValueError on anything unsupported."""
    tree = ast.parse(expression, mode="eval")
    return _eval(tree.body)


class CalcRequest(BaseModel):
    """Input for the exact arithmetic tool."""

    expression: str = Field(..., description="Arithmetic expression, e.g. '(37-13)/13*100'.")


def render_calc(req: CalcRequest) -> str:
    """Exact-arithmetic renderer — the string an agent sees."""
    result = safe_eval(req.expression)
    # Two-decimal display with trailing zeros stripped: 3 -> "3", 184.61538 -> "184.62".
    shown = f"{result:.2f}".rstrip("0").rstrip(".")
    return f"`{req.expression}` = **{shown}**"
