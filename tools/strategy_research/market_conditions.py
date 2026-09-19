"""Opt-in equity entry gates and cache-only readiness for the follow-up campaign."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re
import sys


def _shared_paths():
    root = Path(__file__).resolve().parents[2]
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


def preflight(backtest, cache_root):
    """Verify pins and every required session, allowing only explicit initial-history gaps.

    This may prepare local mapped arrays. It never downloads or calculates indicators.
    Inspection uses all regular sessions, a conservative superset of the entry schedule.
    """
    if "market_condition_profiles" not in backtest:
        return None
    import numpy as np
    import pyarrow.parquet as pq
    from ba2_common.core.market_calendar import nyse_regular_sessions, prior_regular_session, NY_TZ
    from ba2_common.core.market_condition_reader import MappedMarketConditionReader, prepare_host
    from ba2_common.core.market_condition_store import calendar_version
    from ba2_common.core.market_conditions import (
        PROFILES, STATUSES, STATUS_VALID, STATUS_MISSING_SESSION, STATUS_INSUFFICIENT_HISTORY, WINDOW)

    start, end = (date.fromisoformat(backtest[k]) for k in ("start_date", "end_date"))
    decisions = [o.astimezone(NY_TZ).date() for o, _ in nyse_regular_sessions(start, end)]
    if not decisions:
        raise ValueError("Market-condition window contains no regular sessions")
    sessions = [prior_regular_session(d) for d in decisions]
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
                raise ValueError(f"{profile}/{symbol}: missing required prior-session rows {missing}; rewarm the window")
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
