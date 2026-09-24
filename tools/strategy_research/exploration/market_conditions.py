"""Opt-in equity entry gates, market exits and cache-only readiness for the follow-up campaign."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import sys


def _shared_paths():
    root = Path(__file__).resolve().parents[3]
    path = str(root / "packages/common")
    if path not in sys.path:
        sys.path.insert(0, path)


def selection(profile, manifest, mode):
    """Parse explicit CLI pins without opening a cache, provider or database."""
    if mode not in ("search", "all-off"):
        raise ValueError("Market-condition mode must be search or all-off")
    if profile == "none":
        if manifest or mode != "search":
            raise ValueError("Market-condition manifest/mode requires a nonempty profile")
        return (), {}
    _shared_paths()
    from ba2_common.core.market_condition_rules import parse_profile_setting
    from ba2_common.core.market_conditions import PROFILES

    parsed = parse_profile_setting(profile)
    if not parsed:
        raise ValueError("Use --market-condition-profile none to disable conditions")
    # Equivalent comma-list order must not produce another experiment identity.
    profiles = tuple(p for p in PROFILES if p in parsed)
    if not manifest:
        raise ValueError("Every market-condition profile needs a pinned manifest; warm it first")
    tokens = [s.strip() for s in manifest.split(",")]
    pins = {}
    for token in tokens:
        if "=" in token:
            name, digest = (s.strip() for s in token.split("=", 1))
        elif len(tokens) == len(profiles) == 1:
            name, digest = profiles[0], token
        else:
            raise ValueError("Use profile=digest pairs when selecting multiple profiles")
        digest = digest.removeprefix("sha256:")
        if name not in profiles or name in pins or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid, duplicate or unselected market-condition pin: {token!r}")
        pins[name] = digest
    if set(pins) != set(profiles):
        raise ValueError(f"Missing market-condition pins: {sorted(set(profiles) - set(pins))}")
    return profiles, {p: pins[p] for p in profiles}


def attach(job, backtest, profiles, pins, mode):
    """Mutate a new job only: gate opening rules, preserving actions and management trees."""
    if not profiles:
        return
    from ba2_common.core.market_conditions import PROFILES
    from ba2_common.core.market_condition_context import TIMING_POLICY_PRIOR_SESSION_V1
    from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
    from ba2_common.core.market_condition_templates import market_condition_leaves

    gene_names = []
    for index, rule in enumerate(job["strategy"]["entry_rules"]):
        actions = {a["action_type"] for a in rule["actions"]}
        if not actions & {"buy", "sell"}:
            continue
        if mode == "all-off":
            continue  # Exact original tree; profile data remains available for run diagnostics.
        prefix = f"research-{job['family']}-entry{index + 1}"
        leaves = market_condition_leaves(prefix, profiles)
        tree = rule["conditions"]
        if tree.get("type") == "AND":
            tree["conditions"].extend(leaves)
        else:
            rule["conditions"] = {"id": prefix + "-market-root", "type": "AND",
                                  "conditions": [tree, *leaves]}
        for leaf in leaves:
            gene_names.append(f"cond:{leaf['id']}:mode")
            if leaf.get("optimize"):
                gene_names.append(f"cond:{leaf['id']}:value")
    if mode == "search" and not gene_names:
        raise ValueError(f"{job['family']}: no opening rule to attach market conditions")
    for expert in backtest["experts"]:
        expert["settings"]["market_condition_profile"] = ",".join(profiles)
    backtest["market_condition_profiles"] = list(profiles)
    backtest["market_condition_manifests"] = dict(pins)
    backtest["market_condition"] = {
        "profiles": list(profiles), "manifests": dict(pins), "mode": mode,
        "calc_versions": {p: PROFILES[p].calc_version for p in profiles},
        "source_profile": SOURCE_PROFILE_FMP_DAILY,
        "timing_policy": TIMING_POLICY_PRIOR_SESSION_V1,
        "fields": [f.to_dict() for p in profiles for f in PROFILES[p].fields],
        "genes": sorted(gene_names), "gene_count": len(gene_names),
    }


def exit_selection(kinds, profiles, mode, search):
    """Validate ``--market-exit`` kinds before any job is built; returns them in canonical order.

    Each requested kind must be served by the selected profiles (refused otherwise). A
    one-profile selection serves ``exit`` only partly: ta-structure-v1 alone emits only the
    structure close and ohlcv-v1 alone only the slope close. That is accepted, and the job's
    ``market_exit.rules`` list names the rules actually emitted."""
    if not kinds:
        return ()
    _shared_paths()
    from ba2_common.core.market_condition_templates import MARKET_EXIT_KINDS, market_exit_rules

    unknown = [k for k in kinds if k not in MARKET_EXIT_KINDS]
    if unknown or len(set(kinds)) != len(kinds):
        raise ValueError(f"--market-exit takes distinct kinds from {','.join(MARKET_EXIT_KINDS)}; got {list(kinds)}")
    if not profiles:
        raise ValueError("--market-exit requires --market-condition-profile (the rules read market conditions)")
    if search != "genetic":
        raise ValueError("--market-exit searches rule toggles and thresholds: it requires --search genetic")
    if mode != "search":
        raise ValueError("--market-exit adds searched genes; --market-condition-mode all-off is the "
                         "no-impact control and cannot carry them")
    served_by = {"exit": "ohlcv-v1 (slope close) or ta-structure-v1 (structure close)",
                 "stop": "ta-structure-v1", "tp": "ohlcv-v1"}
    for kind in kinds:
        if not market_exit_rules("probe", profiles, "long", (kind,)):
            raise ValueError(f"--market-exit {kind}: no rule of this kind reads the selected profile(s) "
                             f"{','.join(profiles)}; it needs {served_by[kind]}")
    return tuple(k for k in MARKET_EXIT_KINDS if k in kinds)


def job_direction(job, backtest):
    """The ONE direction every position of this job has: the templates apply to every open
    position of the expert, so a job that could hold both sides is refused.

    ``pullback_rsi`` declares it in the expert setting ``direction`` (checked against its entry
    actions); every other family is long-only and must open only with ``buy``."""
    opens = {a["action_type"] for rule in job["strategy"]["entry_rules"]
             for a in rule["actions"]} & {"buy", "sell"}
    where = f"{job['family']}/{job['variant']}"
    if opens == {"buy", "sell"}:
        raise ValueError(f"{where}: entry rules both buy and sell; market exits need a single-direction job")
    if not opens:
        raise ValueError(f"{where}: no buy or sell entry action, so the position direction is unknown")
    implied = "long" if opens == {"buy"} else "short"
    if job["family"] == "pullback_rsi":
        declared = {e["settings"]["direction"] for e in backtest["experts"]}
        if declared != {implied}:
            raise ValueError(f"{where}: expert direction {sorted(declared)} disagrees with its "
                             f"{'/'.join(sorted(opens))} entry actions")
    elif implied != "long":
        raise ValueError(f"{where}: {job['family']} is a long-only family but its entry sells")
    return implied


def _always_matching_stop(rule):
    """True for a rule that matches every held position and stops processing (the floor
    stops: ``has_position is_true`` only). Nothing after it can ever run."""
    if rule.get("continue_processing"):
        return False
    leaves = [n for n in _walk(rule.get("conditions")) if "field" in n]
    return all(n["field"] == "has_position" and n.get("op") == "is_true" for n in leaves)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def assert_unique_ids(strategy, where):
    """Genes are keyed by id across the whole strategy: two condition nodes sharing an id share
    their genes (a B5 review found an entry toggle emptying an exit rule that way). Condition ids
    must be unique across ALL entry and exit trees, and rule ids unique within each list."""
    seen, dup = set(), set()
    for rule in strategy["entry_rules"] + strategy["exit_rules"]:
        for node in _walk(rule.get("conditions")):
            cid = node.get("id")
            if cid is not None:
                (dup if cid in seen else seen).add(cid)
    for key in ("entry_rules", "exit_rules"):
        ids = [r["id"] for r in strategy[key] if r.get("id") is not None]
        dup |= {f"{key}:{i}" for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"{where}: duplicate rule/condition ids {sorted(dup)}; they would share genes")


def attach_exits(job, backtest, profiles, kinds, direction=None):
    """Append the off-by-default market exit/stop/TP templates AFTER the job's exit rules.

    ``direction`` defaults to :func:`job_direction`; a given one must equal it. Refuses a job
    whose exit list has an always-matching stop-processing rule (the floor stops), because the
    appended templates could never run there."""
    if not kinds:
        return
    _shared_paths()
    from ba2_common.core.market_condition_rules import assert_market_rule_actions
    from ba2_common.core.market_condition_templates import market_exit_rules

    derived = job_direction(job, backtest)
    if direction is not None and direction != derived:
        raise ValueError(f"{job['family']}/{job['variant']}: direction {direction!r} but the job is {derived}")
    exits = job["strategy"]["exit_rules"]
    blocked = [r.get("id") for r in exits if _always_matching_stop(r)]
    if blocked:
        raise ValueError(
            f"{job['family']}/{job['variant']}: exit rule(s) {blocked} match every held position and "
            f"stop processing, so market exits appended after them would never run; deselect this family")
    prefix = f"research-{job['family']}-exit"
    rules = market_exit_rules(prefix, profiles, derived, kinds)
    if not rules:
        raise ValueError(f"{job['family']}: the selected profiles serve none of {list(kinds)}")
    exits.extend(rules)
    assert_unique_ids(job["strategy"], f"{job['family']}/{job['variant']}")
    assert_market_rule_actions(exits, f"{job['family']}/{job['variant']} exit rules")
    genes = []
    for rule in rules:
        genes.append(f"exit:{rule['id']}:enabled")
        genes += [f"cond:{leaf['id']}:value" for leaf in rule["conditions"]["conditions"] if leaf.get("optimize")]
        genes += [f"exit:{rule['id']}:a{i}:action_value"
                  for i, a in enumerate(rule["actions"]) if a.get("action_value_optimize")]
    backtest["market_exit"] = {"kinds": list(kinds), "direction": derived,
                               "rules": [r["id"] for r in rules], "default": "off",
                               "genes": sorted(genes), "gene_count": len(genes)}


def preflight(backtest, cache_root):
    """Verify pins and every required session, allowing only explicit initial-history gaps.

    This may prepare local mapped arrays. It never downloads or calculates indicators.
    Inspection uses all regular sessions, a conservative superset of the entry schedule.

    ``start_date``/``end_date`` are BACKTEST BARS; each reads its own session's row (the rule
    ``BacktestMarketConditionResolver`` applies).
    """
    if "market_condition_profiles" not in backtest:
        return None
    import numpy as np
    import pyarrow.parquet as pq
    from ba2_common.core.market_calendar import (
        NY_TZ, backtest_decision_label, decision_data_session, nyse_regular_sessions)
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader, prepare_host
    from ba2_common.core.market_condition_store import calendar_version
    from ba2_common.core.market_conditions import (
        PROFILES, STATUSES, STATUS_VALID, STATUS_MISSING_SESSION, STATUS_INSUFFICIENT_HISTORY, WINDOW)

    start, end = (date.fromisoformat(backtest[k]) for k in ("start_date", "end_date"))
    bars = [o.astimezone(NY_TZ).date() for o, _ in nyse_regular_sessions(start, end)]
    if not bars:
        raise ValueError("Market-condition window contains no regular sessions")
    sessions = [decision_data_session(backtest_decision_label(d)) for d in bars]  # == bars, by the rule
    days = np.asarray(sessions, dtype="datetime64[D]")
    symbols = backtest["enabled_instruments"]
    raw_dates = {}
    for symbol in symbols:
        path = Path(cache_root) / "FMPOHLCVProvider" / f"{symbol}_1d.parquet"
        dates = pq.read_table(path, columns=["Date"]).column("Date").to_numpy()
        raw_dates[symbol] = np.unique(dates.astype("datetime64[D]"))
        if not len(raw_dates[symbol]) or np.isnat(raw_dates[symbol]).any():
            raise ValueError(f"{symbol}: empty/invalid raw daily dates")
    reports = {}
    provenance = backtest["market_condition"]
    for profile in backtest["market_condition_profiles"]:
        digest = backtest["market_condition_manifests"][profile]
        reader = MappedMarketConditionReader(cache_root, digest, profile)
        manifest = reader.manifest
        if (manifest["source_profile"] != provenance["source_profile"]
                or manifest["timing_policy"] != provenance["timing_policy"]
                or manifest["calendar_version"] != calendar_version()
                or reader.calc_version != provenance["calc_versions"][profile]):
            raise ValueError(f"{profile}: snapshot source/timing/calendar/calculator differs from the job")
        report = prepare_host(cache_root, digest, profile)
        if not report.ok:
            raise ValueError(f"{profile}: snapshot verification failed: {report.to_dict()}")
        counts, initial, undefined = {}, {}, {}
        for symbol in symbols:
            present, statuses = reader.statuses_for_sessions(symbol, sessions)
            if not present.all():
                missing = [str(d) for d in days[~present][:5]]
                raise ValueError(f"{profile}/{symbol}: missing required bar-session rows {missing}; rewarm the window")
            # The first cached bar is an availability boundary, not a claim of a verified IPO.
            dates = raw_dates[symbol]
            before_source = days < dates[0]
            initial_history = np.searchsorted(dates, days, side="right") < WINDOW
            usable = False
            for index, field in enumerate(reader.fields):
                codes = statuses[:, index]
                field_counts = {st: int(np.count_nonzero(codes == i)) for i, st in enumerate(STATUSES)}
                counts.setdefault(field, {st: 0 for st in STATUSES})
                for st, n in field_counts.items():
                    counts[field][st] += n
                valid = codes == STATUSES.index(STATUS_VALID)
                usable = usable or bool(valid.any())
                missing = codes == STATUSES.index(STATUS_MISSING_SESSION)
                short = codes == STATUSES.index(STATUS_INSUFFICIENT_HISTORY)
                structural = profile == "ta-structure-v1" and field.startswith("structure_")
                allowed = valid | (missing & before_source) | (short & initial_history)
                if structural:
                    allowed |= short  # No confirmed level/break is an observation, not a cache hole.
                if not allowed.all():
                    bad = [str(d) for d in days[~allowed][:5]]
                    raise ValueError(f"{profile}/{symbol}/{field}: unexplained invalid/missing data at {bad}")
                if field_counts[STATUS_MISSING_SESSION] or field_counts[STATUS_INSUFFICIENT_HISTORY]:
                    initial[symbol] = int(np.count_nonzero(before_source | initial_history))
                if structural and (short & ~initial_history).any():
                    undefined[f"{symbol}:{field}"] = int(np.count_nonzero(short & ~initial_history))
            if not usable:
                raise ValueError(f"{profile}/{symbol}: no usable feature observations in the requested window")
        reports[profile] = {"manifest": digest, "symbols": len(symbols), "sessions": len(sessions),
                            "first_session": str(days[0]), "last_session": str(days[-1]),
                            "status_counts": counts, "initial_history_sessions": initial,
                            "undefined_structure_sessions": undefined}
    return reports
