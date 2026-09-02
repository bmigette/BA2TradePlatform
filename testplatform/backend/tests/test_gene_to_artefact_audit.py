"""EVERY option gene must land in an artefact a LIVE ExpertInstance loads. Nothing else.

THE OPERATOR PRINCIPLE (standing, restated 2026-09-02): backtest and live run the SAME code
and behave 100% identically, so a GA gene may land in exactly TWO places --

  1. a rule / action / condition parameter inside the EMITTED RULESET, or
  2. a key in the EXPERT SETTINGS dict,

because those two artefacts are what a live ExpertInstance is deployed with (rulesets through
``ba2_common.core.rules_convert``'s shared converters, settings through ``save_settings``).
A gene consumed anywhere else -- a launcher-only knob, a testplatform-only config key, a
parameter the shared converter silently drops -- is a gene whose backtest result cannot be
reproduced live. Fixed (unsearched) choices are ordinary ruleset parameters and are covered by
the same rule. GA-LEVEL knobs are the ONLY exception, and each is named in ``_ALLOWLIST``.

HOW THIS TEST CHECKS IT -- and why the FIRST version of it did not bite
======================================================================
The first version set every gene to its domain maximum in ONE genome and asked whether that
value appeared ANYWHERE in the emitted artefact (bag membership over every scalar). Reviewer
finding, 2026-09-02: deleting ``("w_rvol", ("option_w_rvol",))`` from
``rule_builders._OPTION_ACTION_PARAM_KEYS`` -- a searched gene that then reaches NOTHING --
left all 59 cases GREEN, because ``w_premium`` and ``w_iv`` happened to carry the same value
2.0 and the bag still contained it. A bag membership test cannot tell "this gene arrived" from
"some other gene arrived with the same number".

So the audit now varies ONE GENE AT A TIME to a SENTINEL -- a value inside that gene's own
band which no other gene in the genome holds -- and asserts the sentinel appears at that
gene's MAPPED DESTINATION KEY (the specific action param / trigger value / settings key the
shared converter forwards it to), not anywhere in the artefact. A gene with no known
destination FAILS: that is the fail-closed half, and it is what the w_rvol mutation trips.

Per (key, expert) case:

  a. build the gene space EXACTLY as ``ba2test_launcher``'s ``optimize`` command does
     (``spec["expert_params"]`` + ``_rm_opt_for(key)`` + the ``schedule:`` block, and the
     ``screener:`` block when the case is screener-enabled, split into model/screener/schedule
     the way ``strategy_optimization_handler`` splits it), so no gene the launcher can emit is
     missing from the audit;
  b. decode a NON-DEFAULT base genome through the real ``decode_params`` +
     ``_build_daily_trial_config`` (the whitelist trap: a knob missing from that rebuild is
     inert while every log says it works);
  c. convert the decoded rules through ``rules_convert.trade_rules_to_live_export`` -- the REAL
     live-export artefact, not the intermediate rule dict, which still carries every authoring
     key (``option_*_optimize``, ``value_min``/``value_max``) and would match a gene against
     the domain metadata it came from;
  d. re-decode once per gene with that gene's sentinel and look ONLY at its destination.

TOGGLE genes (``*:enabled``) carry no value to find -- their non-default value IS removal -- so
they are audited by EFFECT in ``test_every_toggle_gene_changes_the_emitted_ruleset``.

``option_dte`` is the one DERIVED gene: it decodes as the window CENTRE and the action carries
``dte_min``/``dte_max`` = centre -/+ the authored half-width (``_apply_option_dte``). The audit
re-derives that arithmetic rather than looking for the centre literally.
"""
import importlib
import importlib.util
import os
import sys

import pytest

# tests/ -> backend/ -> testplatform/, then the launcher beside backend/. Resolved off
# __file__ (see test_option_grid_foundations.py for why a CWD-relative path is wrong here).
_LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_audit", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_audit"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


