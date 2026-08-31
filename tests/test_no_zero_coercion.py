"""The unknown-reads-as-zero lint rule, and the helper that makes the right thing shorter to type.

WHY THIS EXISTS. A read-only audit found 25 places where a value nobody measured became ``0.0``
and was then read as a measured answer. They were found one at a time, over months, each by its
own investigation. Ones that reached production:

  * an unknown option reserve *freeing* buying power;
  * ``float(o.strike or 0.0)`` making an unknown strike free money;
  * ``and self._peak_equity`` letting a peak of exactly ``0.0`` disable a circuit breaker;
  * a 429 rejection reading as "this symbol has no data".

The audit weighed a typed ``Measured[T]``/``Unknown`` sum type and rejected it: SQLModel columns
cannot hold one, so you would unwrap at every DB boundary -- which is precisely where the
coercions live. That moves ``or 0.0`` to ``.unwrap_or(0.0)`` and calls it progress. So: a lint
rule, plus :func:`ba2_common.core.failure_modes.must_measure`, which is SHORTER to type than
``or 0.0`` -- the ergonomic reason the pattern keeps reappearing.

WHAT IT DETECTS. ``<money-shaped name> or <numeric literal>``, and ``.get(key, <numeric
literal>)`` / ``getattr(obj, key, <numeric literal>)`` on a money-shaped key. ``float(...)`` and
``int(...)`` around either come free -- the coercion is a child node, so the walk reaches it
regardless of the wrapper.

CALIBRATION, 2026-08-25, measured against this tree rather than imagined:

  * 185 raw hits. A 40-site random sample (seed 20260825), each read together with the
    declaration of the field it coerces, classified as 18 real defects (45%), 15 dead defensive
    ``or`` on fields declared NON-Optional -- these can never fire -- and 7 legitimate zeros
    (17.5%). Only that last group is the kind that gets a rule switched off.
  * The starting vocabulary offered ``_pct``, ``value``, ``amount`` and ``cost``. Measured:
    ``_pct`` +47 hits, ``value`` +49, ``amount`` +31, ``cost`` +9, and among them not one sum of
    money nobody had measured -- they were percentages, ratios, configured bounds and generic
    container fields. All four were REMOVED, and ``_pct`` was inverted into the
    ``NOT_MEASUREMENTS`` veto because a percentage is categorically not a measurement of money.
    ``profit`` and ``capital`` were tried and cut for the same reason (forecast percentages and
    backtest config). ``filled_qty`` was dropped as redundant: ``qty`` already matches it.
  * That veto then went one step too far, and measuring RECALL is what caught it. Replaying the
    already-fixed instances of this bug class through the rule showed ``virtual_equity_pct or
    100.0`` -- the "a 0% sleeve gets the whole account" defect that shipped in FOUR places at
    once -- being silently vetoed on ``_pct``. Hence ``STOCKS_OF_MONEY``: a percentage OF a
    stock of money is the money question wearing a "%". Cost, measured: two hits, both settings
    defaults, both allowlisted. Within minutes of the carve-out landing it flagged a FIFTH,
    previously unknown clone of that same defect being reintroduced in AccountInterface.

RECALL, measured by replaying 32 reconstructed instances of this bug class through the rule:
8 are caught (25%) -- below the audit's estimate of 10 of 25. The misses cluster, and the
pattern is worth knowing before trusting a clean run:

  * a CALL on the left of ``or`` (``self.get_balance() or 0.0``) -- excluded on purpose;
  * bare truthiness with no literal at all (``and self._peak_equity``, ``if not close_price``)
    -- the single largest miss category, and it includes one of the audit's four headline bugs;
  * shapes that are not coercion expressions: ``return 0.0``, a tuple literal, ``x if x is not
    None else 0.0``, a wrong constant, a cache miss serving an empty frame.

This rule is a ratchet on one specific syntax, not a proof of correctness.
  * Words kept despite finding nothing today -- ``reserve``, ``premium``, ``proceeds``,
    ``payout``, ``credit``, ``debit``, ``margin``, ``nav``, ``p_l``. They cost zero noise and one
    of them names the audit's worst production bug (an unknown option RESERVE freeing buying
    power), which is exactly the thing that must not come back.

A DISCARDED IDEA, recorded so it is not retried. Resolving each coerced name against the type
annotations in its own file would drop the ~35 dead-defensive hits in ``portfolio_allocation.py``
(``delta_quantity: float = 0.0`` can never be None). Measured, it also silenced 21 ``quantity``
and 14 ``multiplier`` hits in ``backtest_account.py``, where a same-named local parameter
annotated ``float`` shadows the genuinely ``Optional`` ``TradingOrder`` column. Trading real
defects for tidiness is the wrong direction for this rule, so the annotation lever is not used.

KNOWN BLIND SPOT, pinned by a test: a money value whose name carries no money word --
``quote.last``, ``bar.close``. The vocabulary that would catch those matches hundreds of
non-money names. The recall is traded for the precision that keeps the rule switched on.
"""
from __future__ import annotations

