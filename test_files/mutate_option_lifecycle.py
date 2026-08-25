"""Task 6 mutation harness (ad-hoc; NOT collected by pytest).

Applies one textual mutation at a time to
``packages/common/ba2_common/core/option_lifecycle.py``, runs the Task 6 test file,
records which NAMED tests fail, and restores the file byte-identically (verified via
``git hash-object``). A mutation that no test kills is a test that is missing.

Run:  venv/bin/python test_files/mutate_option_lifecycle.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "packages/common/ba2_common/core/option_lifecycle.py"
TESTS = "packages/common/tests/test_option_lifecycle.py"

MUTATIONS = [
    # ---- threshold boundaries and inversions -----------------------------
    ("M01 profit target strict instead of inclusive",
     "if pnl_pct is not None and pnl_pct >= capture:",
     "if pnl_pct is not None and pnl_pct > capture:"),
    ("M02 profit target inverted",
     "if pnl_pct is not None and pnl_pct >= capture:",
     "if pnl_pct is not None and pnl_pct <= capture:"),
    ("M03 credit stop strict instead of inclusive",
     "            if pnl_pct <= limit:",
     "            if pnl_pct < limit:"),
    ("M04 credit stop inverted (fires on winners)",
     "            if pnl_pct <= limit:",
     "            if pnl_pct >= limit:"),
    ("M05 credit stop sign error (positive limit)",
     "            limit = -100.0 * mult",
     "            limit = 100.0 * mult"),
    ("M06 roll window strict instead of inclusive",
     "    if dte is not None and dte <= roll_dte:",
     "    if dte is not None and dte < roll_dte:"),
    ("M07 roll window inverted",
     "    if dte is not None and dte <= roll_dte:",
     "    if dte is not None and dte >= roll_dte:"),
    ("M08 tested delta strict instead of inclusive",
     "        if abs(row.delta) >= threshold:",
     "        if abs(row.delta) > threshold:"),
    ("M09 tested delta inverted",
     "        if abs(row.delta) >= threshold:",
     "        if abs(row.delta) <= threshold:"),
    ("M10 tested delta drops the absolute value",
     "        if abs(row.delta) >= threshold:",
     "        if row.delta >= threshold:"),

    # ---- unknown collapsing back into a safe-looking value ---------------
    ("M11 missing greek reads as untested (the promoted behaviour)",
     "    if blind:\n        return None, \"\", blind\n    return False, \"\", \"\"",
     "    return False, \"\", \"\""),
    ("M12 a short leg absent from the chain reads as untested",
     "            blind = blind or (f\"no chain row for short {leg.contract_symbol} — its \"\n"
     "                              f\"|delta| is unknown\")\n            continue",
     "            continue"),
    ("M13 an unmeasurable P&L becomes 0.0",
     "        if mark is None:\n            return None, (f\"no usable mark for {leg.contract_symbol} (bid/ask/last all \"\n"
     "                          f\"missing) — the structure's P&L is unmeasurable\")",
     "        if mark is None:\n            mark = 0.0"),
    ("M14 an unknown entry premium becomes a 0.0 P&L",
     "    if structure.entry_net_premium is None:\n"
     "        return None, \"entry net premium is unknown — the P&L percent basis is undefined\"",
     "    if structure.entry_net_premium is None:\n        return 0.0, \"\""),
    ("M15 UNKNOWN folded into HOLD",
     "    blind = [b for b in (pnl_blind, tested_blind, dte_blind) if b]\n"
     "    if blind:\n"
     "        return LifecycleDecision(txn, LIFECYCLE_UNKNOWN, \"; \".join(blind), pnl_pct)",
     "    blind = []"),
    ("M16 a missing expiry is silent (empty detail -> reads as hold)",
     "        return None, (\"no expiry on the structure or any of its held legs — the roll \"\n"
     "                      \"window cannot be evaluated\")",
     "        return None, \"\""),
    ("M17 conflicting expiries guess the latest",
     "    if len(candidates) > 1:\n"
     "        listed = \", \".join(str(e) for e in sorted(candidates))\n"
     "        return None, (f\"conflicting expiries on one structure ({listed}) — its DTE is \"\n"
     "                      f\"undefined\")",
     "    if len(candidates) > 1:\n        return (max(candidates) - as_of).days, \"\""),
    ("M18 the legs are ignored, only the parent expiry counts (the dead roll gene)",
     "    candidates = {l.expiry for l in structure.held_legs if l.expiry is not None}",
     "    candidates = set()"),
    ("M19 a structure with no held legs is priced as flat and fine",
     "    if not held:\n        return None, (\"no held option legs — the structure's P&L is unmeasurable\")",
     "    if not held:\n        return 0.0, \"\""),

    # ---- precedence -------------------------------------------------------
    ("M20 the roll window is checked before profit capture",
     "    pnl_pct, pnl_blind = _pnl_pct(structure, chain_by_symbol)",
     "    pnl_pct, pnl_blind = _pnl_pct(structure, chain_by_symbol)\n"
     "    _d, _b = _dte(structure, as_of)\n"
     "    if _d is not None and _d <= int(_require(settings, 'roll_dte')):\n"
     "        return LifecycleDecision(txn, LIFECYCLE_ROLL_DTE,\n"
     "                                 f\"{_d} DTE <= roll_dte {int(_require(settings, 'roll_dte'))}\", pnl_pct)"),
    ("M21 the tested check is run before profit capture and the stop",
     "    pnl_pct, pnl_blind = _pnl_pct(structure, chain_by_symbol)",
     "    pnl_pct, pnl_blind = _pnl_pct(structure, chain_by_symbol)\n"
     "    if _require(settings, 'tested_delta_enabled'):\n"
     "        _t, _td, _tb = _tested(structure, chain_by_symbol, float(_require(settings, 'tested_delta')))\n"
     "        if _t:\n"
     "            return LifecycleDecision(txn, LIFECYCLE_TESTED, _td, pnl_pct)"),
    ("M22 the circuit breaker no longer outranks anything",
     "    if breaker_tripped:\n"
     "        return LifecycleDecision(txn, LIFECYCLE_BREAKER,\n"
     "                                 \"sleeve circuit breaker tripped — flattening the book\",\n"
     "                                 pnl_pct)",
     "    if False:\n        pass"),

    # ---- determinism ------------------------------------------------------
    ("M23 output order follows the caller's iteration order",
     "    ordered = sorted(structures, key=lambda s: s.transaction_id)",
     "    ordered = list(structures)"),

    # ---- marks ------------------------------------------------------------
    ("M24 flatten marks swapped (short at bid, long at ask)",
     "    if leg.is_short:\n"
     "        return row.ask if row.ask is not None else row.last\n"
     "    return row.bid if row.bid is not None else row.last",
     "    if leg.is_short:\n"
     "        return row.bid if row.bid is not None else row.last\n"
     "    return row.ask if row.ask is not None else row.last"),
    ("M25 no `last` fallback when one side of the quote is missing",
     "    if leg.is_short:\n"
     "        return row.ask if row.ask is not None else row.last\n"
     "    return row.bid if row.bid is not None else row.last",
     "    if leg.is_short:\n        return row.ask\n    return row.bid"),
    ("M26 the entry premium sign is flipped",
     "    entry_cash = -structure.entry_net_premium * abs(structure.quantity)",
     "    entry_cash = structure.entry_net_premium * abs(structure.quantity)"),
    ("M27 realised cash from a closed leg is dropped",
     "    amount = (entry_cash + structure.realized_cash + flatten_cash) * structure.multiplier",
     "    amount = (entry_cash + flatten_cash) * structure.multiplier"),
    ("M28 a zero entry premium divides anyway",
     "    if abs(structure.entry_net_premium) < _EPS:\n"
     "        return None, \"entry net premium is 0 — the P&L percent basis is undefined\"",
     "    if False:\n        pass"),

    # ---- which target / which stop ---------------------------------------
    ("M29 a strangle uses the ordinary profit target",
     "    if structure.strategy == \"short_strangle\":",
     "    if False:"),
    ("M30 every structure uses the defined-risk stop",
     "        if structure.strategy in UNDEFINED_RISK_STRATEGIES:",
     "        if False:"),
    ("M31 short_strangle drops out of the undefined-risk list",
     "UNDEFINED_RISK_STRATEGIES = (\"short_put\", \"short_strangle\")",
     "UNDEFINED_RISK_STRATEGIES = (\"short_put\",)"),
    ("M32 the undefined-risk branch falls through to the defined-risk stop",
     "            label, enabled, mult_key = \"ur_stop\", _require(settings, \"ur_stop_enabled\"), \"ur_stop_credit_mult\"",
     "            label, enabled, mult_key = \"dr_stop\", _require(settings, \"dr_stop_enabled\"), \"dr_stop_credit_mult\""),
    ("M33 the tested check ignores its enable flag",
     "    if _require(settings, \"tested_delta_enabled\"):",
     "    if True:"),
    ("M34 the credit stop ignores its enable flag",
     "        if enabled:",
     "        if True:"),
    ("M35 long legs are testable too",
     "        if not leg.is_short:\n            continue",
     "        if False:\n            continue"),
    ("M36 net-flat legs still count as held",
     "        return tuple(sorted((l for l in self.legs if l.is_held),",
     "        return tuple(sorted((l for l in self.legs if True),"),

    # ---- settings ---------------------------------------------------------
    ("M37 a missing setting silently defaults",
     "    if key not in settings:\n"
     "        raise KeyError(\n"
     "            f\"option_lifecycle: required setting {key!r} is missing — refusing to \"\n"
     "            f\"substitute a default for a risk threshold\")\n"
     "    return settings[key]",
     "    return settings[key] if key in settings else 0"),

    # ---- structure_metrics (promoted _txn_metrics) ------------------------
    ("M38 no visible legs reports (True, 0.0, 0.0) -- the all-zeros regime",
     "    if not held:\n"
     "        return StructureMetrics(None, None, None,\n"
     "                                \"no held option legs — the structure's committed \"\n"
     "                                \"capital is unmeasurable\")",
     "    if not held:\n        return StructureMetrics(True, 0.0, 0.0)"),
    ("M39 a missing strike is read as 0.0",
     "        if leg.strike is None:\n"
     "            return StructureMetrics(None, None, None,\n"
     "                                    f\"no strike for {leg.contract_symbol} — notional \"\n"
     "                                    f\"and committed capital are unmeasurable\")",
     "        if leg.strike is None:\n"
     "            object.__setattr__(leg, 'strike', 0.0)"),
    ("M40 width reverts to min(short) - max(long) across all option types",
     "            width = max(abs(s.strike - l.strike) for s in side_shorts for l in side_longs)",
     "            width = min(s.strike for s in shorts) - max(l.strike for l in longs)"),
    ("M41 an uncovered short is treated as covered",
     "        naked = max(0.0, n_short - n_long)",
     "        naked = 0.0"),
    ("M42 everything is reported as defined risk",
     "    return StructureMetrics(not any_naked, notional, committed)",
     "    return StructureMetrics(True, notional, committed)"),
    ("M43 an all-long structure is reported as undefined risk",
     "    if not shorts:\n"
     "        # Every leg is long: the debit already paid is the whole risk, and there is no\n"
     "        # short notional to stress. Distinct from \"no legs visible\", which is unknown.\n"
     "        return StructureMetrics(True, 0.0, 0.0)",
     "    if not shorts:\n        return StructureMetrics(False, 0.0, 0.0)"),

    # ---- round 2 ----------------------------------------------------------
    ("M44 held_legs stops being sorted (nondeterministic per-leg answers)",
     "        return tuple(sorted((l for l in self.legs if l.is_held),\n"
     "                            key=lambda l: l.contract_symbol))",
     "        return tuple(l for l in self.legs if l.is_held)"),
    ("M45 the unknown detail is assembled in an unstable order",
     "        return LifecycleDecision(txn, LIFECYCLE_UNKNOWN, \"; \".join(blind), pnl_pct)",
     "        return LifecycleDecision(txn, LIFECYCLE_UNKNOWN, \"; \".join(reversed(blind)), pnl_pct)"),
    ("M46 LIFECYCLE_TESTED stops being a closing reason",
     "LIFECYCLE_CLOSING_REASONS = (LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_CREDIT_STOP,\n"
     "                             LIFECYCLE_ROLL_DTE, LIFECYCLE_TESTED, LIFECYCLE_BREAKER)",
     "LIFECYCLE_CLOSING_REASONS = (LIFECYCLE_PROFIT_CAPTURE, LIFECYCLE_CREDIT_STOP,\n"
     "                             LIFECYCLE_ROLL_DTE, LIFECYCLE_BREAKER)"),
    ("M47 LIFECYCLE_UNKNOWN becomes a closing reason",
     "                             LIFECYCLE_ROLL_DTE, LIFECYCLE_TESTED, LIFECYCLE_BREAKER)",
     "                             LIFECYCLE_ROLL_DTE, LIFECYCLE_TESTED, LIFECYCLE_BREAKER,\n"
     "                             LIFECYCLE_UNKNOWN)"),
    ("M48 as_of's time component is not dropped",
     "    return as_of.date() if isinstance(as_of, datetime) else as_of",
     "    return as_of"),
    ("M49 pnl_pct is rounded to whole percent",
     "    return round(amount / basis * 100.0, 4), \"\"",
     "    return round(amount / basis * 100.0, 0), \"\""),
    ("M50 holds are dropped from the output instead of reported",
     "    return [_decide_one(s, chain_by_symbol, settings, as_of_date, breaker)\n"
     "            for s in ordered]",
     "    return [d for d in (_decide_one(s, chain_by_symbol, settings, as_of_date, breaker)\n"
     "                        for s in ordered) if d.reason != LIFECYCLE_HOLD]"),
    ("M51 the percent basis takes a signed quantity",
     "    basis = abs(structure.entry_net_premium) * abs(structure.quantity) * structure.multiplier",
     "    basis = abs(structure.entry_net_premium) * structure.quantity * structure.multiplier"),
    ("M52 notional sums the short legs instead of stressing one side",
     "    notional = (max(l.strike for l in shorts) * 100.0\n"
     "                * max(abs(l.net_qty) for l in shorts))",
     "    notional = sum(l.strike * 100.0 * abs(l.net_qty) for l in shorts)"),
    ("M53 the breaker signal is ignored when present",
     "    breaker = bool(settings[SETTING_BREAKER_TRIPPED]) if SETTING_BREAKER_TRIPPED in settings else False",
     "    breaker = False"),
    ("M54 the breaker decision claims a P&L it never measured",
     "                                 \"sleeve circuit breaker tripped — flattening the book\",\n"
     "                                 pnl_pct)",
     "                                 \"sleeve circuit breaker tripped — flattening the book\",\n"
     "                                 0.0)"),
    ("M55 a decision carries no P&L unless it closes",
     "        return LifecycleDecision(txn, LIFECYCLE_ROLL_DTE,\n"
     "                                 f\"{dte} DTE <= roll_dte {roll_dte}\", pnl_pct)",
     "        return LifecycleDecision(txn, LIFECYCLE_ROLL_DTE,\n"
     "                                 f\"{dte} DTE <= roll_dte {roll_dte}\", None)"),

    # ---- round 3 ----------------------------------------------------------
    ("M56 a breaker flatten reports no P&L even when it could measure one",
     "                                 \"sleeve circuit breaker tripped — flattening the book\",\n"
     "                                 pnl_pct)",
     "                                 \"sleeve circuit breaker tripped — flattening the book\",\n"
     "                                 None)"),
    ("M57 a zero contract multiplier divides anyway",
     "    if not structure.multiplier:\n        return None, \"no contract multiplier — the P&L is unmeasurable\"",
     "    if False:\n        pass"),
    ("M58 a zero structure quantity divides anyway",
     "    if structure.quantity is None or abs(structure.quantity) < _EPS:\n"
     "        return None, \"structure quantity is 0 — the P&L percent basis is undefined\"",
     "    if False:\n        pass"),
    ("M59 the breaker signal is read inverted",
     "    breaker = bool(settings[SETTING_BREAKER_TRIPPED]) if SETTING_BREAKER_TRIPPED in settings else False",
     "    breaker = not bool(settings[SETTING_BREAKER_TRIPPED]) if SETTING_BREAKER_TRIPPED in settings else False"),
    ("M60 every short commits the wing width, covered or not",
     "            committed += covered * width * 100.0",
     "            committed += n_short * width * 100.0"),
    ("M61 the tested check reports the LAST blind short instead of the first",
     "            blind = blind or (f\"no delta for short {leg.contract_symbol} — the \"\n"
     "                              f\"tested-delta check is blind\")",
     "            blind = (f\"no delta for short {leg.contract_symbol} — the \"\n"
     "                     f\"tested-delta check is blind\")"),
    ("M62 an absent breaker key is read as tripped",
     "    breaker = bool(settings[SETTING_BREAKER_TRIPPED]) if SETTING_BREAKER_TRIPPED in settings else False",
     "    breaker = bool(settings[SETTING_BREAKER_TRIPPED]) if SETTING_BREAKER_TRIPPED in settings else True"),
]


def run_tests() -> list[str]:
    env_path = ":".join(str(ROOT / "packages" / p) for p in ("common", "providers", "experts"))
    out = subprocess.run(
        [str(ROOT / "venv/bin/python"), "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider",
         "--no-header", "-x" if False else "--tb=no", "-q"],
        cwd=ROOT, capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": f"{env_path}:{ROOT}"},
    )
    failed = []
    for line in out.stdout.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            name = line.split(" ", 1)[1].split(" ")[0]
            failed.append(name.split("::")[-1])
    return failed


def main() -> int:
    pristine = TARGET.read_text()
    base_hash = subprocess.run(["git", "hash-object", str(TARGET)], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
    survivors = []
    for name, old, new in MUTATIONS:
        assert pristine.count(old) == 1, f"{name}: anchor not unique ({pristine.count(old)}x)"
        TARGET.write_text(pristine.replace(old, new, 1))
        try:
            failed = run_tests()
        finally:
            TARGET.write_text(pristine)
        h = subprocess.run(["git", "hash-object", str(TARGET)], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
        assert h == base_hash, f"{name}: restore was not byte-identical!"
        if failed:
            print(f"KILLED  {name}\n         by {len(failed)}: {', '.join(sorted(failed)[:4])}")
        else:
            survivors.append(name)
            print(f"SURVIVED {name}   <-- MISSING TEST")
    print(f"\n{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} killed")
    for s in survivors:
        print(f"  survivor: {s}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
