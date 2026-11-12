"""Render ONE stored backtest as ONE self-contained, offline HTML file.

The file reproduces the backtest DETAIL page of the web UI (``frontend/src/pages/Backtesting.tsx``):
header, headline + secondary metric cards, notes, and the six tabs -- Equity Curve, Drawdown,
Capital Used, Trade List, Strategy, Yearly Breakdown -- plus run configuration and the full metric
table (the page keeps those in other panels).

Chart approach: **server-side inline SVG** (plus ~60 lines of inline vanilla JS for hover tooltips,
trade-table sorting/filtering and the theme toggle). Chosen over inlining a chart library because
(a) the file stays small (a recharts/lightweight-charts bundle is 150-500 kB per file and would be
duplicated into every export), (b) there is nothing to break offline -- no fetch, no CDN, no
``<script src>``/``<link href>`` -- and (c) an SVG still prints, diffs and survives an e-mail
client that strips scripts.

Numbers are NOT re-derived. The payload is ``Backtest.to_dict()`` (the very dict ``GET
/api/backtests/{id}`` serves, so the curves carry the same LTTB thinning the page charts) and the
yearly table is ``results.yearly_breakdown`` on the raw columns, exactly as ``GET
/api/backtests/{id}/yearly`` does. The only logic ported from the frontend is *presentation*:
``lib/capitalUsage.ts`` (no backend equivalent exists), ``lib/optionTrades.ts`` structure grouping,
and the rule/condition formatters, kept line-for-line so the file and the page agree.

Not reproduced (interactive or needs live data): the click-a-trade candlestick/option payoff popup
(needs the on-disk OHLCV cache and a running UI), the "hide trade" what-if recompute, the Re-run
button and label editing, and the resolved-ruleset read-back (``bestParams``; the raw parameters are
included as JSON instead).
"""
from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.backtest.results import yearly_breakdown

IDLE_PCT = 10          # lib/capitalUsage.ts
HEAVY_PCT = 50         # lib/capitalUsage.ts
_EXIT_REF_LABEL = {    # lib/actionValues.ts
    "order_open_price": "entry",
    "expert_target_price": "analyst target",
    "current_price": "price",
}
_WORD_TO_SYMBOL = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "==", "neq": "!=", "ne": "!="}

_e = html.escape
DASH = "—"


# --------------------------------------------------------------------------- small helpers


def _num(v: Any) -> float:
    """Frontend ``Number(v)`` with non-finite -> 0 (``num`` in capitalUsage.ts)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _fx(v: Any, nd: int = 1, none: str = "—") -> str:
    return none if v is None else f"{float(v):.{nd}f}"


def _signed(v: float, nd: int = 1, suffix: str = "") -> str:
    return f"{'+' if v >= 0 else ''}{v:.{nd}f}{suffix}"


def _money(v: float, nd: int = 2) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.{nd}f}"


def _cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _first(d: Any, *keys: str) -> Any:
    """First PRESENT key of a stored JSON dict (the page reads camelCase and snake_case spellings)."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _day(v: Any) -> Optional[str]:
    return v[:10] if isinstance(v, str) and len(v) >= 10 else None


def _parse_ms(s: Any) -> float:
    if not isinstance(s, str) or not s:
        return math.nan
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return math.nan
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp() * 1000.0


def format_duration(ms: float) -> str:
    """Port of ``formatDuration`` in Backtesting.tsx."""
    if not math.isfinite(ms) or ms <= 0:
        return "—"
    minutes = ms / 60000
    if minutes < 60:
        return f"{round(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = round(minutes - h * 60)
        return f"{h}h {m}m" if m else f"{h}h"
    days = hours / 24
    if days < 10:
        d = int(days)
        h = round(hours - d * 24)
        return f"{d}d {h}h" if h else f"{d}d"
    return f"{round(days)}d"


def _trade_ms(t: Dict[str, Any]) -> float:
    return _parse_ms(t.get("exitDate")) - _parse_ms(t.get("entryDate"))


def _fmt_trade_date(s: Any) -> str:
    if not s:
        return "—"
    date, _, time = str(s).partition("T")
    return f"{date} {time[:5]}" if time else date


# --------------------------------------------------------------------------- option trades (lib/optionTrades.ts)


def _is_option(t: Dict[str, Any]) -> bool:
    return bool(t.get("optionType"))


def _option_badge(t: Dict[str, Any]) -> str:
    if not _is_option(t):
        return ""
    right = "C" if t["optionType"] == "call" else "P"
    strike = f"{t['strike']}" if t.get("strike") is not None else "?"
    expiry = f" · {t['expiry']}" if t.get("expiry") else ""
    return f"{right} {strike}{expiry}"


def _contract_value(premium: Any, size: Any, mult: Any) -> float:
    return _num(premium) * _num(size) * (_num(mult) or 1.0)