import ast
import math
import pathlib
from typing import Iterator, NamedTuple, Optional

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The money-carrying trees. Everything else (thirdparties, tests, migrations, tools) can coerce
# what it likes -- nothing there decides how many shares to buy.
SCAN_ROOTS = (
    "packages/common/ba2_common",
    "packages/providers/ba2_providers",
    "packages/experts/ba2_experts",
    "ba2_trade_platform/core",
    "ba2_trade_platform/modules/accounts",
    "ba2_trade_platform/ui",
    "testplatform/backend/app/services",
)

# Substring, case-insensitive, matched against the LAST component of the coerced expression
# (``o.strike`` -> "strike", ``d["net_premium"]`` -> "net_premium", ``raw_qty`` -> "raw_qty").
#
# CALIBRATED, not imagined. Each word below earned its place by finding a real coercion in this
# tree; the ones that only produced noise were cut, and the cuts are recorded next to the list
# in the module docstring of the rule test. A word that matches hundreds of non-money names
# does not make the rule stricter, it makes it deleted.
MONEY_WORDS = frozenset({
    "qty",
    "quantity",
    "price",
    "strike",
    "multiplier",
    "balance",
    "equity",
    "reserve",
    "premium",
    "notional",
    "pnl",
    "p_l",
    "commission",
    "cash",
    "buying_power",
    "margin",
    "proceeds",
    "payout",
    "credit",
    "debit",
    "nav",
})

# VETO. A percentage, a ratio, a count, a span of days or a score is not a money or quantity
# MEASUREMENT, and defaulting one is ordinarily a design decision rather than an unknown read as
# an answer. The audit's own list of legitimate zeros makes the point: ``min_stop_loss_pct or
# 0.0`` is inert because the interface default is 7.0.
#
# This is why ``_pct`` was REMOVED from the starting vocabulary and turned into a veto instead:
# measured against this tree it added 47 hits, and every one was a percentage, a ratio or a
# configured bound -- ``expected_profit_percent``, ``price_delta_pct``, ``profit_ratio``,
# ``price_target_window_days``, ``virtual_equity_pct``, ``price_drop_checked``. Not one was a
# sum of money nobody had measured.
NOT_MEASUREMENTS = (
    "_pct",
    "percent",
    "ratio",
    "_days",
    "_count",
    "_checked",
    "_boost",
    "_adj",
    "_score",
    "_window",
)

# ...EXCEPT when the percentage is a percentage OF A STOCK OF MONEY. A share of the equity, the
# balance or the buying power IS the money question wearing a "%" -- ``virtual_equity_pct or
# 100.0`` shipped in four places and turned "give this expert nothing" into "give it the entire
# account", because the column is NOT NULL with a default of 100.0 so the coercion could only
# ever fire on a real user-entered 0 (``tests/test_virtual_equity_zero_pct.py``).
#
# The distinction is stock vs flow. A percentage of a BALANCE is money. A percentage that is a
# forecast, a price move or a configured tolerance (``expected_profit_percent``,
# ``price_delta_pct``, ``profit_ratio``) is not, and un-vetoing those is what produced the 47
# useless hits. Measured cost of this carve-out over the whole tree: two hits.
STOCKS_OF_MONEY = (
    "equity",
    "balance",
    "cash",
    "notional",
    "buying_power",
)


class Violation(NamedTuple):
    path: str            # repo-relative, POSIX
    line: int            # 1-based
    name: str            # the money-shaped name that got coerced
    kind: str            # "or" | "get"
    snippet: str         # the source line, stripped


def _is_numeric_literal(node: ast.AST) -> bool:
    """A plain number written in the source. ``True``/``False`` are ``int`` subclasses in Python
    and are emphatically NOT measurements, so they are excluded."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_numeric_literal(node.operand)
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _name_of(node: ast.AST) -> Optional[str]:
    """The name a value is known by: ``x`` / ``o.x`` / ``d["x"]``.

    Deliberately NOT calls. ``compute_total() or 0`` is a different judgement call -- the
    function may well be documented to return 0 -- and including calls tripled the hit count
    without adding a single real defect when measured against this tree.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def _is_money_shaped(name: Optional[str]) -> bool:
    if not name:
        return False
    low = name.lower()
    if any(unit in low for unit in NOT_MEASUREMENTS):
        if not any(stock in low for stock in STOCKS_OF_MONEY):
            return False
    return any(word in low for word in MONEY_WORDS)


class _ZeroCoercionVisitor(ast.NodeVisitor):
    def __init__(self, path: str, lines: list) -> None:
        self.path = path
        self.lines = lines
        self.found: list = []

    def _record(self, node: ast.AST, name: str, kind: str) -> None:
        line = getattr(node, "lineno", 0)
        src = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        self.found.append(Violation(self.path, line, name, kind, src))

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # ``a or b or 0`` -- the literal's left neighbour is what is being defaulted.
        if isinstance(node.op, ast.Or) and len(node.values) >= 2:
            if _is_numeric_literal(node.values[-1]):
                left = node.values[-2]
                name = _name_of(left)
                if _is_money_shaped(name):
                    self._record(left, name, "or")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # ``d.get("net_premium", 0.0)`` and ``getattr(o, "strike", 0.0)``.
        key = default = None
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and not node.keywords
        ):
            key, default = node.args
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) == 3
            and not node.keywords
        ):
            key, default = node.args[1], node.args[2]

        if key is not None and _is_numeric_literal(default):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                name = key.value
            else:
                name = _name_of(key)
            if _is_money_shaped(name):
                self._record(node, name, "get")
        self.generic_visit(node)


