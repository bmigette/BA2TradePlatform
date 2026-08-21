# Section F adversarial review — findings to fix

Three independent lenses (correctness / money-and-state / integration), all
**CHANGES_REQUESTED**. Several findings were reached by two or three lenses separately, which is
why they are ranked first. 66 mutations run against
`ui/utils/portfolio_allocation_view.py`: **50 killed, 16 survived**. 8 of 8 page-level mutations
survived.

Not yet fixed — Section G is editing the same two files. Fix in one pass once G lands.

---

## CRITICAL

### F-C1. Opening the label picker silently UNMANAGES labels, destroying their config

`ui/pages/portfolio_allocation.py:251` (and `:267`)

NiceGUI's `Select._event_args_to_value` for `multiple=True` ends with
`return [arg for arg in args if arg in self._values]` — **any selected value absent from the
current OPTIONS is dropped from the reported selection.** Verified against the installed
`nicegui/elements/select.py`.

The picker's options come from `filter_selectable_labels(get_all_instrument_labels())` — labels
currently in use on instrument rows — while its value comes from `get_managed_labels()`. A managed
label whose last instrument was removed is therefore in the VALUE but not the OPTIONS.

`_persist` fires on every change and calls `replace_managed_labels(account_id, selected)`, which
deletes the `PortfolioAllocationLabel` row **and every `PortfolioAllocationSymbol` row under it**.

**Failure:** open the page with a managed label that has no instruments. Touch the picker. Its
`target_pct`, comment and every per-symbol weight and comment are gone, with no warning and no
undo. This is silent destruction of user configuration.

**Fix:** the option list must be the UNION of selectable labels and currently-managed labels, so a
managed label can never be dropped merely by being absent. Separately, `_persist` should not treat
a picker event as authoritative deletion — require an explicit removal gesture, or diff against
the stored set and confirm.

---

## IMPORTANT

### F-I1. The "Managed value" headline double-counts multi-label symbols (40 live today)

`ui/pages/portfolio_allocation.py:389` — found by all three lenses.

`_render_labels` does `total = sum(v.current_value for v in views)`, summing per-label totals.
But `build_label_views` deliberately computes `total_value` over the DISTINCT membership set so a
symbol in two managed labels is counted ONCE — Task 61 decision 7, pinned by
`test_build_label_views_symbol_in_two_labels_is_flagged_and_counted_once`.

So the headline figure and the per-row percentages beneath it use different denominators. The
overlap is real: 40 symbols currently carry two managed labels.

### F-I2. The machine-tag filter hardcodes three expert families; two live tags already leak

`ui/utils/portfolio_allocation_view.py:357` — found by three lenses.

`MACHINE_LABEL_FAMILY_RE = r'^(?:penny|tradingagents|fmprating)-\d+$'` is a snapshot of the label
list, not the rule that generates it. The generating rule is
`MarketExpertInterface.shortname` = `f"{self.__class__.__name__.lower()}-{self.id}"` for EVERY
expert class that does not override it. Any other expert family leaks into the user's label
picker.

**Fix:** derive the filter from `shortname` / the expert registry, not a literal.

### F-I3. Short positions render differently per broker, and can zero a label's percentages

`ui/utils/portfolio_allocation_view.py:103` / `:135` / `:284` — found by three lenses.
Mutation P11 (`+= abs(...)`) SURVIVED: no test models a short.

`positions_by_symbol` never reads `Position.side`. Alpaca returns a short with NEGATIVE
`qty`/`cost_basis`/`market_value` (`alpaca_position_to_position` passes signs through);
TastyTrade sets `qty=abs_qty` (`TastyTradeAccount.py:520-547`). So the same book renders two
different pages depending on the broker.

Worse: `build_label_views` guards denominators with `if label_value > 0`, so on Alpaca a label
whose shorts outweigh its longs reports **every percentage as zero**.

### F-I4. The page module has ZERO behavioural tests — 8 of 8 page mutations survive