def _group_by_structure(trades: List[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """('single', [t]) or ('structure', legs), preserving order; a lone leg is NOT a structure."""
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        k = t.get("transactionId")
        if k is not None:
            by_key.setdefault(str(k), []).append(t)
    emitted = set()
    out: List[Tuple[str, List[Dict[str, Any]]]] = []
    for t in trades:
        k = None if t.get("transactionId") is None else str(t["transactionId"])
        legs = by_key.get(k) if k is not None else None
        if k is None or not legs or len(legs) < 2:
            out.append(("single", [t]))
            continue
        if k in emitted:
            continue
        emitted.add(k)
        out.append(("structure", legs))
    return out


def _summarise_structure(legs: List[Dict[str, Any]]) -> Dict[str, Any]:
    sign = lambda leg: 1 if leg.get("direction") == "long" else -1  # noqa: E731
    reasons = sorted({l.get("exitReason") for l in legs if l.get("exitReason")})
    entries = sorted(l["entryDate"] for l in legs if l.get("entryDate"))
    exits = sorted(l["exitDate"] for l in legs if l.get("exitDate"))
    return {
        "symbol": legs[0].get("underlyingSymbol") or legs[0].get("symbol") or "",
        "legCount": len(legs),
        "longLegs": sum(1 for l in legs if l.get("direction") == "long"),
        "shortLegs": sum(1 for l in legs if l.get("direction") != "long"),
        "entryDate": entries[0] if entries else "",
        "exitDate": exits[-1] if exits else "",
        "netCost": sum(sign(l) * _contract_value(l.get("entryPrice"), l.get("size"), l.get("multiplier")) for l in legs),
        "netValue": sum(sign(l) * _contract_value(l.get("exitPrice"), l.get("size"), l.get("multiplier")) for l in legs),
        "contracts": sum(_num(l.get("size")) for l in legs),
        "pnl": sum(_num(l.get("pnl")) for l in legs),
        "pnlPercent": sum(_num(l.get("pnlPercent")) for l in legs),
        "exitReason": reasons[0] if len(reasons) == 1 else f"{len(reasons)} reasons",
    }


# --------------------------------------------------------------------------- capital usage (lib/capitalUsage.ts)


def _positions_from(trades: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    lone: List[Dict[str, Any]] = []
    for t in trades or []:
        raw = t.get("transactionId")
        key = None if raw is None or raw == "" else (str(raw).strip() or None)
        if key:
            groups.setdefault(key, []).append(t)
        else:
            lone.append(t)

    positions: List[Dict[str, Any]] = []

    def is_opt(t):
        return bool(t.get("optionType") or t.get("contractSymbol"))

    def build(legs, is_structure):
        entry_days = sorted(d for d in (_day(l.get("entryDate")) for l in legs) if d)
        if not entry_days:
            return
        exit_days = sorted(d for d in (_day(l.get("exitDate")) for l in legs) if d)
        exit_day = exit_days[-1] if exit_days else None
        if not is_structure:
            l0 = legs[0]
            notional = abs(_num(l0.get("entryPrice")) * _num(l0.get("size")) * (_num(l0.get("multiplier")) or 1))
            unpriced = is_opt(l0) and not _num(l0.get("multiplier"))
            positions.append({"entryDay": entry_days[0], "exitDay": exit_day, "notional": notional,
                              "priced": not unpriced, "credit": False})
            return
        net, unpriced = 0.0, False
        for leg in legs:
            if is_opt(leg) and not _num(leg.get("multiplier")):
                unpriced = True
                continue
            d = str(leg.get("direction") or "").lower()
            sign = -1 if d == "short" else 1 if d == "long" else (-1 if _num(leg.get("size")) < 0 else 1)
            net += sign * abs(_num(leg.get("size"))) * _num(leg.get("entryPrice")) * (_num(leg.get("multiplier")) or 1)
        positions.append({"entryDay": entry_days[0], "exitDay": exit_day, "notional": abs(net),
                          "priced": not unpriced, "credit": net < 0})

    for legs in groups.values():
        build(legs, len(legs) > 1)
    for t in lone:
        build([t], False)
    return positions


def capital_usage_series(trades: Sequence[Dict[str, Any]], equity_curve: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    curve = [p for p in (equity_curve or []) if _day(p.get("date"))]
    if not curve:
        return []
    equity_by_day: Dict[str, float] = {}
    for p in curve:
        equity_by_day[_day(p["date"])] = _num(p.get("equity"))
    opens, closes = [], []
    for pos in _positions_from(trades):
        if not pos["priced"]:
            continue
        opens.append((pos["entryDay"], pos["notional"]))
        if pos["exitDay"]:
            closes.append((pos["exitDay"], pos["notional"]))
    opens.sort(key=lambda x: x[0])
    closes.sort(key=lambda x: x[0])
    out, open_n, count, o, c = [], 0.0, 0, 0, 0
    for d in sorted(equity_by_day):
        while o < len(opens) and opens[o][0] <= d:
            open_n += opens[o][1]
            count += 1
            o += 1
        while c < len(closes) and closes[c][0] < d:
            open_n -= closes[c][1]
            count -= 1
            c += 1
        open_n = max(open_n, 0.0)
        count = max(count, 0)
        eq = equity_by_day[d]
        out.append({"date": d, "pct": (open_n / eq) * 100 if eq > 0 else 0.0, "positions": count})
    return out


def _summarise_usage(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not points:
        return {"avg": 0.0, "max": 0.0, "idle": 0.0, "heavy": 0.0, "peak": None}
    peak = points[0]
    for p in points:
        if p["pct"] > peak["pct"]:
            peak = p
    n = len(points)
    return {
        "avg": sum(p["pct"] for p in points) / n, "max": peak["pct"], "peak": peak["date"],
        "idle": sum(1 for p in points if p["pct"] < IDLE_PCT) / n * 100,
        "heavy": sum(1 for p in points if p["pct"] > HEAVY_PCT) / n * 100,
    }


def _usage_exclusions(trades) -> Dict[str, int]:
    pos = _positions_from(trades)
    return {"positions": sum(1 for p in pos if p["priced"]),
            "unpriced": sum(1 for p in pos if not p["priced"]),
            "credit": sum(1 for p in pos if p["priced"] and p["credit"])}


# --------------------------------------------------------------------------- SVG charts

_W, _H, _ML, _MR, _MT, _MB = 920, 320, 72, 18, 12, 30


def _nice_ticks(lo: float, hi: float, n: int = 5) -> List[float]:
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    ticks, v = [], first
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _svg_area_chart(dates: List[str], values: List[float], *, color: str, label: str, y_fmt: str,
                    tip_fmt: str, y_lo: Optional[float] = None, y_hi: Optional[float] = None,
                    ref_lines: Iterable[Tuple[float, str, str]] = (), chart_id: str) -> str:
    """Area chart on an index-uniform (category) x axis, like the recharts ones on the page."""
    n = len(values)
    if n == 0:
        return '<p class="muted">No data.</p>'
    lo = min(values) if y_lo is None else y_lo
    hi = max(values) if y_hi is None else y_hi
    refs = list(ref_lines)
    for yv, _, _ in refs:
        lo, hi = min(lo, yv), max(hi, yv)
    pad = (hi - lo) * 0.05 or 1.0
    if y_lo is None:
        lo -= pad
    if y_hi is None:
        hi += pad
    pw, ph = _W - _ML - _MR, _H - _MT - _MB

    def X(i: int) -> float:
        return _ML + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v: float) -> float:
        return _MT + ph * (1 - (v - lo) / (hi - lo)) if hi > lo else _MT + ph / 2

    line = " ".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(values))
    base = Y(lo)
    area = f"{line} L{X(n - 1):.1f},{base:.1f} L{X(0):.1f},{base:.1f} Z"
    parts = [f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img" aria-label="{_e(label)}" data-chart="{chart_id}">']
    for t in _nice_ticks(lo, hi):
        y = Y(t)
        parts.append(f'<line class="grid" x1="{_ML}" x2="{_W - _MR}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="tick" x="{_ML - 6}" y="{y + 4:.1f}" text-anchor="end">{_e(y_fmt.format(t))}</text>')
    n_lab = min(6, n)
    for k in range(n_lab):
        i = round(k * (n - 1) / (n_lab - 1)) if n_lab > 1 else 0
        anchor = "start" if k == 0 else "end" if k == n_lab - 1 else "middle"
        parts.append(f'<text class="tick" x="{X(i):.1f}" y="{_H - 10}" text-anchor="{anchor}">{_e(dates[i][:10])}</text>')
    for yv, text, dash in refs:
        y = Y(yv)
        parts.append(f'<line class="ref" x1="{_ML}" x2="{_W - _MR}" y1="{y:.1f}" y2="{y:.1f}" stroke-dasharray="{dash}"/>'
                     f'<text class="tick" x="{_W - _MR - 4}" y="{y - 4:.1f}" text-anchor="end">{_e(text)}</text>')
    parts.append(f'<path d="{area}" fill="{color}" fill-opacity="0.22"/>')
    parts.append(f'<path d="{line}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/>')
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{_MT}" y2="{_MT + ph}" visibility="hidden"/>')
    parts.append(f'<rect class="hit" x="{_ML}" y="{_MT}" width="{pw}" height="{ph}" fill="transparent"/>')
    parts.append("</svg>")
    data = {"d": [d for d in dates], "v": [round(v, 4) for v in values], "fmt": tip_fmt,
            "x0": _ML, "pw": pw, "label": label}
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return (f'<div class="chartbox" id="{chart_id}">{"".join(parts)}<div class="tip" hidden></div>'
            f'<script type="application/json" class="chartdata">{payload}</script></div>')


def _svg_bars(labels: List[str], values: List[float], chart_id: str, label: str) -> str:
    """Per-year return bars (zero line, green/red)."""
    n = len(values)
    if n == 0:
        return ""
    lo, hi = min(0.0, min(values)), max(0.0, max(values))
    if hi == lo:
        hi = lo + 1
    h, pw, ph = 220, _W - _ML - _MR, 220 - _MT - _MB
    Y = lambda v: _MT + ph * (1 - (v - lo) / (hi - lo))  # noqa: E731
    parts = [f'<svg class="chart" viewBox="0 0 {_W} {h}" role="img" aria-label="{_e(label)}">']
    for t in _nice_ticks(lo, hi, 4):
        parts.append(f'<line class="grid" x1="{_ML}" x2="{_W - _MR}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                     f'<text class="tick" x="{_ML - 6}" y="{Y(t) + 4:.1f}" text-anchor="end">{t:g}%</text>')
    slot = pw / n
    bw = min(48.0, slot * 0.6)
    for i, (lab, v) in enumerate(zip(labels, values)):
        cx = _ML + slot * (i + 0.5)
        y0, y1 = Y(0), Y(v)
        parts.append(f'<rect x="{cx - bw / 2:.1f}" y="{min(y0, y1):.1f}" width="{bw:.1f}" height="{abs(y1 - y0):.1f}" '
                     f'fill="{"var(--pos)" if v >= 0 else "var(--neg)"}"><title>{_e(lab)}: {v:+.1f}%</title></rect>'
                     f'<text class="tick" x="{cx:.1f}" y="{h - 10}" text-anchor="middle">{_e(lab)}</text>')
    parts.append("</svg>")
    return f'<div class="chartbox" id="{chart_id}">{"".join(parts)}</div>'


# --------------------------------------------------------------------------- strategy tab (rule formatters)


def _action_value(rule: Any) -> Optional[float]:
    v = _first(rule, "action_value", "value", "actionValue")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _action_ref(rule: Any) -> str:
    ref = _first(rule, "reference_value", "referenceValue")
    return _EXIT_REF_LABEL.get(ref, ref) if ref else ""


def _fmt_condition_leaf(c: Any) -> str:
    if not isinstance(c, dict):
        return ""
    if isinstance(c.get("conditions"), list):
        op = "OR" if c.get("operator") == "OR" or c.get("type") == "OR" else "AND"
        inner = f" {op} ".join(s for s in (_fmt_condition_leaf(x) for x in c["conditions"]) if s)
        return (f"({inner})" if len(c["conditions"]) > 1 else inner) if inner else ""
    field = _first(c, "field", "event_type", "eventType") or ""
    if not field:
        return ""
    raw_op = _first(c, "comparison", "op", "operator")
    val = _first(c, "value", "threshold")
    if raw_op in ("is_true", "is_false") or raw_op is None:
        return str(field)
    op = _WORD_TO_SYMBOL.get(str(raw_op).lower(), str(raw_op))
    return " ".join(str(x) for x in (field, op, val) if x not in ("", None))


def _fmt_action(rule: Any) -> str:
    action = _first(rule, "action_type", "action") or ""
    ref, v = _action_ref(rule), _action_value(rule)
    pct = f"{'+' if v >= 0 else ''}{v:g}%" if v is not None else ""
    target = " ".join(x for x in (ref, pct) if x)
    if action == "close":
        return "Close position"
    if action == "buy":
        return "Buy"
    if action == "sell":
        return "Sell"
    if action == "adjust_stop_loss":
        return f"Move stop loss → {target or 'reference'}"
    if action == "adjust_take_profit":
        return f"Move take profit → {target or 'reference'}"
    return str(action).replace("_", " ") if action else "Exit"


def _bold_numbers(text: str) -> str:
    out, last = [], 0
    for m in re.finditer(r"-?\d+(?:\.\d+)?%?", text):
        out.append(_e(text[last:m.start()]))
        out.append(f"<b>{_e(m.group())}</b>")
        last = m.end()
    out.append(_e(text[last:]))
    return "".join(out)


def _clause(label: str, joiner: str, lines: List[str]) -> str:
    if not lines:
        return ""
    rows = "".join(f'<div class="clause"><span class="gut">{label if i == 0 else joiner}</span>'
                   f'<span class="mono">{_bold_numbers(t)}</span></div>' for i, t in enumerate(lines))
    return f'<div class="clauses">{rows}</div>'


def _rule_card(rule: Any, title: str, tone: str) -> str:
    actions = rule["actions"] if isinstance(rule, dict) and isinstance(rule.get("actions"), list) else [rule]
    cond_root = rule.get("conditions") if isinstance(rule, dict) else None
    conds = [s for s in (_fmt_condition_leaf(c) for c in ((cond_root or {}).get("conditions") or [])) if s]
    join = "or" if isinstance(cond_root, dict) and (cond_root.get("operator") == "OR" or cond_root.get("type") == "OR") else "and"
    name = (rule.get("name") if isinstance(rule, dict) else None) or title
    cont = ' <span class="chip warn" title="Evaluation continues to the next rule">continues</span>' \
        if isinstance(rule, dict) and rule.get("continue_processing") else ""
    return (f'<div class="rule {tone}"><div><b>{_e(str(name))}</b>{cont}</div>'
            f'{_clause("when", join, conds or ["always"])}'
            f'{_clause("then", "and", [s for s in (_fmt_action(a) for a in actions) if s])}</div>')


def _group_lines(group: Any) -> str:
    conds = (group or {}).get("conditions") or []
    if not conds:
        return '<span class="muted"><i>always</i></span>'
    if len(conds) == 1:
        return f'<span class="mono">{_bold_numbers(_fmt_condition_leaf(conds[0]) or chr(8212))}</span>'
    op = "OR" if group.get("operator") == "OR" or group.get("type") == "OR" else "AND"
    return "".join(f'<div class="clause"><span class="gut">{"" if i == 0 else op}</span>'
                   f'<span class="mono">{_bold_numbers(_fmt_condition_leaf(c) or chr(8212))}</span></div>'
                   for i, c in enumerate(conds))


def _bracket_card(label: str, kind: str, actions: List[Any], color: str) -> str:
    seen: Dict[str, Tuple[Optional[float], str]] = {}
    for a in actions:
        if (_first(a, "action", "action_type")) != kind:
            continue
        v, r = _action_value(a), _action_ref(a)
        seen[f"{r}|{v}"] = (v, r)
    vals = list(seen.values())
    if len(vals) > 1:
        body = '<span class="big muted">Varies</span> <span class="small muted">· per entry rule</span>'
    elif len(vals) == 1 and vals[0][0] is not None and vals[0][0] < 200:
        v, r = vals[0]
        body = (f'<span class="big {color}">{"+" if v > 0 else ""}{v:g}%</span>'
                + (f' <span class="small muted">· vs {_e(r)}</span>' if r else ""))
    else:
        body = '<span class="big muted">Off</span> <span class="small muted">· via exit rules</span>'
    return f'<div class="card"><div class="small muted">{label}</div>{body}</div>'


def _strategy_html(bt: Dict[str, Any], strategy_row: Any) -> str:
    sp = bt.get("strategyParams")
    if not sp and bt.get("strategyId") and strategy_row is not None:
        sp = {"entryRules": strategy_row.entry_rules, "exitRules": strategy_row.exit_rules,
              "strategyName": strategy_row.name}
    if not sp and not bt.get("strategyId"):
        return '<p class="muted">No strategy information available for this backtest.</p>'
    if not sp:
        return f'<p class="muted">Strategy ID: {bt["strategyId"]} (strategy not found)</p>'
    legacy = any(sp.get(k) for k in ("buyEntryConditions", "sellEntryConditions", "exitConditions", "entryActions", "entry_actions"))
    new_entry = _first(sp, "entryRules", "entry_rules")
    new_exit = _first(sp, "exitRules", "exit_rules")
    has_new = not legacy and (isinstance(new_entry, list) or isinstance(new_exit, list))
    entry_cards = (new_entry or []) if has_new else []
    legacy_actions = _first(sp, "entryActions", "entry_actions") or []
    all_actions = ([a for r in entry_cards for a in (r.get("actions") or []) if a.get("action_type") not in ("buy", "sell")]
                   if entry_cards else legacy_actions)
    buy_group, sell_group = sp.get("buyEntryConditions"), sp.get("sellEntryConditions")
    buy_c = (buy_group or {}).get("conditions") or []
    sell_c = (sell_group or {}).get("conditions") or []
    exit_c = sp.get("exitConditions") or ((new_exit or []) if has_new else [])
    genes = [(k, v) for k, v in sp.items() if re.match(r"^(model:|cond:|exit:|entry:)", k)]
    out: List[str] = []
    if sp.get("strategyName"):
        out.append(f'<div><span class="muted">Strategy: </span><b>{_e(str(sp["strategyName"]))}</b></div>')
    out.append('<div class="grid2">' + _bracket_card("Take Profit", "adjust_take_profit", all_actions, "pos")
               + _bracket_card("Stop Loss", "adjust_stop_loss", all_actions, "neg") + "</div>")
    if entry_cards:
        out.append(f'<h4 class="h-entry">Entry Rules ({len(entry_cards)})</h4>' + "".join(
            _rule_card(r, f"Entry Rule {i + 1}", "entry") for i, r in enumerate(entry_cards)))
    else:
        if buy_c:
            out.append(f'<h4 class="pos">Entry Conditions ({len(buy_c)})</h4><div class="rule buy">{_group_lines(buy_group)}</div>')
        if sell_c:
            out.append(f'<h4 class="neg">Short Entry Conditions ({len(sell_c)})</h4><div class="rule sell">{_group_lines(sell_group)}</div>')
        if legacy_actions:
            out.append(f'<h4 class="h-entry">Entry Actions ({len(legacy_actions)})</h4>' + "".join(
                f'<div class="rule entry">{_e(_fmt_action(a))}</div>' for a in legacy_actions))
    if exit_c:
        out.append(f'<h4 class="h-exit">Exit Conditions ({len(exit_c)})</h4>' + "".join(
            _rule_card(r, f"Exit Rule {i + 1}", "exit") for i, r in enumerate(exit_c)))
    if genes:
        rows = "".join(f'<div class="kv"><span class="mono muted">{_e(k)}</span><b>'
                       f'{_e(str(int(v)) if isinstance(v, float) and v.is_integer() else f"{v:.2f}" if isinstance(v, float) else str(v))}'
                       f'</b></div>' for k, v in genes)
        out.append(f'<h4>Optimized Parameters ({len(genes)})</h4><div class="grid2">{rows}</div>')
    raw = json.dumps(sp, indent=2, default=str, sort_keys=True)
    out.append(f'<details><summary>All strategy parameters (raw JSON)</summary><pre>{_e(raw)}</pre></details>')
    return "".join(out)


# --------------------------------------------------------------------------- trade table


def _trade_cells(t: Dict[str, Any], leg: bool, attrs: str = "") -> str:
    opt = _is_option(t)
    badge = _option_badge(t)
    sym = (t.get("underlyingSymbol") or t.get("symbol")) if opt else t.get("symbol")
    ec = _contract_value(t.get("entryPrice"), t.get("size"), t.get("multiplier"))
    xc = _contract_value(t.get("exitPrice"), t.get("size"), t.get("multiplier"))
    entry_p, exit_p = _num(t.get("entryPrice")), _num(t.get("exitPrice"))
    pnl = _num(t.get("pnl"))
    pct = _num(t.get("pnlPercent"))
    if entry_p:
        rel = ((entry_p - exit_p) if t.get("direction") == "short" else (exit_p - entry_p)) / entry_p * 100
    else:
        rel = 0.0
    badge_html = ""
    if badge:
        badge_html = (f' <span class="chip {"call" if t.get("optionType") == "call" else "put"}" '
                      f'title="{_e(str(t.get("contractSymbol") or ""))}">{_e(badge)}</span>')
    d = t.get("direction")
    return (
        f'<tr class="{"leg" if leg else ""}" {attrs}>'
        f'<td class="sym{" legind" if leg else ""}"><span title="{_e(str(t.get("contractSymbol") or ""))}">{_e(str(sym or chr(8212)))}</span>{badge_html}</td>'
        f'<td class="nw">{_e(_fmt_trade_date(t.get("entryDate")))}</td>'
        f'<td class="nw">{_e(_fmt_trade_date(t.get("exitDate")))}</td>'
        f'<td class="r">${entry_p:.2f}{f"<small>${ec:,.2f}</small>" if opt else ""}</td>'
        f'<td class="r">${exit_p:.2f}{f"<small>${xc:,.2f}</small>" if opt else ""}</td>'
        f'<td class="c"><span class="chip {"long" if d == "long" else "short"}">{_e(str(d))}</span></td>'
        f'<td class="r">{_e(format(_num(t.get("size")), "g"))}</td>'
        f'<td class="r {_cls(pnl)}">{_signed(pnl, 2).replace("+", "+$", 1) if pnl >= 0 else "-$" + format(abs(pnl), ".2f")}</td>'
        f'<td class="r {_cls(pnl)} pct" data-eq="{pct:.4f}" data-tr="{rel:.4f}">{_signed(pct, 2)}%</td>'
        f'<td class="c nw">{format_duration(_trade_ms(t))}</td>'
        f'<td class="c"><span class="chip">{_e(str(t.get("exitReason")))}</span></td></tr>'
    )


def _trades_html(trades: List[Dict[str, Any]]) -> str:
    if not trades:
        return '<p class="muted">No trades.</p>'
    heads = [("Symbol", "s", "l"), ("Entry", "s", "l"), ("Exit", "s", "l"), ("Entry $", "n", "r"), ("Exit $", "n", "r"),
             ("Dir", "s", "c"), ("Size", "n", "r"), ("P&amp;L", "n", "r"), ("P&amp;L %", "n", "r"), ("Duration", "n", "c"), ("Reason", "s", "c")]
    th = "".join(f'<th class="{a}" data-col="{i}" data-type="{ty}">{h}</th>' for i, (h, ty, a) in enumerate(heads))
    bodies: List[str] = []
    for gi, (kind, legs) in enumerate(_group_by_structure(trades)):
        if kind == "single":
            t = legs[0]
            bodies.append(f'<tbody data-pnl="{_num(t.get("pnl"))}">{_trade_cells(t, False)}</tbody>')
            continue
        s = _summarise_structure(legs)
        cost = f'{"" if s["netCost"] >= 0 else "-"}${abs(s["netCost"]):,.2f}<small>net {"debit" if s["netCost"] >= 0 else "credit"}</small>'
        val = f'{"" if s["netValue"] >= 0 else "-"}${abs(s["netValue"]):,.2f}<small>net</small>'
        pnl = s["pnl"]
        head = (
            f'<tr class="structure" data-toggle="s{gi}">'
            f'<td class="sym"><span class="chev">▸</span> {_e(str(s["symbol"] or chr(8212)))} '
            f'<span class="chip spread" title="{s["longLegs"]} long / {s["shortLegs"]} short">{s["legCount"]} legs</span></td>'
            f'<td class="nw">{_e(_fmt_trade_date(s["entryDate"]))}</td><td class="nw">{_e(_fmt_trade_date(s["exitDate"]))}</td>'
            f'<td class="r">{cost}</td><td class="r">{val}</td><td class="c"><span class="chip spread">spread</span></td>'
            f'<td class="r">{s["contracts"]:g}</td>'
            f'<td class="r {_cls(pnl)}">{"-" if pnl < 0 else "+"}${abs(pnl):,.2f}</td>'
            f'<td class="r {_cls(pnl)} pct" data-eq="{s["pnlPercent"]:.4f}" data-tr="{s["pnlPercent"]:.4f}">{_signed(s["pnlPercent"], 2)}%</td>'
            f'<td class="c nw">{format_duration(_parse_ms(s["exitDate"]) - _parse_ms(s["entryDate"]))}</td>'
            f'<td class="c"><span class="chip">{_e(s["exitReason"])}</span></td></tr>')
        legs_html = "".join(_trade_cells(l, True, f'data-leg="s{gi}" hidden') for l in legs)
        bodies.append(f'<tbody data-pnl="{pnl}">{head}{legs_html}</tbody>')
    ctrl = ('<div class="controls"><label>Filter <select id="tf"><option value="all">All Trades</option>'
            '<option value="profit">Profitable</option><option value="loss">Losing</option></select></label>'
            '<span>P&amp;L %: <button type="button" class="seg on" data-mode="eq">Equity</button>'
            '<button type="button" class="seg" data-mode="tr">Trade</button></span>'
            '<span class="muted small">Click a column header to sort. Equity: P&amp;L as % of account equity at entry; '
            'Trade: % price move on the trade.</span><span class="muted small" id="tcount"></span></div>')
    return f'{ctrl}<div class="scroll"><table id="trades"><thead><tr>{th}</tr></thead>{"".join(bodies)}</table></div>'


# --------------------------------------------------------------------------- page assembly

_CSS = """
:root{--bg:#f3f4f6;--card:#fff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--head:#f9fafb;--pos:#16a34a;--neg:#dc2626;
--blue:#2563eb;--purple:#9333ea;--grid:#d1d5db;--amber:#b45309;--amberbg:#fffbeb}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111827;--card:#1f2937;--fg:#f3f4f6;--muted:#9ca3af;
--line:#374151;--head:#2b3544;--pos:#4ade80;--neg:#f87171;--blue:#60a5fa;--purple:#c084fc;--grid:#374151;--amber:#fbbf24;--amberbg:#3a2f12}}
:root[data-theme=dark]{--bg:#111827;--card:#1f2937;--fg:#f3f4f6;--muted:#9ca3af;--line:#374151;--head:#2b3544;--pos:#4ade80;
--neg:#f87171;--blue:#60a5fa;--purple:#c084fc;--grid:#374151;--amber:#fbbf24;--amberbg:#3a2f12}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:0 0 10px}h4{font-size:13px;margin:16px 0 6px}
.muted{color:var(--muted)}.small{font-size:12px}.mono{font-family:ui-monospace,Consolas,monospace}
.pos{color:var(--pos)}.neg{color:var(--neg)}
header.top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;margin-bottom:14px}
.meta{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;color:var(--muted);margin-top:4px}
.panel,.card{background:var(--card);border-radius:8px;box-shadow:0 1px 2px rgba(0,0,0,.12);padding:14px;margin-bottom:14px}
.card{margin:0;padding:12px}.grid4,.grid5,.grid2,.gridm{display:grid;gap:12px;margin-bottom:14px}
.grid4{grid-template-columns:repeat(4,1fr)}.grid5{grid-template-columns:repeat(5,1fr)}.grid2{grid-template-columns:repeat(2,1fr);margin-bottom:8px}
.gridm{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
@media(max-width:760px){.grid4{grid-template-columns:repeat(2,1fr)}.grid5{grid-template-columns:repeat(2,1fr)}}
.lbl{font-size:12px;color:var(--muted)}.val{font-size:24px;font-weight:700}.sm .val{font-size:18px;text-align:center}.sm{text-align:center}
.sub{font-size:11px;color:var(--muted)}.big{font-size:18px;font-weight:700}
.chip{display:inline-block;padding:1px 7px;border-radius:4px;background:var(--head);font-size:11px;font-weight:600;border:1px solid var(--line)}
.chip.long{background:var(--pos);color:#fff;border-color:transparent}.chip.short{background:var(--neg);color:#fff;border-color:transparent}
.chip.spread{background:#4f46e5;color:#fff;border-color:transparent}.chip.call{color:var(--pos)}.chip.put{color:var(--neg)}
.chip.warn{color:var(--amber)}.pill{display:inline-block;padding:1px 9px;border-radius:99px;background:var(--head);font-size:12px;border:1px solid var(--line)}
.note{background:var(--amberbg);color:var(--amber);border-radius:8px;padding:10px 12px;margin-bottom:14px;font-size:12px}
nav.tabs{display:flex;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:-14px -14px 14px}
nav.tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);padding:10px 16px;cursor:pointer;font:inherit}
nav.tabs button.on{color:var(--blue);border-bottom-color:var(--blue)}
section.tab{display:none}section.tab.on{display:block}
@media print{section.tab{display:block!important;page-break-inside:avoid}nav.tabs,.controls,#theme{display:none}}
.chartbox{position:relative}.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--grid);stroke-dasharray:3 3}.chart .ref{stroke:var(--muted)}.chart .tick{fill:var(--muted);font-size:11px}
.chart .cross{stroke:var(--muted);stroke-width:1}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;
box-shadow:0 2px 6px rgba(0,0,0,.25);white-space:nowrap;z-index:2}
.scroll{overflow:auto;max-height:75vh;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--line)}
th{background:var(--head);position:sticky;top:0;text-align:left;font-weight:600;white-space:nowrap}
#trades th{cursor:pointer;user-select:none}#trades th.asc::after{content:" \\25b2"}#trades th.desc::after{content:" \\25bc"}
.r{text-align:right}.c{text-align:center}.l{text-align:left}.nw{white-space:nowrap}td small{display:block;font-size:10px;color:var(--muted)}
tr.structure{background:var(--head);cursor:pointer}tr.structure.open .chev{display:inline-block;transform:rotate(90deg)}
td.legind{padding-left:26px}.controls{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
select,button.seg{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 8px;font:inherit}
button.seg.on{background:var(--blue);color:#fff;border-color:var(--blue)}
.rule{border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:13px}
.rule.entry{background:rgba(59,130,246,.12)}.rule.exit{background:rgba(234,179,8,.14)}.rule.buy{background:rgba(34,197,94,.12)}.rule.sell{background:rgba(239,68,68,.12)}
.clause{display:flex;gap:8px;align-items:baseline}.gut{width:40px;text-align:right;font-size:10px;text-transform:uppercase;opacity:.55;flex:none}
.h-entry{color:var(--blue)}.h-exit{color:var(--amber)}.kv{display:flex;justify-content:space-between;background:var(--head);border-radius:4px;padding:5px 10px;gap:8px}
details{margin-top:14px}summary{cursor:pointer;color:var(--muted)}pre{background:var(--head);padding:10px;border-radius:6px;overflow:auto;max-height:420px;font-size:12px}
#theme{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
footer{color:var(--muted);font-size:11px;text-align:center;margin-top:20px}
"""

_JS = r"""
(function(){
var root=document.documentElement,tb=document.getElementById('theme');
function setT(t){if(t){root.setAttribute('data-theme',t)}else{root.removeAttribute('data-theme')}}
tb.onclick=function(){var cur=root.getAttribute('data-theme');
var dark=cur?cur==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;setT(dark?'light':'dark')};
var tabs=document.querySelectorAll('nav.tabs button'),secs=document.querySelectorAll('section.tab');
function show(id){tabs.forEach(function(b){b.classList.toggle('on',b.dataset.tab===id)});
secs.forEach(function(s){s.classList.toggle('on',s.id==='tab-'+id)})}
tabs.forEach(function(b){b.onclick=function(){show(b.dataset.tab)}});show(tabs[0].dataset.tab);
document.querySelectorAll('.chartbox').forEach(function(box){
var el=box.querySelector('.chartdata');if(!el)return;var d=JSON.parse(el.textContent),svg=box.querySelector('svg'),
cross=svg.querySelector('.cross'),hit=svg.querySelector('.hit'),tip=box.querySelector('.tip'),n=d.v.length;
function fmt(v){return d.fmt.replace('{v}',v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}))}
hit.addEventListener('mousemove',function(e){var r=svg.getBoundingClientRect(),k=r.width/svg.viewBox.baseVal.width,
x=(e.clientX-r.left)/k;var i=n>1?Math.round((x-d.x0)/d.pw*(n-1)):0;i=Math.max(0,Math.min(n-1,i));
var cx=n>1?d.x0+d.pw*i/(n-1):d.x0+d.pw/2;cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('visibility','visible');
tip.hidden=false;tip.textContent=d.d[i].replace('T',' ').slice(0,16)+'  '+fmt(d.v[i]);
var bw=box.getBoundingClientRect().width,tx=cx*k+10;if(tx>bw-170)tx=cx*k-180;tip.style.left=tx+'px';tip.style.top='8px'});
hit.addEventListener('mouseleave',function(){cross.setAttribute('visibility','hidden');tip.hidden=true})});
var T=document.getElementById('trades');if(!T)return;
var groups=Array.prototype.slice.call(T.tBodies),mode='eq',sortCol=-1,asc=true;
function cellVal(g,c,type){var td=g.rows[0].cells[c];var s=td.textContent.trim();
if(type==='n'){var m=s.replace(/[,$+%]/g,'').match(/-?\d+(\.\d+)?/);return m?parseFloat(m[0]):-Infinity}return s.toLowerCase()}
T.tHead.querySelectorAll('th').forEach(function(th){th.onclick=function(){var c=+th.dataset.col,ty=th.dataset.type;
asc=sortCol===c?!asc:true;sortCol=c;T.tHead.querySelectorAll('th').forEach(function(x){x.classList.remove('asc','desc')});
th.classList.add(asc?'asc':'desc');groups.sort(function(a,b){var x=cellVal(a,c,ty),y=cellVal(b,c,ty);return(x<y?-1:x>y?1:0)*(asc?1:-1)});
groups.forEach(function(g){T.appendChild(g)})}});
function apply(){var f=document.getElementById('tf').value,shown=0;groups.forEach(function(g){var p=parseFloat(g.dataset.pnl),
ok=f==='all'||(f==='profit'&&p>=0)||(f==='loss'&&p<0);g.hidden=!ok;if(ok)shown++});
document.getElementById('tcount').textContent=shown+' of '+groups.length+' rows'}
document.getElementById('tf').onchange=apply;apply();
document.querySelectorAll('button.seg').forEach(function(b){b.onclick=function(){mode=b.dataset.mode;
document.querySelectorAll('button.seg').forEach(function(x){x.classList.toggle('on',x===b)});
T.querySelectorAll('td.pct').forEach(function(td){var v=parseFloat(td.dataset[mode]);td.textContent=(v>=0?'+':'')+v.toFixed(2)+'%'})}});
T.querySelectorAll('tr.structure').forEach(function(tr){tr.onclick=function(){var id=tr.dataset.toggle,open=!tr.classList.contains('open');
tr.classList.toggle('open',open);tr.parentNode.querySelectorAll('tr[data-leg="'+id+'"]').forEach(function(l){l.hidden=!open})}});
})();
"""


def _card(label: str, value_html: str, sub_html: str = "", small: bool = False) -> str:
    return (f'<div class="card{" sm" if small else ""}"><div class="lbl">{label}</div>'
            f'<div class="val">{value_html}</div>{f"<div class=sub>{sub_html}</div>" if sub_html else ""}</div>')


def _all_metrics_rows(bt: Dict[str, Any]) -> str:
    rows = [
        ("Total return", bt.get("totalReturn"), "{:.2f}%"), ("Adjusted total return", bt.get("adjustedTotalReturn"), "{:.2f}%"),
        ("Annualized return", bt.get("annualizedReturn"), "{:.2f}%"), ("Buy &amp; hold return", bt.get("buyHoldReturn"), "{:.2f}%"),
        ("GA fitness", bt.get("gaFitness"), "{:.4f}"), ("Sharpe", bt.get("sharpeRatio"), "{:.2f}"),
        ("Sortino", bt.get("sortinoRatio"), "{:.2f}"), ("Calmar", bt.get("calmarRatio"), "{:.2f}"), ("SQN", bt.get("sqn"), "{:.2f}"),
        ("Volatility (ann.)", bt.get("volatility"), "{:.2f}%"), ("Max drawdown", bt.get("maxDrawdown"), "{:.2f}%"),
        ("Avg drawdown", bt.get("avgDrawdown"), "{:.2f}%"), ("Max DD duration (days)", bt.get("maxDrawdownDuration"), "{:.1f}"),
        ("Win rate", bt.get("winRate"), "{:.2f}%"), ("Profit factor", bt.get("profitFactor"), "{:.2f}"),
        ("Expectancy", bt.get("expectancy"), "{:.4f}"), ("Avg trade", bt.get("avgTrade"), "{:.3f}%"),
        ("Best trade", bt.get("bestTrade"), "{:.2f}%"), ("Worst trade", bt.get("worstTrade"), "{:.2f}%"),
        ("Total trades", bt.get("totalTrades"), "{}"), ("Winning trades", bt.get("winningTrades"), "{}"),
        ("Losing trades", bt.get("losingTrades"), "{}"), ("Avg trade duration", bt.get("avgTradeDuration"), "{:.2f}"),
        ("Exposure time", bt.get("exposureTime"), "{:.2f}%"), ("Final equity", bt.get("finalEquity"), "${:,.2f}"),
        ("Equity peak", bt.get("equityPeak"), "${:,.2f}"),
    ]
    return "".join(f'<tr><td>{k}</td><td class="r">{DASH if v is None else f.format(v)}</td></tr>' for k, v, f in rows)


def _config_rows(bt: Dict[str, Any]) -> str:
    rows = [
        ("Backtest id", bt["id"]), ("Name", bt["name"]), ("Engine", bt.get("engineType")), ("Expert", bt.get("expertName")),
        ("Status", bt.get("status")), ("Period", f'{(bt.get("startDate") or "")[:10]} → {(bt.get("endDate") or "")[:10]}'),
        ("Initial capital", None if bt.get("initialCapital") is None else f'${bt["initialCapital"]:,.2f}'),
        ("Position sizing", f'{bt.get("positionSizingType")} {bt.get("positionSizingValue")}'),
        ("Commission", bt.get("commission")), ("Slippage", bt.get("slippage")), ("Fitness metric", bt.get("fitnessMetric")),
        ("Optimization id", bt.get("optimizationId")), ("Model id", bt.get("modelId")), ("Strategy id", bt.get("strategyId")),
        ("Prediction dataset id", bt.get("predictionDatasetId")), ("Execution dataset id", bt.get("executionDatasetId")),
        ("Labels", ", ".join(bt.get("labels") or [])), ("Created", bt.get("createdAt")), ("Started", bt.get("startedAt")),
        ("Completed", bt.get("completedAt")), ("Error", bt.get("errorMessage")),
    ]
    return "".join(f'<tr><td>{_e(k)}</td><td>{_e(str(v))}</td></tr>' for k, v in rows if v not in (None, ""))


def render_backtest_html(backtest: Any, strategy: Any = None, exported_at: Optional[datetime] = None) -> str:
    """Return the complete HTML document for one ``Backtest`` ORM row.

    ``strategy`` is the ``Strategy`` row for ``backtest.strategy_id`` when the caller has a session
    (the page falls back to it when ``strategyParams`` is empty); ``None`` is fine.
    """
    bt = backtest.to_dict()
    # Raw (non-thinned) columns, like GET /api/backtests/{id}/yearly.
    years = yearly_breakdown(backtest.equity_curve or [], backtest.drawdown_curve or [], backtest.trades or [])
    exported_at = exported_at or datetime.now(timezone.utc)
    trades: List[Dict[str, Any]] = bt["results"]["trades"]
    eq_curve: List[Dict[str, Any]] = bt["results"]["equityCurve"]
    dd_curve: List[Dict[str, Any]] = bt["results"]["drawdownCurve"]

    # ---- metric cards (same formulas as the page's tiles)
    raw_ret = bt.get("totalReturn") or 0.0
    adj = bt.get("adjustedTotalReturn")
    has_adj = adj is not None and abs(adj - raw_ret) > 0.05
    ret = adj if has_adj else raw_ret
    ret_sub = f"raw {_signed(raw_ret, 1)}%" if has_adj else ""
    dur = [ms for ms in (_trade_ms(t) for t in trades) if math.isfinite(ms) and ms > 0]
    avg_dur = format_duration(sum(dur) / len(dur)) if dur else "—"
    sharpe = bt.get("sharpeRatio")
    top = "".join([
        _card("Total Return", f'<span class="{_cls(ret)}">{_signed(ret, 1)}%</span>', ret_sub),
        _card("Sharpe Ratio", f'<span style="color:var(--blue)">{_fx(sharpe, 2)}</span>'),
        _card("Max Drawdown", f'<span class="neg">-{abs(bt.get("maxDrawdown") or 0):.1f}%</span>'),
        _card("Win Rate", f'<span style="color:var(--purple)">{(bt.get("winRate") or 0):.1f}%</span>'),
    ])
    best, worst = bt.get("bestTrade") or 0.0, bt.get("worstTrade") or 0.0
    second = "".join([
        _card("Profit Factor", _fx(bt.get("profitFactor") or 0.0, 2), small=True),
        _card("Total Trades", str(bt.get("totalTrades")), small=True),
        _card("Avg Duration", avg_dur, small=True),
        _card("Best Trade", f'<span class="pos">{_signed(best, 1)}%</span>', small=True),
        _card("Worst Trade", f'<span class="neg">{worst:.1f}%</span>', small=True),
    ])

    # ---- charts
    init_cap = bt.get("initialCapital") or 10000
    if eq_curve:
        equity = _svg_area_chart([p["date"] for p in eq_curve], [_num(p.get("equity")) for p in eq_curve],
                                 color="#22c55e", label="Equity curve", y_fmt="${:,.0f}", tip_fmt="${v}",
                                 ref_lines=[(float(init_cap), "Initial", "3 3")], chart_id="c-equity")
    else:
        equity = '<p class="muted">No equity curve stored for this run.</p>'
    if dd_curve:
        vals = [_num(p.get("drawdown")) for p in dd_curve]
        drawdown = _svg_area_chart([p["date"] for p in dd_curve], vals, color="#ef4444", label="Drawdown",
                                   y_fmt="{:.1f}%", tip_fmt="{v}%", y_lo=min(vals + [0.0]), y_hi=max(vals + [0.0]),
                                   chart_id="c-dd")
    else:
        drawdown = '<p class="muted">No drawdown curve stored for this run.</p>'

    usage = capital_usage_series(trades, eq_curve)
    if usage:
        st, ex = _summarise_usage(usage), _usage_exclusions(trades)
        cards = "".join(f'<div class="card"><div class="lbl">{a}</div><div class="big">{b}</div><div class="sub">{_e(c)}</div></div>'
                        for a, b, c in [
                            ("Average", f'{st["avg"]:.1f}%', "of equity committed on a typical day"),
                            ("Peak", f'{st["max"]:.1f}%', f'on {st["peak"]}' if st["peak"] else ""),
                            ("Idle days", f'{st["idle"]:.0f}%', f"under {IDLE_PCT}% used — room for another strategy"),
                            ("Heavy days", f'{st["heavy"]:.0f}%', f"over {HEAVY_PCT}% used — competing for cash")])
        warn = ""
        if ex["unpriced"]:
            warn += (f'<li>{ex["unpriced"]} option position(s) left out: the contract multiplier was never recorded, '
                     f'so their notional is unknown. They are NOT counted as zero.</li>')
        if ex["credit"]:
            warn += (f'<li>{ex["credit"]} credit structure(s) counted at the net premium RECEIVED — the margin '
                     f'they actually tie up is not modelled here.</li>')
        capital = (f'<div class="grid4">{cards}</div><p class="sub">Open position notional as a share of account equity that day. '
                   f'An option structure counts once, at its NET premium ({ex["positions"]} position(s) counted).</p>'
                   f'{f"<ul class=small>{warn}</ul>" if warn else ""}'
                   + _svg_area_chart([u["date"] for u in usage], [u["pct"] for u in usage], color="#3b82f6",
                                     label="Capital used", y_fmt="{:.0f}%", tip_fmt="{v}% of equity", y_lo=0.0,
                                     ref_lines=[(IDLE_PCT, f"{IDLE_PCT}% idle", "4 4"), (HEAVY_PCT, f"{HEAVY_PCT}% heavy", "4 4")],
                                     chart_id="c-cap"))
    else:
        capital = '<p class="muted">No equity curve for this run, so capital usage cannot be measured.</p>'

    if years:
        yr_rows = "".join(
            f'<tr><td><b>{_e(str(y["year"]))}</b></td><td class="{_cls(y["returnPct"])}">{_signed(y["returnPct"], 1)}%</td>'
            f'<td class="neg">{y["maxDrawdownPct"]:.1f}%</td><td>{y["sharpeRatio"]:.2f}</td><td>{y["totalTrades"]}</td>'
            f'<td>{y["winRate"]:.1f}%</td></tr>' for y in years)
        yearly = (_svg_bars([str(y["year"]) for y in years], [y["returnPct"] for y in years], "c-yearly", "Return by year")
                  + '<table><thead><tr>' + "".join(f"<th>{h}</th>" for h in ("Year", "Return %", "Max DD %", "Sharpe", "Trades", "Win %"))
                  + f"</tr></thead><tbody>{yr_rows}</tbody></table>")
    else:
        yearly = '<p class="muted">Not enough equity-curve data to break down by year.</p>'

    open_note = ""
    if (bt.get("totalTrades") or 0) == 0 and not trades:
        open_note = '<div class="note">This run recorded 0 closed round-trips; any P&amp;L comes from positions still open at the end.</div>'
    notes = (f'<div class="panel"><h2>Notes</h2><div style="white-space:pre-wrap">{_e(bt["description"])}</div></div>'
             if bt.get("description") else "")
    err = (f'<div class="note">Run error: {_e(bt["errorMessage"])}</div>' if bt.get("errorMessage") else "")
    labels = "".join(f'<span class="pill">{_e(l)}</span> ' for l in (bt.get("labels") or []))

    tabs = [("equity", "Equity Curve", equity), ("drawdown", "Drawdown", drawdown), ("capital", "Capital Used", capital),
            ("trades", f"Trade List ({len(trades)})", _trades_html(trades)),
            ("strategy", "Strategy", _strategy_html(bt, strategy)), ("yearly", "Yearly Breakdown", yearly)]
    nav = "".join(f'<button type="button" data-tab="{k}">{_e(t)}</button>' for k, t, _ in tabs)
    body = "".join(f'<section class="tab" id="tab-{k}">{c}</section>' for k, _, c in tabs)

    period = f'{(bt.get("startDate") or "")[:10]} → {(bt.get("endDate") or "")[:10]}'
    model_sfx = " · Model #%s" % bt["modelId"] if bt.get("modelId") is not None else ""
    engine_pill = "Daily expert · multi-asset" if bt.get("engineType") == "daily_expert" else "ML strategy" + model_sfx
    title = f'Backtest #{bt["id"]} — {bt["name"]}'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>
<header class="top"><div><h1>{_e(title)}</h1>
<div class="meta"><span>Backtest id: <b>{bt["id"]}</b></span><span>Expert: <b>{_e(str(bt.get("expertName") or DASH))}</b></span>
<span>Period: <b>{_e(period)}</b></span><span>Ran: {_e(str(bt.get("completedAt") or bt.get("createdAt") or DASH))}</span>
<span>Exported: {_e(exported_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip())}</span></div>
<div style="margin-top:6px"><span class="pill">{_e(engine_pill)}</span> <span class="pill">{_e(str(bt.get("status")))}</span> {labels}</div></div>
<button id="theme" type="button" title="Toggle light/dark">Light / Dark</button></header>
{err}{open_note}<div class="grid4">{top}</div><div class="grid5">{second}</div>{notes}
<div class="panel"><nav class="tabs">{nav}</nav>{body}</div>
<div class="panel"><h2>Run configuration</h2><table><tbody>{_config_rows(bt)}</tbody></table></div>
<div class="panel"><h2>All metrics</h2><div class="scroll"><table><tbody>{_all_metrics_rows(bt)}</tbody></table></div></div>
<footer>Exported from BA2 Test Platform. Self-contained file; no network access required.</footer>
</main><script>{_JS}</script></body></html>
"""