def find_zero_coercions(source: str, filename: str) -> list:
    """Every money-shaped value in *source* coerced to a numeric literal.

    At most one violation per line: two coercions on one line cannot be allowlisted apart, and
    a per-line report is what a reviewer can act on.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = _ZeroCoercionVisitor(filename, source.splitlines())
    visitor.visit(tree)
    seen, out = set(), []
    for v in visitor.found:
        if v.line in seen:
            continue
        seen.add(v.line)
        out.append(v)
    return sorted(out, key=lambda v: v.line)


def find_zero_coercions_in_file(path) -> list:
    p = pathlib.Path(path)
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = p.as_posix()
    return find_zero_coercions(source, rel)


def iter_scanned_files() -> Iterator:
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def scan_repo(apply_allowlist: bool = True) -> list:
    out = []
    for path in iter_scanned_files():
        for v in find_zero_coercions_in_file(path):
            if apply_allowlist and f"{v.path}:{v.line}" in ALLOWLIST:
                continue
            out.append(v)
    return out


def scan_counts_by_file() -> dict:
    counts: dict = {}
    for v in scan_repo():
        counts[v.path] = counts.get(v.path, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# The allowlist's own guards, as PURE FUNCTIONS.
#
# They were inline assertions until a mutation run showed why that is not good enough: deleting
# the body of ``test_every_allowlist_entry_carries_a_justification`` SURVIVED, because a test
# whose only job is to assert something cannot notice its own assertion going missing. Pulled
# out here, each guard is exercised against synthetic input by a test that is not the guard, so
# gutting one now fails a different test.
# --------------------------------------------------------------------------- #

MIN_JUSTIFICATION = 15


def unjustified_entries(allowlist: dict) -> list:
    """Sites exempted without saying why. Blank, whitespace, or too short to be a reason."""
    return sorted(
        site for site, why in allowlist.items()
        if not isinstance(why, str) or len(why.strip()) < MIN_JUSTIFICATION
    )


def stale_entries(allowlist: dict, live_sites) -> list:
    """Exemptions whose site no longer coerces -- blanket permission nobody is using."""
    return sorted(set(allowlist) - set(live_sites))


# --------------------------------------------------------------------------- #
# ALLOWLIST -- exact ``repo/relative/path.py:line`` -> one line saying why this zero is MEASURED.
#
# An empty or hand-wavy justification fails ``test_every_allowlist_entry_carries_a_justification``
# on purpose: a list you can append to silently is not a guard, it is a mute button. An entry
# whose site no longer coerces fails too, so the exemptions cannot outlive their reason.
#
# This list is SMALL BY DESIGN. It is not where pre-existing findings go -- those go in
# BASELINE below, unblessed. An entry here is a claim that the zero is CORRECT, made by someone
# who read the site.
# --------------------------------------------------------------------------- #
ALLOWLIST: dict = {
    "ba2_trade_platform/ui/pages/settings.py:3851":
        "settings import: 100.0 is the documented virtual_equity_pct column default, not a "
        "measurement of anything",
    "ba2_trade_platform/ui/pages/settings.py:3899":
        "same settings-import default; the comment above the line records why the fallback was "
        "deliberately restored",
    "ba2_trade_platform/ui/components/performance_charts.py:412":
        "a transaction with no P&L counts as neither a win (>0) nor a loss (<0), which is the "
        "right answer for an unmeasured trade",
    "ba2_trade_platform/ui/components/performance_charts.py:413":
        "loss half of the same win-rate count; 0 is excluded from both tallies rather than "
        "scored as either",
    "testplatform/backend/app/services/data_build_handler.py:157":
        "screener CONFIG bound: an absent price_min means 'no minimum', which is what 0.0 "
        "expresses; it is not a quote",
    "packages/experts/ba2_experts/settings_io.py:210":
        "settings IMPORT default matching the NOT NULL column default; an export of a 0% sleeve "
        "carries the key explicitly, so this only fires on a pre-field export",
    "packages/common/ba2_common/core/TradeActions.py:1546":
        "10.0 is the documented default of the max_virtual_equity_per_instrument_percent "
        "SETTING, a configured cap rather than a measurement of anything "
        "(was :1529 before the ARC gate added lines above it; :1532 before the Phase-2a "
        "resolve split added the option_request import; :1533 before it added the "
        "option_payoff import; :1534 before the F3 entry-quote gene added the "
        "option_entry_quote import; :1537 before the option risk manager wiring added "
        "the OptionRiskManagement import and the _option_risk_manager helper)",
    "testplatform/backend/app/services/backtest/parity_harness.py:223":
        "parity HARNESS synthesising a stub bar; 100.0 is an arbitrary fixture price and the "
        "double 'or 100.0' says so",
}


# --------------------------------------------------------------------------- #
# BASELINE -- the debt register, frozen 2026-08-25. NOT an allowlist.
#
# The tree already contained 179 of these when the rule was written. They are deliberately NOT
# justified and NOT blessed: a measured sample of 40 (seed 20260825) classified 18 as real
# defects, 15 as dead defensive ``or`` on fields declared non-Optional, and 7 as legitimate --
# so roughly half of what is parked below is a live bug. Fixing them is a separate pass; this
# rule's job today is to stop the 180th.
#
# Keyed by FILE, not file:line, on purpose. Three agents are editing scan roots concurrently and
# a line-keyed baseline of this size would spend its life being re-pointed by unrelated commits;
# a per-file cap survives line drift while still failing the moment a file grows a new coercion.
# The cost is that a fix and a fresh defect in the same file can cancel out -- which is why the
# per-file numbers must RATCHET DOWN (``test_baseline_has_not_rotted`` fails when a count is
# higher than reality) rather than being left as generous headroom.
# --------------------------------------------------------------------------- #
BASELINE: dict = {
    "ba2_trade_platform/core/ModelBillingUsage.py": 12,
    "ba2_trade_platform/core/SmartRiskManagerGraph.py": 6,
    "ba2_trade_platform/core/SmartRiskManagerToolkit.py": 2,
    # option_lifecycle_service.py is GONE from this register: its 3 coercions moved with
    # ``_build_structure`` into ba2_common.core.OptionRiskManagement (the shared sleeve
    # reader, listed below at the same count), and the service now delegates.
    "ba2_trade_platform/core/portfolio_allocation_service.py": 3,
    "ba2_trade_platform/modules/accounts/AlpacaAccount.py": 11,
    "ba2_trade_platform/modules/accounts/IBKRAccount.py": 2,
    "ba2_trade_platform/modules/accounts/TastyTradeAccount.py": 5,
    "ba2_trade_platform/ui/pages/marketanalysishistory.py": 1,
    "ba2_trade_platform/ui/pages/overview.py": 17,
    "ba2_trade_platform/ui/pages/settings.py": 2,
    "ba2_trade_platform/ui/pages/tools.py": 1,
    "ba2_trade_platform/ui/utils/portfolio_allocation_view.py": 3,
    "packages/common/ba2_common/core/TradeActions.py": 3,
    "packages/common/ba2_common/core/TradeConditions.py": 5,
    "packages/common/ba2_common/core/TransactionHelper.py": 4,
    "packages/common/ba2_common/core/interfaces/OptionsAccountInterface.py": 1,
    "packages/common/ba2_common/core/interfaces/ReadOnlyAccountInterface.py": 1,
    "packages/common/ba2_common/core/models.py": 2,
    "packages/common/ba2_common/core/portfolio_allocation.py": 39,
    # 2 MOVED here, not added, and the total below is unchanged: the per-contract
    # option balance was lifted out of ReadOnlyAccountInterface.refresh_transactions
    # (3 -> 1) into ``option_contract_net`` so the LIVE settlement door
    # (AlpacaAccount._apply_option_activity) can call the same arithmetic instead of
    # growing a second copy of it. The two coercions travelled verbatim.
    "packages/common/ba2_common/core/utils.py": 2,
    "packages/common/ba2_common/core/position_sizing.py": 2,
    "packages/experts/ba2_experts/DeterministicScorer/__init__.py": 3,
    # The three PROMOTED sleeve-reader coercions, moved verbatim out of
    # option_lifecycle_service (whose entry above drops to 0 by exactly this amount): a
    # filled/ordered quantity, a transaction quantity and the contract multiplier, each
    # read off a persisted row. Net repo change: zero.
    "packages/common/ba2_common/core/OptionRiskManagement.py": 3,
    "packages/experts/ba2_experts/FMPRating.py": 1,
    "packages/experts/ba2_experts/FMPSenateTraderCopy.py": 1,
    "packages/experts/ba2_experts/FMPSenateTraderWeight.py": 3,
    "packages/experts/ba2_experts/FactorRanker/data.py": 1,
    "packages/experts/ba2_experts/FactorRanker/portfolio.py": 1,
    "packages/experts/ba2_experts/PennyMomentumTrader/monitoring.py": 1,
    "packages/experts/ba2_experts/PennyMomentumTrader/screening.py": 1,
    # 4 -> 2: two went with the deleted ``_txn_metrics`` rails stopgap (design 2026-08-27
    # S4); the two left are in _tested / _close_structure, which this work does not touch.
    "packages/experts/ba2_experts/PremiumSeller/portfolio.py": 2,
    "packages/providers/ba2_providers/fundamentals/overview/FMPCompanyOverviewProvider.py": 1,
    "packages/providers/ba2_providers/insider/FMPInsiderProvider.py": 4,
    "testplatform/backend/app/services/backtest/backtest_account.py": 26,
    "testplatform/backend/app/services/backtest_handler.py": 1,
    "testplatform/backend/app/services/strategy_optimization_handler.py": 1,
}
BASELINE_TOTAL = 174


# =========================================================================== #
# The helper: must_measure / unmeasured
# =========================================================================== #

def test_must_measure_returns_a_real_number_unchanged():
    from ba2_common.core.failure_modes import must_measure

    assert must_measure(123.45, "AAPL strike") == 123.45


def test_must_measure_honours_a_genuinely_measured_zero():
    """A measured $0.00 is an ANSWER. The helper must not confuse it with absence -- that
    confusion is the whole bug class it exists to stop."""
    from ba2_common.core.failure_modes import must_measure

    assert must_measure(0.0, "realised P&L on a scratch") == 0.0
    assert must_measure(0, "quantity after a full close") == 0.0


def test_must_measure_rejects_none_naming_what_was_unmeasurable():
    from ba2_common.core.failure_modes import UnmeasuredValue, must_measure

    with pytest.raises(UnmeasuredValue) as exc:
        must_measure(None, "AAPL 2026-01-16 C150 strike")
    assert "AAPL 2026-01-16 C150 strike" in str(exc.value)


def test_must_measure_rejects_nan():
    """NaN is the other face of the same bug: it propagates silently through arithmetic and
    lands in a column as a number."""
    from ba2_common.core.failure_modes import UnmeasuredValue, must_measure

    with pytest.raises(UnmeasuredValue) as exc:
        must_measure(float("nan"), "option delta")
    assert "option delta" in str(exc.value)


def test_must_measure_rejects_infinity():
    from ba2_common.core.failure_modes import UnmeasuredValue, must_measure

    with pytest.raises(UnmeasuredValue):
        must_measure(math.inf, "notional")


def test_must_measure_coerces_a_numeric_string_rather_than_guessing():
    """Broker payloads arrive as strings and ``Decimal``. Accepting them is what keeps the
    helper shorter than the coercion it replaces."""
    from decimal import Decimal

    from ba2_common.core.failure_modes import must_measure

    assert must_measure("150.25", "alpaca qty") == 150.25
    assert must_measure(Decimal("150.25"), "tastytrade balance") == 150.25


def test_must_measure_rejects_a_non_numeric_string_rather_than_zeroing_it():
    from ba2_common.core.failure_modes import UnmeasuredValue, must_measure

    with pytest.raises(UnmeasuredValue) as exc:
        must_measure("", "alpaca qty")
    assert "alpaca qty" in str(exc.value)


def test_unmeasured_builds_the_tri_state_absence_without_raising():
    """The tri-state return: a caller that legitimately cannot measure says so, and the value
    is not a number anyone can accidentally add up."""
    from ba2_common.core.failure_modes import Unmeasured, unmeasured

    u = unmeasured("HTTP 429 from the quote endpoint")
    assert isinstance(u, Unmeasured)
    assert u.reason == "HTTP 429 from the quote endpoint"
    assert "429" in str(u)


def test_unmeasured_is_falsy_but_is_not_a_number():
    """``if not value`` must not silently treat it as zero-the-quantity; arithmetic must fail
    loudly instead of producing a plausible number."""
    from ba2_common.core.failure_modes import unmeasured

    u = unmeasured("no quote")
    assert not u
    with pytest.raises(TypeError):
        _ = u + 1.0
    with pytest.raises(TypeError):
        _ = float(u)


def test_must_measure_rejects_an_unmeasured_marker():
    """Passing the tri-state absence into a place that needs a number must name the reason."""
    from ba2_common.core.failure_modes import UnmeasuredValue, must_measure, unmeasured

    with pytest.raises(UnmeasuredValue) as exc:
        must_measure(unmeasured("HTTP 429"), "AAPL last price")
    assert "HTTP 429" in str(exc.value)
    assert "AAPL last price" in str(exc.value)


# =========================================================================== #
# The fixture module.
#
# The rule is a test, so it needs tests of its own -- against a FIXED corpus, not against the
# real tree, which changes under us (three other agents are editing scan roots right now).
#
# Every line is tagged. ``# BAD:`` marks a coercion the checker must flag and ``# OK:`` one it
# must not; the expectations are read back off the markers, so the fixture cannot drift out of
# sync with its assertions. The OK half is the important half: it is copied from the audit's
# list of LEGITIMATE zeros, and a rule that flags those gets switched off within a week.
# =========================================================================== #

FIXTURE = '''
def known_bad(quote, o, leg, payload, d, row, t, alloc, self, x, acct, raw_qty):
    qty = raw_qty or 0.0                                # BAD: unmeasured size becomes flat
    price = quote.last_price or 0                       # BAD: no quote becomes free
    strike = float(o.strike or 0.0)                     # BAD: the audit's own example
    mult = int(leg.multiplier or 1)                     # BAD: unknown contract size, 100x off
    bal = payload["balance"] or 0.0                     # BAD: subscript form
    reserve = d.get("option_reserve", 0.0)              # BAD: unknown reserve FREES buying power
    n = row.get("filled_qty", 0)                        # BAD: a fill nobody read becomes no fill
    pnl = float(t.realized_pnl or 0.0)                  # BAD: float() around the coercion
    w = alloc.get("net_credit", 0)                      # BAD: .get with a money-shaped key
    commission = self.commission_paid or 0.0            # BAD: unbilled fees read as free
    notional = float(x.notional or 0)                   # BAD: attribute under float()
    bp = acct.buying_power or 0.0                       # BAD: a broker outage reads as broke
    cash = payload.get("cash_available", 0)             # BAD: .get on a money-shaped key
    prem = int(d["premium_collected"] or 0)             # BAD: int() around a subscript coercion
    return qty, price, strike, mult, bal, reserve, n, pnl, w, commission, notional, bp, cash, prem


def known_good(is_option, cost, self, raw, positions, payload, attempts, cfg, flags, rows, quote):
    mult = 100 if is_option else 1                      # OK: equities have no multiplier
    if cost <= self._cash:                              # OK: a cash floor is the real constraint
        pass
    cash = max(self._cash, 0.0)                         # OK: same
    px = float(raw) if raw is not None else None        # OK: the CORRECT shape
    if positions is None:                               # OK: "0 reconciled" from a fetch failure
        return 0
    name = payload.get("symbol") or "UNKNOWN"           # OK: not a numeric literal
    retries = attempts or 3                             # OK: not money-shaped
    timeout = cfg.get("timeout_seconds", 30)            # OK: not money-shaped
    unknown_qty = raw or None                           # OK: stays absent
    label = self.price_label or ""                      # OK: not a numeric literal
    capped = flags.get("use_price_cap", False)          # OK: bool is not a measurement
    total = len(rows) or 0                              # OK: a count is not money
    last = quote.last if quote.last is not None else None   # OK
    stop = self.min_stop_loss_pct or 0.0                # OK: a percentage, not a measurement
    win = cfg.get("price_target_window_days", 90)       # OK: a span of days
    ratio = cfg.get("profit_ratio", 1.0)                # OK: a ratio
    checked = self.price_drop_checked or 0              # OK: a count
    return (mult, cash, px, name, retries, timeout, unknown_qty, label, capped, total, last,
            stop, win, ratio, checked)
'''


def _fixture_expectations():
    """(bad_lines, ok_lines) as 1-based line numbers, read off the ``# BAD:``/``# OK:`` tags."""
    bad, ok = set(), set()
    for i, text in enumerate(FIXTURE.splitlines(), start=1):
        if "# BAD:" in text:
            bad.add(i)
        elif "# OK:" in text:
            ok.add(i)
    return bad, ok


