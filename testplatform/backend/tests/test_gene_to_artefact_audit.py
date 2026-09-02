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
the same rule. GA-LEVEL knobs (fitness metric, population, trade/breadth floors) are the ONLY
exception, and every one of them is named in ``_GA_LEVEL_GENE_ALLOWLIST`` below with a reason.

WHAT THIS TEST DOES, per option strategy key the launcher can emit (singles, GROUP keys, and
-- inside a group's built Strategy -- every member's own rules):

  a. build the gene space EXACTLY as ``ba2test_launcher``'s ``optimize`` command does
     (``spec["expert_params"]`` + ``_rm_opt_for(key)`` + the ``schedule:`` block, split into
     model/screener/schedule the way ``strategy_optimization_handler`` splits it), so no gene
     the launcher can emit is missing from the audit;
  b. decode a NON-DEFAULT genome -- every value gene at its domain MAX (or last choice), every
     toggle gene ON -- through the real ``decode_params`` + ``_build_daily_trial_config``
     (the whitelist trap: a knob missing from that rebuild is inert while every log says it
     works);
  c. convert the decoded rules through ``rules_convert.trade_rules_to_live_export`` -- the
     REAL live-export artefact, not the intermediate rule dict. This is the load-bearing
     choice: the decoded rule dict still carries every authoring key (``option_*_optimize``,
     ``value_min``/``value_max``, ...), so searching IT would match a gene's value against the
     domain metadata it came from and pass for a gene that reaches nothing. The export carries
     ONLY what the converters forward, which is exactly what live gets;
  d. assert each gene's value is in that export (trigger value / action param) or in the trial
     config's expert settings.

TOGGLE genes (``*:enabled``) carry no value to find -- their non-default value IS removal --
so they are audited by EFFECT in ``test_every_toggle_gene_changes_the_emitted_ruleset``:
flipping one to 0 must change the export. A toggle that changes nothing in the export is a
toggle the live ruleset cannot express.