# ==================================================================================================
# THE ALLOWLIST -- one line of reason each. Nothing else may consume a gene.
# ==================================================================================================
#: Gene-name PREFIXES whose consumer is deliberately NOT the ruleset and NOT the expert settings.
#: Every entry is a run-level knob in the sense the operator principle exempts. Adding an entry
#: here is a REVIEWED decision, not a way to make this test pass -- which is why
#: ``test_the_allowlist_is_exactly_this`` asserts the DICT, not a size bound.
_ALLOWLIST = {
    # Which weekdays the run SCANS for entries. Run-level cadence, not strategy logic: it lands
    # on the trial config as ``run_schedule_override``, and its live analogue is the
    # ExpertInstance's JobManager schedule (when the expert is asked for an analysis at all),
    # which is configured on the instance, not inside a ruleset or a settings key.
    "schedule:": "run-level entry-scan cadence; live analogue is the ExpertInstance job schedule",
}
# ``screener:`` is NOT here, deliberately (reviewer finding 2026-09-02). Those six genes were
# outside the first audit only because it built every space with ``screener_cfg=None``; they are
# real strategy knobs and they ARE traceable -- to ``trial["screener_runtime"]["settings"]``, the
# per-day universe gate the engine applies -- so the audit now traces them instead.


def _allowlisted(gene):
    for prefix, reason in _ALLOWLIST.items():
        if gene.startswith(prefix):
            return reason
    return None


# ==================================================================================================
# The keys under audit
# ==================================================================================================
def _option_keys(m):
    """Every OPTION strategy key ``_STRATEGY_BUILDERS`` can actually build.

    Phase-gated keys are excluded because their builder REFUSES (``_build_strategy_phase_gated``)
    -- there is no genome to audit until the two-expiry work lands. Group MEMBERS need no row of
    their own: a group's built Strategy carries every member's entry rule, so auditing the group
    key audits every member's genes (asserted by ``test_the_audit_covers_every_group_member``)."""
    return sorted(k for k in m._STRATEGY_BUILDERS
                  if k in m._OPTION_STRATEGY_KEYS
                  and k not in m._PHASE_GATED_OPTION_STRATEGIES)


OPTION_KEYS = _option_keys(_M)

#: (key, expert, screener_on). Every option key under FMPRating (the grid's default ranking
#: expert); O_ERN under FMPEarningsEvent as well, because that is the expert grid-2 actually
#: pairs it with (design 2026-08-31 S9) and its ``model:`` genes are a different set; and ONE
#: screener-enabled case, which is the only shape that emits the six ``screener:*`` genes
#: (the launcher merges them only under ``--screener``).
AUDIT_CASES = ([(k, "FMPRating", False) for k in OPTION_KEYS]
               + [("O_ERN", "FMPEarningsEvent", False),
                  ("O_LC", "FMPRating", True)])


# ==================================================================================================
# Building the artefacts the way a real run does
# ==================================================================================================
def _launcher_expert_cfg(m, key, expert, screener):
    """The ``expert_params`` block the launcher's ``optimize`` command assembles for a NON-bypass
    run (ba2test_launcher.py, the ``cfg = {...}`` dict), with the ``screener:`` block merged in
    exactly when ``--screener`` would have been passed."""
    spec = m._EXPERT_OPT[expert]
    cfg = {**spec["expert_params"], **m._rm_opt_for(key),
           **{f"schedule:{k}": v for k, v in m._SCHEDULE_DAY_OPT.items()}}
    if screener:
        cfg.update({f"screener:{k}": v for k, v in m._SCREENER_OPT.items()})
    return cfg


def _space(m, key, expert, screener=False):
    """``collect_param_space`` fed exactly what ``strategy_optimization_handler`` feeds it."""
    from app.services.strategy_param_space import collect_param_space

    strat = m._build_strategy(key, f"audit-{key}", expert)
    cfg = _launcher_expert_cfg(m, key, expert, screener)
    model_cfg = {k: v for k, v in cfg.items()
                 if not k.startswith("screener:") and not k.startswith("schedule:")}
    screener_cfg = {k[len("screener:"):]: v for k, v in cfg.items()
                    if k.startswith("screener:")} or None
    schedule_cfg = {k[len("schedule:"):]: v for k, v in cfg.items()
                    if k.startswith("schedule:")} or None
    return strat, collect_param_space(strat, expert_cfg=model_cfg, screener_cfg=screener_cfg,
                                      schedule_cfg=schedule_cfg)