def test_the_fixture_actually_contains_both_kinds():
    """Guards the guard: an empty fixture would make every assertion below vacuous."""
    bad, ok = _fixture_expectations()
    assert len(bad) >= 12, f"fixture lost its bad cases: {len(bad)}"
    assert len(ok) >= 12, f"fixture lost its good cases: {len(ok)}"
    assert not (bad & ok)


def test_fixture_is_valid_python():
    import ast
    ast.parse(FIXTURE)


def test_checker_flags_exactly_the_known_bad_lines():
    from tests.test_no_zero_coercion import find_zero_coercions

    bad, _ = _fixture_expectations()
    found = {v.line for v in find_zero_coercions(FIXTURE, "fixture.py")}
    assert found == bad, f"missed={sorted(bad - found)} spurious={sorted(found - bad)}"


def test_checker_ignores_every_legitimate_zero():
    from tests.test_no_zero_coercion import find_zero_coercions

    _, ok = _fixture_expectations()
    found = {v.line for v in find_zero_coercions(FIXTURE, "fixture.py")}
    assert not (found & ok), f"legitimate zeros flagged: {sorted(found & ok)}"


def test_a_violation_carries_the_file_the_line_and_the_source():
    """The report has to be actionable without opening the file."""
    from tests.test_no_zero_coercion import find_zero_coercions

    v = [x for x in find_zero_coercions(FIXTURE, "fixture.py") if "strike" in x.snippet][0]
    assert v.path == "fixture.py"
    assert isinstance(v.line, int) and v.line > 0
    assert "or 0.0" in v.snippet
    assert v.name == "strike"


