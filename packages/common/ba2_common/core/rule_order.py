"""Rule precedence of a ruleset in an export file -- ONE reading, shared by every consumer.

``RulesetEventActionLink.order_index`` IS rule precedence: the live evaluator walks a ruleset's
links in ``order_index`` order and the FIRST matching rule wins (``db.ruleset_event_actions``).
An export file carries that as each rule's ``order_index``, and two sides read it:

* the LIVE importer (``rules_export_import.RulesImporter``), which writes the links;
* the BACKTEST converters (``rules_convert.live_export_to_strategy`` behind
  ``/ruleset/convert-live``, and ``live_export_to_trade_rules``), which build the rule lists the
  backtester walks in list order.

If the two read a file differently -- say one by ``order_index``, the other by file position --
a hand-edited file whose two orders disagree runs one precedence in the backtest and another
live. So both call :func:`resolve_order_indices` and nothing else decides.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def resolve_order_indices(rules: Sequence[Dict[str, Any]],
                          ruleset_name: Any) -> Tuple[List[int], Optional[str]]:
    """The precedence index of each rule, aligned with ``rules`` (file order), plus a warning
    when the file's own values could not be used as they stand.

    * every rule carries a distinct integer -> those values EXACTLY (the normal round trip);
    * none carries one (an older export, a hand-written file) -> the FILE ORDER, ``0..n-1``;
    * duplicates, or only some rules carrying one -> indexed rules by value, then the rest;
      ties and unindexed rules keep file order; renumbered ``0..n-1`` so no two rules tie;
    * ``null`` counts as "not carried";
    * anything else that is not a plain ``int`` (a bool, ``"3"``, ``3.0``) -> ``ValueError``.
      A precedence that has to be guessed at is refused, not coerced.
    """
    values: List[Optional[int]] = []
    for pos, rule_data in enumerate(rules):
        v = rule_data.get("order_index")
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise ValueError(
                f"Ruleset {ruleset_name!r}: rule {rule_data.get('name')!r} (position {pos}) has "
                f"order_index {v!r} ({type(v).__name__}); it must be an integer (it is the "
                f"rule's precedence: first match wins)")
        values.append(v)

    n = len(values)
    present = [v for v in values if v is not None]
    if len(present) == n and len(set(present)) == n:
        return [int(v) for v in values], None

    ranked = sorted(range(n), key=lambda i: (values[i] is None, values[i] if values[i] is not None else 0, i))
    orders = [0] * n
    for new_index, pos in enumerate(ranked):
        orders[pos] = new_index

    if not present:
        why = "carries no order_index (older export format); assigned 0..n-1 in file order"
    elif len(present) < n:
        why = (f"carries order_index on only {len(present)} of {n} rules; indexed rules first by "
               f"value, the rest in file order, renumbered 0..n-1")
    else:
        why = "has tied order_index values; tie broken by file order, renumbered 0..n-1"
    return orders, f"Ruleset {ruleset_name!r} {why} (order_index is rule precedence: first match wins)"


def rules_in_precedence(rules: Sequence[Any], ruleset_name: Any
                        ) -> Tuple[List[Tuple[int, Dict[str, Any]]], Optional[str]]:
    """``(order_index, rule)`` pairs sorted by precedence, for a converter that walks the rules.

    Non-dict entries are dropped first (the converters' long-standing tolerance of junk list
    items); the rest go through :func:`resolve_order_indices`."""
    dict_rules = [r for r in rules if isinstance(r, dict)]
    orders, warning = resolve_order_indices(dict_rules, ruleset_name)
    return sorted(zip(orders, dict_rules), key=lambda pair: pair[0]), warning