`tests/test_portfolio_allocation_route.py` is purely structural (ast-parse plus "content exists").
Nothing exercises `_load_view_payload`, `_load_gate`, `_save_symbol_comment` or
`_save_label_comment`. Mutations that survived a full green suite (1873 + 564):

| Mutation | Re-arms |
|---|---|
| `_load_gate`: `manual = True` | the entire `manual_trading_enabled` gate, i.e. all of Task 56 |
| `_load_gate`: pass `[]` for enabled experts | the expert gate |
| `:122` `get_positions() or []` | **the tri-state incident, for the fifth time** |
| `_save_symbol_comment`: drop `weight_pct=effective.get(symbol)` | **the c63d34c comment-zeroing bug** |
| `:95` `settings.get('manual_trading_enabled', False)` | the exact trap Task 56's own test documents |

### F-I5. "Add symbol" creates phantom global Instrument rows from any typed string

`ui/pages/portfolio_allocation.py:218`

`_open_add_symbol_dialog` uppercases whatever was typed and calls `add_symbols_to_label` ->
`add_label_to_instruments`, which does `if inst is None: session.add(Instrument(name=sym, ...))`.
Typing `APPL`, or `AAPL,` with a stray comma, silently creates a global Instrument row. The
account interface has `symbols_exist` — use it.

---

## MINOR (worth doing while in the file)

- **Blocking DB work on the NiceGUI event loop, per keystroke.** `ui.input(on_change=...)` has no
  `debounce`; `_save_label_comment` does SELECT+UPDATE+commit+refresh per character and
  `_save_symbol_comment` does two round trips. Breaks the page's own `asyncio.to_thread`
  convention (`:166`, `:174`, `:236`).
- **Typing in a stale label's comment box re-creates it** at target 0% — `set_managed_label`
  creates the row when absent (`:166`).
- **`content()` awaits two loaders outside any try** — a DB error 500s the route with no message
  (`:419`).
- **Market mode with no quote renders $0.00** with no indication the quote is missing; a bulk-quote
  outage silently shows every position at zero (`:275`).
- **`get_symbols_by_label` matches labels raw while `get_all_instrument_labels` strips them**, so a
  padded label `' tech '` is offered but can never match (`packages/common/ba2_common/core/utils.py:179`).
- **Deselecting a label irreversibly deletes its per-symbol weights and comments** — one stray
  click on a chip's ✕ (`:251`). Related to F-C1.
- **`LabelView.target_pct` has no test at all** (mutation B24 -> `0.0` survived). This is the
  number Section G's engine keys off as `LabelTarget.target_pct`.
- **`LabelView.market_value` is computed on a MIXED basis** — live price for priced symbols, the
  broker's stamped `market_value` for unpriced ones — never rendered, never tested (B15 survived).
- **`positions_by_symbol` silently drops a blank-symbol row** (P9 survived), in a function whose
  stated purpose is refusing to guess; it raises for a missing quantity but not for this.
- **Cross-account scoping untested** in `remove_symbols_from_label` / `remove_managed_label`;
  dropping the `account_id` predicate leaves 137/137 green.
- **The machine-regex end anchor is untested** — dropping `$` leaves 137/137 green and would
  classify a user label `penny-17-core` as a machine tag.

---

## Plan defects recorded by the implementers

- **Task 67's test imports a class that does not exist**: `AccountsTab`. The real class is
  `AccountDefinitionsTab` (`settings.py:915`). Copied verbatim, the step fails with `ImportError`
  rather than the predicted `AssertionError` — so "watch it fail" would be satisfied by the WRONG
  reason.
- **Task 67's line numbers are stale by 14** (`1025-1042` -> `1039-1056`); the quoted body is
  byte-exact.
- **Neither of Task 67's own tests asserts on `portfolio_allocation_config`** — the one table with
  a UNIQUE `account_id`, and the exact row the Section B review flagged. A cleanup that skipped
  that table would pass the plan's suite.