def test_checker_reads_a_real_file_from_disk(tmp_path):
    """The tree scan goes through a path, not a string; exercise that seam too."""
    from tests.test_no_zero_coercion import find_zero_coercions_in_file

    p = tmp_path / "money.py"
    p.write_text("def f(o):\n    return float(o.strike or 0.0)\n", encoding="utf-8")
    found = find_zero_coercions_in_file(p)
    assert [v.line for v in found] == [2]


def test_checker_survives_a_file_it_cannot_parse(tmp_path):
    """A syntax error is somebody else's failing test, not a reason for the lint to explode."""
    from tests.test_no_zero_coercion import find_zero_coercions_in_file

    p = tmp_path / "broken.py"
    p.write_text("def f(:\n", encoding="utf-8")
    assert find_zero_coercions_in_file(p) == []


def test_money_vocabulary_is_populated():
    """An emptied vocabulary silently disables the whole rule."""
    from tests.test_no_zero_coercion import MONEY_WORDS

    assert len(MONEY_WORDS) >= 10
    for required in ("qty", "price", "strike", "balance", "premium"):
        assert required in MONEY_WORDS


def test_a_non_money_name_with_a_zero_default_is_not_flagged():
    from tests.test_no_zero_coercion import find_zero_coercions

    assert find_zero_coercions("x = retries or 0\n", "f.py") == []