def _non_default(domain, choice_index=-1):
    """The BASE value for one gene: the top of its domain, and ``choice_index`` for a
    categorical (-1 = the last choice, i.e. never the authored default; 0 = the first, used
    only as the conditional-domain second pass).

    Toggle genes are 0/1 int ranges and land on 1 here -- ON, so every rule and every leaf is
    present for the per-gene sentinel pass to address."""
    if domain["type"] == "choice":
        return domain["choices"][choice_index]
    if domain["type"] == "int":
        return int(domain["max"])
    return float(domain["max"])


def _numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sentinel(domain, taken):
    """A value INSIDE this gene's own band that NO other gene in the genome holds.

    Walks the gene's own grid down from its maximum and takes the first level that is not
    already in ``taken``. That keeps the probe a value a real genome could actually produce --
    an off-grid or out-of-band number would prove the plumbing forwards arbitrary floats, not
    that it forwards THIS gene. Returns None when the whole band is taken, which the caller
    reports rather than silently probing with an ambiguous value."""
    if domain["type"] == "choice":
        for c in reversed(list(domain["choices"])):
            if c not in taken:
                return c
        return None
    lo, hi = float(domain["min"]), float(domain["max"])
    step = float(domain["step"]) if domain["step"] else 1.0
    is_int = domain["type"] == "int"
    v = hi
    while v >= lo - 1e-9:
        cand = int(round(v)) if is_int else round(v, 10)
        if not any(_numeric(t) and abs(float(t) - float(cand)) <= 1e-9 for t in taken):
            return cand
        v -= step
    return None


def _backtest_cfg(strat, expert):
    return {
        "backtest_id": "gene-audit", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": expert, "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
        "options_store": "parquet",
    }


def _hoisted(screener):
    """The run-level hoisted state ``_build_daily_trial_config`` reads for a screener run.

    The store PATH need not exist: the only place the file is opened is the candidate-bound
    optimisation, which is wrapped in its own try/except and falls back to the full band. The
    ``screener_runtime`` block this audit traces to is built before that, from
    ``normalize_screener_settings`` alone."""
    if not screener:
        return None
    return {"screener_store": os.path.join(os.path.dirname(_LAUNCHER_PATH),
                                           "no-such-metric-store.parquet"),
            "screener_base": {}, "screener_cadence_days": 7}


def _trial(m, strat, expert, genome, screener=False):
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    decoded = decode_params(strat, genome)
    return decoded, _build_daily_trial_config(_backtest_cfg(strat, expert), decoded,
                                              _hoisted(screener))


def _export(entry_rules, exit_rules):
    """The decoded rules as a LIVE EXPORT FILE -- through the shared converters, i.e. the exact
    artefact ``tools/import_deploy_payload.py`` writes onto an ExpertInstance."""
    from ba2_common.core.rules_convert import trade_rules_to_live_export

    return trade_rules_to_live_export(entry_rules=entry_rules or [],
                                      exit_rules=exit_rules or [])


def _export_rules(export):
    return [r for rs in export["rulesets"] for r in rs["rules"]]


def _export_one(rule, ns):
    """ONE decoded TradeRule through the shared converters, on its own.

    Per-rule rather than "find it again in the whole export" because the exporter NAMES a rule
    from its ``name`` key and several exit rules (``cc_sell``, the overlay guards) carry only an
    ``id`` -- matching by name would silently look at the wrong rule, or at none."""
    rules = _export_rules(_export([rule] if ns == "entry" else [],
                                  [] if ns == "entry" else [rule]))
    return rules[0] if rules else None