``option_dte`` is the one DERIVED gene: it decodes as the window CENTRE and the action carries
``dte_min``/``dte_max`` = centre -/+ the authored half-width (``_apply_option_dte``). The
audit re-derives that arithmetic rather than looking for the centre literally.
"""
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
#: Every entry is a GA-/run-level knob in the sense the operator principle exempts. Adding an
#: entry here is a REVIEWED decision, not a way to make this test pass.
_GA_LEVEL_GENE_ALLOWLIST = {
    # Which weekdays the run SCANS for entries. Run-level cadence, not strategy logic: it lands
    # on the trial config as ``run_schedule_override``, and its live analogue is the
    # ExpertInstance's JobManager schedule (when the expert is asked for an analysis at all),
    # which is configured on the instance, not inside a ruleset or a settings key.
    "schedule:": "run-level entry-scan cadence; live analogue is the ExpertInstance job schedule",
}


def _allowlisted(gene: str):
    for prefix, reason in _GA_LEVEL_GENE_ALLOWLIST.items():
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


# ==================================================================================================
# Building the artefacts the way a real run does
# ==================================================================================================
def _launcher_expert_cfg(m, key, expert):
    """The ``expert_params`` block the launcher's ``optimize`` command assembles for a NON-bypass
    run without ``--screener`` (ba2test_launcher.py, the ``cfg = {...}`` dict)."""
    spec = m._EXPERT_OPT[expert]
    return {**spec["expert_params"], **m._rm_opt_for(key),
            **{f"schedule:{k}": v for k, v in m._SCHEDULE_DAY_OPT.items()}}


def _space(m, key, expert):
    """``collect_param_space`` fed exactly what ``strategy_optimization_handler`` feeds it."""
    from app.services.strategy_param_space import collect_param_space

    strat = m._build_strategy(key, f"audit-{key}", expert)
    cfg = _launcher_expert_cfg(m, key, expert)
    model_cfg = {k: v for k, v in cfg.items()
                 if not k.startswith("screener:") and not k.startswith("schedule:")}
    schedule_cfg = {k[len("schedule:"):]: v for k, v in cfg.items()
                    if k.startswith("schedule:")} or None
    return strat, collect_param_space(strat, expert_cfg=model_cfg, screener_cfg=None,
                                      schedule_cfg=schedule_cfg)


def _non_default(domain, choice_index=-1):
    """The audited value for one gene: the top of its domain, and ``choice_index`` for a
    categorical (-1 = the last choice, i.e. never the authored default; 0 = the first, used
    only as the conditional-domain second pass).

    Toggle genes are 0/1 int ranges and land on 1 here -- ON, so every rule and every leaf is
    present to be searched for. Their own non-default (0) is the removal
    ``test_every_toggle_gene_changes_the_emitted_ruleset`` asserts."""
    if domain["type"] == "choice":
        return domain["choices"][choice_index]
    if domain["type"] == "int":
        return int(domain["max"])
    return float(domain["max"])


def _backtest_cfg(strat, expert):
    return {
        "backtest_id": "gene-audit", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": expert, "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "entry_action": getattr(strat, "entry_action", None),
        "options_store": "parquet",
    }


def _trial(m, strat, expert, genome):
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    decoded = decode_params(strat, genome)
    return decoded, _build_daily_trial_config(_backtest_cfg(strat, expert), decoded, None)


def _export(entry_rules, exit_rules):
    """The decoded rules as a LIVE EXPORT FILE -- through the shared converters, i.e. the exact
    artefact ``tools/import_deploy_payload.py`` writes onto an ExpertInstance."""
    from ba2_common.core.rules_convert import trade_rules_to_live_export

    return trade_rules_to_live_export(entry_rules=entry_rules or [],
                                      exit_rules=exit_rules or [])


def _scalars(node, out):
    """Every scalar the export carries, flattened (list-valued params -- the per-leg delta pair
    -- contribute their elements)."""
    if isinstance(node, dict):
        for v in node.values():
            _scalars(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _scalars(v, out)
    else:
        out.append(node)
    return out


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


def _action_scalars(rule, ns):
    exported = _export_one(rule, ns)
    return _scalars(exported["actions"], []) if exported else []


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


def _matches(value, scalars):
    """Membership with a float tolerance -- the converters pass values through untouched, but a
    decoded float that went through ``float()``/``round()`` must still compare equal."""
    for s in scalars:
        if isinstance(value, (int, float)) and isinstance(s, (int, float)) \
                and not isinstance(value, bool) and not isinstance(s, bool):
            if abs(float(s) - float(value)) <= 1e-9:
                return True
        elif s == value:
            return True
    return False


# ==================================================================================================
# THE AUDIT
# ==================================================================================================
def _audit_one_genome(m, key, expert, strat, space, genome):
    """Audit ONE decoded genome; returns {gene: complaint} for the genes that landed nowhere."""
    decoded, trial = _trial(m, strat, expert, genome)

    # The rules the ENGINE receives are the decoded rules verbatim -- pinned here so the export
    # below is provably the artefact of the audited genome and not of some later rebuild.
    assert trial["entry_rules"] == decoded["entry_rules"]
    assert trial["exit_rules"] == decoded["exit_rules"]
    settings = trial["experts"][0]["settings"]

    bad = {}
    for gene, value in sorted(genome.items()):
        if _allowlisted(gene) or gene.endswith(":enabled"):
            continue  # allowlisted, or audited by EFFECT in the toggle test below
        ns = gene.split(":", 1)[0]
        if ns == "model":
            name = gene[len("model:"):]
            if name not in settings or settings[name] != value:
                bad[gene] = f"{gene}={value!r} is not in the trial's expert settings"
            continue
        if ns == "cond":
            _, cid, _field = gene.split(":", 2)
            for sub_ns in ("entry", "exit"):
                rule, leaf = _find_leaf(decoded[f"{sub_ns}_rules"], cid)
                if leaf is not None:
                    break
            if leaf is None:
                bad[gene] = f"{gene}: no leaf {cid!r} survives in the decoded ruleset"
                continue
            ev = _event_type_for_field(leaf["field"])
            if ev is None:
                bad[gene] = (
                    f"{gene}: field {leaf['field']!r} maps to no ExpertEventType, so the "
                    f"shared converter DROPS the leaf and the gene reaches nothing")
                continue
            emitted = _trigger_values(rule, sub_ns, ev)
            if not _matches(value, emitted):
                bad[gene] = (f"{gene}={value!r} is in no emitted {ev} trigger "
                             f"(emitted: {emitted})")
            continue
        if ns in ("entry", "exit"):
            _, rid, rest = gene.split(":", 2)
            field = rest.split(":", 1)[1] if ":" in rest else rest
            rule = _rule_by_id(decoded[f"{ns}_rules"], rid)
            if rule is None:
                bad[gene] = f"{gene}: rule {rid!r} does not survive decode"
                continue
            scalars = _action_scalars(rule, ns)
            if field == "option_dte":
                # DERIVED, the ONE gene whose value is not written verbatim: it decodes as the
                # window CENTRE and ``_apply_option_strike``'s sibling ``_apply_option_dte``
                # writes dte_min = centre - hw, dte_max = centre + hw with
                # hw = max((authored max - authored min)//2, 7). Re-derived here from the
                # AUTHORED window on the template action rather than looked for literally.
                authored = _authored_action(m, key, rid, int(rest.split(":")[0][1:]))
                hw = max(int((authored["option_dte_max"] - authored["option_dte_min"]) // 2), 7)
                want = {int(value) - hw, int(value) + hw}
                if not want.issubset({s for s in scalars if isinstance(s, int)}):
                    bad[gene] = (f"{gene}={value!r} (window centre, hw={hw}) should emit dte "
                                 f"bounds {sorted(want)}; emitted {sorted(scalars, key=str)}")
                continue
            if not _matches(value, scalars):
                bad[gene] = (f"{gene}={value!r} is in no emitted action param of rule {rid!r} "
                             f"(emitted: {sorted(str(s) for s in scalars)})")
            continue
        if ns == "optsel":
            hits = []
            for sub_ns in ("entry", "exit"):
                for rule in decoded[f"{sub_ns}_rules"] or []:
                    hits.extend(_action_scalars(rule, sub_ns))
            if not _matches(value, hits):
                bad[gene] = f"{gene}={value!r} is in no emitted action param at all"
            continue
        bad[gene] = (
            f"{gene}: unknown gene namespace {ns!r} -- not a ruleset parameter, not an expert "
            f"setting, and not on the GA-level allowlist")
    return bad


def _authored_action(m, key, rid, aidx):
    """The TEMPLATE (pre-decode) action a gene addresses -- the source of the authored DTE
    window ``_apply_option_dte`` takes its half-width from."""
    strat = m._build_strategy(key, f"audit-{key}", "FMPRating")
    for ns in ("entry_rules", "exit_rules"):
        for rule in getattr(strat, ns, None) or []:
            if rule.get("id") == rid:
                return [a for a in rule["actions"] if isinstance(a, dict)][aidx]
    raise AssertionError(f"{key}: no template rule {rid!r}")


#: (key, expert) pairs. Every option key is audited under FMPRating (the grid's default
#: ranking expert); O_ERN is audited under FMPEarningsEvent as well, because that is the expert
#: grid-2 actually pairs it with (design 2026-08-31 S9) and its expert_params block is a
#: different set of ``model:`` genes.
AUDIT_PAIRS = ([(k, "FMPRating") for k in OPTION_KEYS]
               + [("O_ERN", "FMPEarningsEvent")])


@pytest.mark.parametrize("key,expert", AUDIT_PAIRS,
                         ids=[f"{k}|{e}" for k, e in AUDIT_PAIRS])
def test_every_gene_lands_in_the_ruleset_or_the_expert_settings(key, expert):
    """CONDITIONAL DOMAINS, and why the audit runs TWO genomes.

    A categorical gene selects which OTHER genes are read: under ``option_strike_method=delta``
    the percent-OTM ``option_strike_param`` is not consumed (``_apply_option_strike`` writes the
    delta instead), and under ``option_structure=open_straddle`` the strangle's width is
    ignored (an ATM builder). Those are conditional domains, not dead genes -- the same
    distinction the O_ERN row records in the launcher -- so a gene passes if it lands under
    EITHER the first-choice or the last-choice genome. A gene that lands under NEITHER is
    consumed nowhere at all, and that is the failure this test exists to catch."""
    m = _M
    strat, space = _space(m, key, expert)
    genomes = [{g: _non_default(d, choice) for g, d in space.items()}
               for choice in (-1, 0)]
    bad = _audit_one_genome(m, key, expert, strat, space, genomes[0])
    if bad:
        still = _audit_one_genome(m, key, expert, strat, space, genomes[1])
        bad = {g: c for g, c in bad.items() if g in still}
    assert not bad, (
        f"{key}: {len(bad)} gene(s) are consumed somewhere other than the emitted "
        f"ruleset or the expert settings, under EITHER categorical branch:\n  "
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
            f"{group}: entry rules {sorted(ids)} do not cover members {members}")


def test_the_allowlist_is_small_and_every_entry_carries_a_reason():
    """The allowlist is the ONE escape from the operator principle; it must stay auditable."""
    assert len(_GA_LEVEL_GENE_ALLOWLIST) <= 2, (
        "a new allowlist entry is a reviewed decision about where a gene may live, not a way "
        "to make this test pass")
    for prefix, reason in _GA_LEVEL_GENE_ALLOWLIST.items():
        assert prefix.endswith(":"), f"{prefix!r} must be a gene NAMESPACE prefix"
        assert len(reason) > 20, f"{prefix!r} needs a real reason, got {reason!r}"