def test_bool_defaults_are_not_numeric_literals():
    from tests.test_no_zero_coercion import find_zero_coercions

    assert find_zero_coercions("x = d.get('price_ok', False)\n", "f.py") == []
    assert find_zero_coercions("x = enabled_price or True\n", "f.py") == []


def test_dot_get_with_a_money_key_and_a_zero_default_is_flagged():
    from tests.test_no_zero_coercion import find_zero_coercions

    assert len(find_zero_coercions("x = d.get('net_premium', 0.0)\n", "f.py")) == 1


def test_dot_get_with_a_non_numeric_default_is_left_alone():
    from tests.test_no_zero_coercion import find_zero_coercions

    assert find_zero_coercions("x = d.get('price', None)\n", "f.py") == []
    assert find_zero_coercions("x = d.get('price')\n", "f.py") == []


def test_a_percentage_is_vetoed_even_though_it_contains_a_money_word():
    """``price_delta_pct``/``expected_profit_percent`` contain "price"/"profit" but measure
    neither. Without the veto these were 47 of the tree's hits and none was a defect."""
    from tests.test_no_zero_coercion import find_zero_coercions

    assert find_zero_coercions("x = t.get('price_delta_pct', 0)\n", "f.py") == []
    assert find_zero_coercions("x = rec.expected_profit_percent or 0.0\n", "f.py") == []