def _exported_action(rule, ns, aidx):
    """The exported ACTION dict a gene addresses. ``live_actions_from_trade_rule`` keys actions
    ``a0``..``aN`` in order; a single-action rule is looked up by index OR as the only entry, so
    a legacy ``act`` key (the one-action-per-rule shape) resolves too."""
    exported = _export_one(rule, ns)
    if exported is None:
        return None
    actions = exported["actions"]
    if f"a{aidx}" in actions:
        return actions[f"a{aidx}"]
    return list(actions.values())[aidx] if len(actions) > aidx else None


def _trigger_values(rule, ns, event_type):
    exported = _export_one(rule, ns)
    if exported is None:
        return []
    return [t["value"] for t in exported["triggers"].values()
            if t.get("event_type") == event_type and "value" in t]


def _find_leaf(rules, cid):
    """(the rule carrying leaf ``cid``, the leaf) across one decoded rule list."""
    def walk(node):
        if not isinstance(node, dict):
            return None
        if node.get("id") == cid and node.get("field"):
            return node
        for child in (node.get("conditions") or []):
            hit = walk(child)
            if hit is not None:
                return hit
        return None

    for rule in rules or []:
        hit = walk(rule.get("conditions"))
        if hit is not None:
            return rule, hit
    return None, None


def _rule_by_id(rules, rid):
    for rule in rules or []:
        if rule.get("id") == rid:
            return rule
    return None


def _event_type_for_field(field):
    from ba2_common.core.rule_builders import FIELD_EVENT, FLAG_FIELD_EVENT

    ev = FIELD_EVENT.get(field) or FLAG_FIELD_EVENT.get(field)
    return ev.value if ev is not None else None


# ==================================================================================================
# THE DESTINATION MAP -- where each gene is SUPPOSED to arrive
# ==================================================================================================
#: Genes whose destination key is not simply the converter's forwarding table entry.
_SPECIAL_ACTION_DESTINATION = {
    # The STRUCTURE gene is the action TYPE itself (``_decode_rule_list`` writes action_type).
    "option_structure": "action_type",
    # Both per-leg delta genes are written onto ``option_strike_param`` by
    # ``_apply_option_strike`` (as a scalar, or as the ``[long, short]`` pair), and the
    # converter forwards that field as ``strike_param``.
    "option_strike_delta": "strike_param",
    "option_strike_delta_long": "strike_param",
    # An exit rule's adjust value: ``action_from_rule`` writes it as ``value``.
    "action_value": "value",
}
#: The DERIVED gene -- checked by re-deriving _apply_option_dte's arithmetic, not by lookup.
_DERIVED_DTE = "option_dte"


def _action_destination(field):
    """The exported ACTION key the shared converter forwards this gene field to, or None.

    DERIVED FROM ``rule_builders._OPTION_ACTION_PARAM_KEYS`` -- the converter's OWN forwarding
    table -- rather than hand-listed, and that is the point: a gene whose source key is not in
    that table reaches nothing, and this function returns None for it, which the audit reports
    as a failure. Deleting a row from that table (the reviewer's w_rvol mutation) is therefore
    caught here rather than absorbed by a bag membership test."""
    if field in _SPECIAL_ACTION_DESTINATION:
        return _SPECIAL_ACTION_DESTINATION[field]
    from ba2_common.core.rule_builders import _OPTION_ACTION_PARAM_KEYS

    for cfg_key, source_keys in _OPTION_ACTION_PARAM_KEYS:
        if field in source_keys:
            return cfg_key
    return None


def _declared_settings(expert_name):
    """Every settings key this expert actually DECLARES -- its own definitions plus the shared
    ``MarketExpertInterface`` builtins (trading permissions, RM sizing, schedules).

    A ``model:`` gene whose key is in neither is a gene the expert never reads: it would be
    written into the trial's settings dict (``_build_daily_trial_config`` copies every override
    verbatim, which is why asserting it arrives there is tautological) and then ignored by both
    runtimes -- and, on a live deploy, ``save_settings`` would carry a key with no definition."""
    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS

    cls = getattr(importlib.import_module(_SUPPORTED_EXPERTS[expert_name]), expert_name)
    cls._ensure_builtin_settings()
    return set(cls.get_settings_definitions()) | set(cls._builtin_settings)


def _at_destination(value, arrived):
    """``value`` is AT ``arrived`` -- the value found at the gene's destination key.

    A list is accepted by membership because one destination legitimately holds a PAIR: the
    per-leg backspread deltas share ``strike_param`` as ``[long, short]``."""
    candidates = list(arrived) if isinstance(arrived, (list, tuple)) else [arrived]
    for c in candidates:
        if _numeric(value) and _numeric(c) and abs(float(c) - float(value)) <= 1e-9:
            return True
        if c == value:
            return True
    return False


# ==================================================================================================
# THE AUDIT
# ==================================================================================================
def _check_gene(m, key, expert, strat, base, gene, domain, screener):
    """Vary ONE gene to a sentinel and follow it to its destination. Returns a complaint or None."""
    from app.services.strategy_param_space import decode_params

    taken = [v for g, v in base.items() if g != gene]
    probe = _sentinel(domain, taken)
    if probe is None:
        return (f"{gene}: every level of its band is already held by another gene, so no "
                f"unambiguous sentinel exists -- widen the band or split the case")
    genome = dict(base, **{gene: probe})
    ns = gene.split(":", 1)[0]

    if ns == "model":
        name = gene[len("model:"):]
        declared = _declared_settings(expert)
        if name not in declared:
            return (f"{gene}: {expert} declares no setting {name!r} (neither in "
                    f"get_settings_definitions() nor in the MarketExpertInterface builtins), "
                    f"so the gene is written into the trial's settings and read by nothing")
        decoded = decode_params(strat, genome)
        arrived = (decoded["expert_overrides"] or {}).get(name, _MISSING)
        if not _at_destination(probe, arrived):
            return f"{gene}={probe!r} did not arrive at expert setting {name!r} ({arrived!r})"
        return None

    if ns == "screener":
        name = gene[len("screener:"):]
        _decoded, trial = _trial(m, strat, expert, genome, screener=True)
        runtime = trial.get("screener_runtime")
        if not runtime:
            return (f"{gene}: the trial config carries no screener_runtime block, so the "
                    f"per-day universe gate never sees this gene")
        dest = name[len("screener_"):] if name.startswith("screener_") else name
        arrived = runtime["settings"].get(dest, _MISSING)
        if not _at_destination(probe, arrived):
            return (f"{gene}={probe!r} did not arrive at screener_runtime.settings[{dest!r}] "
                    f"({arrived!r}) -- normalize_screener_settings drops keys the store does "
                    f"not use, so this gene gates nothing")
        return None

    decoded = decode_params(strat, genome)

    if ns == "cond":
        _, cid, field = gene.split(":", 2)
        for sub_ns in ("entry", "exit"):
            rule, leaf = _find_leaf(decoded[f"{sub_ns}_rules"], cid)
            if leaf is not None:
                break
        if leaf is None:
            return f"{gene}: no leaf {cid!r} survives in the decoded ruleset"
        if field != "value":
            return (f"{gene}: this audit has no destination mapping for a cond gene field "
                    f"{field!r}; map it or prove it is GA-level and allowlist it")
        ev = _event_type_for_field(leaf["field"])
        if ev is None:
            return (f"{gene}: field {leaf['field']!r} maps to no ExpertEventType, so the "
                    f"shared converter DROPS the leaf and the gene reaches nothing")
        emitted = _trigger_values(rule, sub_ns, ev)
        if not _at_destination(probe, emitted):
            return (f"{gene}={probe!r} is at no emitted {ev} trigger value (emitted: {emitted})")
        return None

    if ns in ("entry", "exit"):
        _, rid, rest = gene.split(":", 2)
        aidx = int(rest.split(":")[0][1:])
        field = rest.split(":", 1)[1]
        rule = _rule_by_id(decoded[f"{ns}_rules"], rid)
        if rule is None:
            return f"{gene}: rule {rid!r} does not survive decode"
        action = _exported_action(rule, ns, aidx)
        if action is None:
            return f"{gene}: rule {rid!r} action a{aidx} is not in the emitted export at all"
        if field == _DERIVED_DTE:
            # DERIVED, the ONE gene whose value is not written verbatim: it decodes as the
            # window CENTRE and ``_apply_option_dte`` writes dte_min = centre - hw,
            # dte_max = centre + hw with hw = max((authored max - authored min)//2, 7).
            authored = _authored_action(m, key, expert, rid, aidx)
            hw = max(int((authored["option_dte_max"] - authored["option_dte_min"]) // 2), 7)
            want = (int(probe) - hw, int(probe) + hw)
            got = (action.get("dte_min"), action.get("dte_max"))
            if got != want:
                return (f"{gene}={probe!r} (window centre, hw={hw}) should emit "
                        f"dte_min/dte_max {want}; emitted {got}")
            return None
        dest = _action_destination(field)
        if dest is None:
            return (f"{gene}: no destination -- {field!r} is in neither "
                    f"rule_builders._OPTION_ACTION_PARAM_KEYS nor this audit's special map, so "
                    f"the shared converter forwards it NOWHERE and the GA searches a knob the "
                    f"simulation cannot see")
        arrived = action.get(dest, _MISSING)
        if not _at_destination(probe, arrived):
            return (f"{gene}={probe!r} did not arrive at action[{dest!r}] of rule {rid!r} "
                    f"({arrived!r})")
        return None

    if ns == "optsel":
        _, half, weight = gene.split(":", 2)
        dest = _action_destination(f"option_{weight}")
        if dest is None:
            return (f"{gene}: no destination -- ``option_{weight}`` is not in "
                    f"rule_builders._OPTION_ACTION_PARAM_KEYS, so the shared converter drops "
                    f"the weight and the selection policy never sees this gene")
        hits = []
        for sub_ns in ("entry", "exit"):
            for rule in decoded[f"{sub_ns}_rules"] or []:
                for aidx, a in enumerate(x for x in (rule.get("actions") or [])
                                         if isinstance(x, dict)):
                    if a.get("option_selection_half") != half:
                        continue
                    action = _exported_action(rule, sub_ns, aidx)
                    if action is not None:
                        hits.append(action.get(dest, _MISSING))
        if not hits:
            return f"{gene}: no emitted action is stamped option_selection_half={half!r}"
        if not all(_at_destination(probe, h) for h in hits):
            return (f"{gene}={probe!r} did not arrive at action[{dest!r}] on every {half} "
                    f"action ({hits!r})")
        return None

    return (f"{gene}: unknown gene namespace {ns!r} -- not a ruleset parameter, not an expert "
            f"setting, and not on the GA-level allowlist")


class _Missing:
    def __repr__(self):
        return "<absent>"


_MISSING = _Missing()


def _authored_action(m, key, expert, rid, aidx):
    """The TEMPLATE (pre-decode) action a gene addresses -- the source of the authored DTE
    window ``_apply_option_dte`` takes its half-width from."""
    strat = m._build_strategy(key, f"audit-{key}", expert)
    for ns in ("entry_rules", "exit_rules"):
        for rule in getattr(strat, ns, None) or []:
            if rule.get("id") == rid:
                return [a for a in rule["actions"] if isinstance(a, dict)][aidx]
    raise AssertionError(f"{key}: no template rule {rid!r}")


@pytest.mark.parametrize("key,expert,screener", AUDIT_CASES,
                         ids=[f"{k}|{e}{'|screener' if s else ''}"
                              for k, e, s in AUDIT_CASES])
def test_every_gene_lands_at_its_own_destination(key, expert, screener):
    """CONDITIONAL DOMAINS, and why a failing gene gets a second look.

    A categorical gene selects which OTHER genes are read: under ``option_strike_method=delta``
    the percent-OTM ``option_strike_param`` is not consumed (``_apply_option_strike`` writes the
    delta instead), and under ``option_structure=open_straddle`` the strangle's width is ignored
    (an ATM builder). Those are conditional domains, not dead genes -- the same distinction the
    O_ERN row records in the launcher -- so a gene that fails under the last-choice base is
    retried under the first-choice base. A gene that lands under NEITHER is consumed nowhere at
    all, and that is the failure this test exists to catch."""
    m = _M
    strat, space = _space(m, key, expert, screener)
    bases = [{g: _non_default(d, choice) for g, d in space.items()} for choice in (-1, 0)]

    # ONE full trial-config build per case: the rules the ENGINE receives must be the decoded
    # rules verbatim, or the per-gene export below is the artefact of some later rebuild.
    decoded, trial = _trial(m, strat, expert, bases[0], screener)
    assert trial["entry_rules"] == decoded["entry_rules"]
    assert trial["exit_rules"] == decoded["exit_rules"]

    audited, bad = 0, {}
    for gene, domain in sorted(space.items()):
        if _allowlisted(gene) or gene.endswith(":enabled"):
            continue  # allowlisted, or audited by EFFECT in the toggle test below
        audited += 1
        complaint = _check_gene(m, key, expert, strat, bases[0], gene, domain, screener)
        if complaint is not None:
            complaint = _check_gene(m, key, expert, strat, bases[1], gene, domain, screener)
        if complaint is not None:
            bad[gene] = complaint

    assert audited, f"{key}: no gene was audited at all -- the space is empty or all skipped"
    assert not bad, (
        f"{key}: {len(bad)} of {audited} gene(s) do not arrive at their own destination in the "
        f"emitted ruleset or the expert settings, under EITHER categorical branch:\n  "
        + "\n  ".join(bad.values()))


@pytest.mark.parametrize("key", OPTION_KEYS)
def test_every_toggle_gene_changes_the_emitted_ruleset(key):
    """A ``*:enabled`` gene's value is structural: switching it off must remove something from
    the LIVE EXPORT. A toggle whose export is byte-identical either way is a knob the live
    ruleset cannot express -- the same defect ``enabled: False`` had before 64981161."""
    from app.services.strategy_param_space import decode_params

    m = _M
    expert = "FMPRating"
    strat, space = _space(m, key, expert)
    genome = {g: _non_default(d) for g, d in space.items()}
    base = _export(*[decode_params(strat, genome)[k] for k in ("entry_rules", "exit_rules")])

    inert = []
    for gene in sorted(g for g in space if g.endswith(":enabled")):
        off = dict(genome)
        off[gene] = 0
        decoded = decode_params(strat, off)
        alt = _export(decoded["entry_rules"], decoded["exit_rules"])
        if alt == base:
            inert.append(gene)
    assert not inert, (
        f"{key}: toggling these genes OFF changes nothing in the emitted live export, so the "
        f"GA is searching a switch the ruleset does not carry: {inert}")


def test_the_audit_covers_every_group_member():
    """A group key's built Strategy must carry one entry rule per member, so auditing the group
    key really does audit every member's genes (no member is silently unaudited)."""
    m = _M
    for group, members in m._OPTION_GROUPS.items():
        if group not in OPTION_KEYS:
            continue
        strat = m._build_strategy(group, f"audit-{group}", "FMPRating")
        ids = {r["id"] for r in strat.entry_rules}
        assert ids == {f"{x.lower()}-entry" for x in members}, (
            f"{group}: entry rules {sorted(ids)} do not cover members {members}"
        )


def test_the_case_list_is_exactly_what_the_launcher_tables_produce():
    """COMPUTED, not a magic number: the count is re-derived here from the launcher's own key
    tables, so a deliberate change (the 2026-09-02 O_LEAP merge collapsed two singles into one
    group key) moves it by an arithmetic anyone can check, and an ACCIDENTAL change -- a key
    quietly leaving _STRATEGY_BUILDERS, or a new one arriving unaudited -- fails.

        launchable option keys = _STRATEGY_BUILDERS & _OPTION_STRATEGY_KEYS - phase-gated
                               = 15 grid-1 singles (O_LC O_LP O_VERT O_BF O_BULLCS O_BEARCS
                                 O_BULLPS O_CSP O_IC O_JL O_RS O_SSTD O_SSTG O_STRD O_STRG)
                               +  3 grid-2 singles (O_ERN O_CBS O_PBS)
                               +  6 group/composite keys (OS1 OS2 OS3 OS4 O_CONVEX O_LEAP)
                               +  3 equity-entry/overlay keys (O_CC O_PP O_STK)
                               +  1 wheel (O_WHEEL)
                               = 28
        cases = 28 x FMPRating + O_ERN|FMPEarningsEvent + O_LC|screener = 30
    """
    m = _M
    expected_keys = sorted((set(m._STRATEGY_BUILDERS) & set(m._OPTION_STRATEGY_KEYS))
                           - set(m._PHASE_GATED_OPTION_STRATEGIES))
    assert OPTION_KEYS == expected_keys
    assert len(OPTION_KEYS) == 28, (
        f"the launchable option-key set moved to {len(OPTION_KEYS)}: {OPTION_KEYS}. If that was "
        f"deliberate, update the arithmetic in this docstring in the same commit.")
    assert len(AUDIT_CASES) == len(OPTION_KEYS) + 2
    assert ("O_LEAP", "FMPRating", False) in AUDIT_CASES, "the merged LEAPS key must be audited"
    assert ("O_ERN", "FMPEarningsEvent", False) in AUDIT_CASES
    assert ("O_LC", "FMPRating", True) in AUDIT_CASES, "the screener genes must be audited"


def test_the_screener_case_actually_emits_screener_genes():
    """Guards the screener case against becoming decorative: if ``--screener``'s gene block ever
    stops reaching ``collect_param_space``, the case would pass by auditing nothing."""
    m = _M
    _strat, space = _space(m, "O_LC", "FMPRating", screener=True)
    screener_genes = sorted(g for g in space if g.startswith("screener:"))
    assert len(screener_genes) == len(m._SCREENER_OPT) == 6, screener_genes
    # ... and the non-screener cases genuinely do NOT carry them (the drivers never pass
    # --screener; they use the separate gate-only --screener-gate-store).
    _s2, plain = _space(m, "O_LC", "FMPRating", screener=False)
    assert not [g for g in plain if g.startswith("screener:")]


def test_the_allowlist_is_exactly_this():
    """The allowlist is the ONE escape from the operator principle. Asserted as a SET, not a
    size bound: a bound of "<= 2" on a one-entry dict silently permits a second entry."""
    assert set(_ALLOWLIST) == {"schedule:"}
    for prefix, reason in _ALLOWLIST.items():
        assert prefix.endswith(":"), f"{prefix!r} must be a gene NAMESPACE prefix"
        assert len(reason) > 20, f"{prefix!r} needs a real reason, got {reason!r}"


def test_the_sentinel_is_unique_and_in_band():
    """The probe's own contract, since every assertion above rests on it: inside the gene's
    declared band, on its own grid, and held by no other gene in the genome."""
    m = _M
    _strat, space = _space(m, "O_ERN", "FMPEarningsEvent")
    base = {g: _non_default(d) for g, d in space.items()}
    checked = 0
    for gene, domain in space.items():
        if gene.endswith(":enabled") or domain["type"] == "choice":
            continue
        probe = _sentinel(domain, [v for g, v in base.items() if g != gene])
        assert probe is not None, f"{gene}: no unique sentinel available"
        assert float(domain["min"]) <= float(probe) <= float(domain["max"]), (
            f"{gene}: sentinel {probe} is outside its band")
        assert not any(_numeric(v) and abs(float(v) - float(probe)) <= 1e-9
                       for g, v in base.items() if g != gene), (
            f"{gene}: sentinel {probe} collides with another gene's value")
        checked += 1
    assert checked > 20, f"only {checked} genes exercised the sentinel contract"