def test_a_percentage_OF_A_STOCK_OF_MONEY_survives_the_veto():
    """The veto's one carve-out, and it is not theoretical.

    ``ExpertInstance.virtual_equity_pct`` is NOT NULL with a default of 100.0, so
    ``pct or 100.0`` can only ever fire on a REAL user-entered 0 -- turning "give this expert
    nothing" into "give it the entire account". It shipped in four places at once
    (``tests/test_virtual_equity_zero_pct.py``). The first cut of this rule vetoed it on
    ``_pct`` and would have let it come back, so a percentage that names a STOCK of money
    (equity, balance, cash, notional, buying power) is exempt from the veto. Measured cost of
    the carve-out against the whole tree: two hits, both settings defaults.
    """
    from tests.test_no_zero_coercion import find_zero_coercions

    assert len(find_zero_coercions("x = expert.virtual_equity_pct or 100.0\n", "f.py")) == 1
    assert len(find_zero_coercions("x = d.get('cash_balance_pct', 0)\n", "f.py")) == 1


def test_a_percentage_of_a_FLOW_stays_vetoed():
    """A forecast or a price delta expressed as a percentage is still not a measurement of
    money, and un-vetoing those is what put 47 useless hits on the board."""
    from tests.test_no_zero_coercion import find_zero_coercions

    assert find_zero_coercions("x = t.get('price_delta_pct', 0)\n", "f.py") == []
    assert find_zero_coercions("x = rec.expected_profit_percent or 0.0\n", "f.py") == []


def test_the_veto_does_not_swallow_the_plain_money_name():
    """Guards the guard: a veto broad enough to hide ``price`` itself is the rule switched off."""
    from tests.test_no_zero_coercion import find_zero_coercions

    assert len(find_zero_coercions("x = quote.price or 0.0\n", "f.py")) == 1


def test_known_blind_spot_a_money_value_with_no_money_word_in_its_name():
    """PINNED, not fixed. ``quote.last`` and ``bar.close`` are prices, but the only vocabulary
    that would catch them ("last", "close") matches hundreds of non-money names. The rule
    trades this recall for the precision that keeps it switched on; a reviewer reading a report
    should know the gap is deliberate rather than assume the file is clean."""
    from tests.test_no_zero_coercion import find_zero_coercions

    assert find_zero_coercions("x = quote.last or 0.0\n", "f.py") == []
    assert find_zero_coercions("x = bar.close or 0.0\n", "f.py") == []


def test_getattr_with_a_numeric_default_is_flagged():
    """``float(getattr(order, 'filled_qty', 0) or 0)`` is the same coercion wearing getattr."""
    from tests.test_no_zero_coercion import find_zero_coercions

    assert len(find_zero_coercions("x = getattr(o, 'filled_qty', 0)\n", "f.py")) == 1
    assert find_zero_coercions("x = getattr(o, 'name', 0)\n", "f.py") == []


def test_a_negative_literal_default_is_still_a_coercion():
    from tests.test_no_zero_coercion import find_zero_coercions

    assert len(find_zero_coercions("x = pos.quantity or -1\n", "f.py")) == 1


def test_the_scan_roots_all_exist():
    """A typo'd root makes the rule scan nothing and pass forever."""
    from tests.test_no_zero_coercion import REPO_ROOT, SCAN_ROOTS

    assert SCAN_ROOTS
    for root in SCAN_ROOTS:
        assert (REPO_ROOT / root).is_dir(), f"scan root missing: {root}"


def test_the_scan_actually_visits_files():
    """If the walk finds no files the rule passes vacuously."""
    from tests.test_no_zero_coercion import iter_scanned_files

    assert len(list(iter_scanned_files())) > 200


# --------------------------------------------------------------------------- #
# The allowlist is itself a thing that can rot.
# --------------------------------------------------------------------------- #

def test_the_justification_guard_rejects_a_silent_exemption():
    """Tests the GUARD, on synthetic input, so gutting the guard cannot pass unnoticed."""
    from tests.test_no_zero_coercion import unjustified_entries

    assert unjustified_entries({"a.py:1": ""}) == ["a.py:1"]
    assert unjustified_entries({"a.py:1": "   "}) == ["a.py:1"]
    assert unjustified_entries({"a.py:1": "because"}) == ["a.py:1"], "too short to be a reason"
    assert unjustified_entries({"a.py:1": None}) == ["a.py:1"]
    assert unjustified_entries({"a.py:1": "the broker reports a measured zero cash balance"}) == []


def test_the_staleness_guard_spots_an_exemption_that_outlived_its_site():
    from tests.test_no_zero_coercion import stale_entries

    assert stale_entries({"a.py:1": "why"}, ["a.py:2"]) == ["a.py:1"]
    assert stale_entries({"a.py:1": "why"}, ["a.py:1"]) == []


def test_every_allowlist_entry_carries_a_justification():
    """An allowlist you can add to silently is not a guard."""
    from tests.test_no_zero_coercion import ALLOWLIST, unjustified_entries

    bad = unjustified_entries(ALLOWLIST)
    assert not bad, f"allowlisted with no justification: {bad}"


def test_allowlist_keys_are_exact_file_colon_line():
    from tests.test_no_zero_coercion import ALLOWLIST, REPO_ROOT

    for site in ALLOWLIST:
        path, _, line = site.rpartition(":")
        assert line.isdigit(), f"{site} is not path:line"
        assert (REPO_ROOT / path).is_file(), f"{site} points at no such file"


def test_no_allowlist_entry_is_stale():
    """A site that no longer coerces must lose its exemption, or the list becomes a place
    where blanket permission accumulates."""
    from tests.test_no_zero_coercion import ALLOWLIST, scan_repo, stale_entries

    live = {f"{v.path}:{v.line}" for v in scan_repo(apply_allowlist=False)}
    stale = stale_entries(ALLOWLIST, live)
    assert not stale, f"allowlisted sites that no longer coerce (re-point or delete): {stale}"


# --------------------------------------------------------------------------- #
# The rule.
# --------------------------------------------------------------------------- #

_FIX_HINT = (
    "Use ba2_common.core.failure_modes.must_measure(value, 'what this is') -- it is shorter "
    "than the coercion and names what was unknown. If the zero is genuinely MEASURED, add the "
    "exact file:line to ALLOWLIST with a one-line justification."
)


def test_no_new_unknown_reads_as_zero():
    """THE RULE. A file may not grow a coercion it did not already have."""
    from tests.test_no_zero_coercion import BASELINE, scan_counts_by_file, scan_repo

    counts = scan_counts_by_file()
    over = {p: (n, BASELINE.get(p, 0)) for p, n in counts.items() if n > BASELINE.get(p, 0)}
    if not over:
        return
    detail = []
    for v in scan_repo():
        if v.path in over:
            detail.append(f"  {v.path}:{v.line}  {v.name}: {v.snippet}")
    summary = "\n".join(f"  {p}: {n} now, {b} at baseline" for p, (n, b) in sorted(over.items()))
    raise AssertionError(
        "a money/quantity value is newly coerced to a numeric literal.\n"
        f"{summary}\n{_FIX_HINT}\nall coercions in the affected file(s):\n" + "\n".join(detail)
    )


def test_no_unlisted_file_coerces():
    """A file absent from BASELINE gets no headroom at all -- new modules start clean."""
    from tests.test_no_zero_coercion import BASELINE, scan_repo

    fresh = [v for v in scan_repo() if v.path not in BASELINE]
    report = "\n".join(f"  {v.path}:{v.line}  {v.name}: {v.snippet}" for v in fresh)
    assert not fresh, f"{len(fresh)} coercion(s) in file(s) with no baseline.\n{_FIX_HINT}\n{report}"


def test_baseline_has_not_rotted():
    """The register must RATCHET DOWN. A count left higher than reality is headroom somebody can
    spend on a fresh defect without any test noticing."""
    from tests.test_no_zero_coercion import BASELINE, scan_counts_by_file

    counts = scan_counts_by_file()
    slack = {p: (counts.get(p, 0), b) for p, b in BASELINE.items() if counts.get(p, 0) < b}
    assert not slack, (
        "coercions were removed but the baseline was not lowered -- edit BASELINE to the "
        f"current numbers so the headroom cannot be re-spent: {slack}"
    )


def test_baseline_total_is_the_declared_number():
    """One number, stated once, so a reviewer can see the debt move in a diff."""
    from tests.test_no_zero_coercion import BASELINE, BASELINE_TOTAL

    assert sum(BASELINE.values()) == BASELINE_TOTAL


def test_the_baseline_is_debt_and_not_a_blessing():
    """Guards the intent: BASELINE must never be used as a silent allowlist. It is keyed by file
    and carries no justifications precisely BECAUSE nothing in it is claimed to be correct --
    the sample says roughly half of it is a live bug. ALLOWLIST is where a claim goes, and it
    has to be argued per line."""
    from tests.test_no_zero_coercion import ALLOWLIST, BASELINE

    # BASELINE carries counts, never prose: there is no field in which to write "this is fine".
    assert all(isinstance(v, int) and v > 0 for v in BASELINE.values())
    # ALLOWLIST carries prose, never counts: every exemption has to be argued.
    assert all(isinstance(v, str) for v in ALLOWLIST.values())
    # And it must stay the small, hand-argued list -- if it ever approaches the debt register's
    # size, the exemption mechanism has become the parking mechanism.
    assert len(ALLOWLIST) < 40, "ALLOWLIST is being used to park findings; that is what BASELINE is for"
