"""--market-exit and --allow-sl-loosen in the exploration driver (plan 2026-09-24, Task B6).

Pinned here:

* without the flags both default manifests are byte-identical (default + pullback_rsi);
* with ``--market-exit`` every job's exit rules end with the off-by-default templates, built for
  the job's own single direction, or immediately before a terminal catch-all stop rule (which
  would shadow them); a job that could hold both sides is refused;
* ids are unique across all entry and exit rule nodes (colliding ids share genes);
* the REAL decoder: an all-off genome gives exactly the job's original exit rules, and an all-on
  genome exports one trigger per template leaf;
* ``--allow-sl-loosen`` reaches the experts and survives the trial-config whitelist.

No broker, provider request, live database or grid run.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.strategy_research.exploration import profiles as P, market_conditions as MC
from tools.strategy_research.exploration import run_exploration as D, runtime as R
from app.services.strategy_param_space import collect_param_space, decode_params
from ba2_common.core.market_condition_rules import assert_market_rule_actions
from ba2_common.core.market_condition_templates import market_exit_rules
from ba2_common.core.market_conditions import STRUCTURE_STATE_CODES
from ba2_common.core.rules_convert import trade_rules_to_live_export

DEFAULT_FINGERPRINT = "798c8787f90a6e1215f964edc453d3c58a29138fec845cefab1f7d2873bb2fa9"
PULLBACK_RSI_FINGERPRINT = "b9524f745111e7ecfd4f17c7cbbc8c952df545879612e1359456b5c14aee6720"
PINS = {"ohlcv-v1": "a" * 64, "ta-structure-v1": "b" * 64}
BOTH = "ohlcv-v1,ta-structure-v1"
KINDS = ("exit", "stop", "tp")
#: Families whose exit list ends with a terminal catch-all (the has_position floor stop).
CATCH_ALL = {"mid_insider": "s1_sl_hold", "small_earnings": "exit_stoploss",
             "small_rating": "s1_sl_hold", "mid_earnings": "s1_sl_hold"}
SETTING = "allow_ruleset_sl_loosen"


def manifest(profile=BOTH, families=P.ALL_FAMILIES, **kwargs):
    kwargs.setdefault("search", "genetic")
    if profile != "none":
        kwargs.update(market_condition_profile=profile,
                      market_condition_manifest=",".join(f"{p}={PINS[p]}" for p in profile.split(",")))
    return P.build_manifest(families=families, **kwargs)


def by_key(m):
    return {(j["family"], j["variant"]): j for j in m["jobs"]}


def space(job):
    return collect_param_space(SimpleNamespace(**job["strategy"]), job["optimization_config"]["expert_params"])


def strategy(job):
    return SimpleNamespace(**job["strategy"])


# --------------------------------------------------------------------------- identity
def test_without_the_flags_both_default_fingerprints_are_unchanged():
    assert P.fingerprint(P.build_manifest()) == DEFAULT_FINGERPRINT
    assert P.fingerprint(P.build_manifest(market_exit=(), allow_sl_loosen=False)) == DEFAULT_FINGERPRINT
    assert P.fingerprint(P.build_manifest(families=("pullback_rsi",))) == PULLBACK_RSI_FINGERPRINT
    assert P.fingerprint(P.build_manifest(families=("pullback_rsi",), market_exit=(),
                                          allow_sl_loosen=False)) == PULLBACK_RSI_FINGERPRINT


def test_the_pins_match_the_other_research_tests():
    here = Path(__file__).parent
    assert f'== "{DEFAULT_FINGERPRINT}"' in (here / "test_research10_market_conditions.py").read_text(encoding="utf-8")


def test_each_flag_changes_the_job_identity_and_name():
    plain = by_key(manifest())
    exits = by_key(manifest(market_exit=KINDS))
    loose = by_key(manifest(allow_sl_loosen=True))
    both = by_key(manifest(market_exit=KINDS, allow_sl_loosen=True))
    ungated_loose = by_key(P.build_manifest(families=P.ALL_FAMILIES, allow_sl_loosen=True))
    ungated = by_key(P.build_manifest(families=P.ALL_FAMILIES))
    for key, job in plain.items():
        prints = {m[key]["fingerprint"] for m in (plain, exits, loose, both)}
        assert len(prints) == 4, key
        names = {m[key]["name"] for m in (plain, exits, loose, both)}
        assert len(names) == 4, key
        assert "-mx_" not in job["name"] and "-slloosen" not in job["name"]
        assert "-mx_exit_stop_tp-" in exits[key]["name"] and "-slloosen" not in exits[key]["name"]
        assert "-slloosen-" in loose[key]["name"] and "-mx_" not in loose[key]["name"]
        assert "-mx_exit_stop_tp-slloosen-" in both[key]["name"]
        assert ungated_loose[key]["fingerprint"] != ungated[key]["fingerprint"]
        labels = [m[key]["optimization_config"]["backtest"]["labels"] for m in (plain, exits, loose)]
        assert "market-exit" not in labels[0] and "sl-loosen" not in labels[0]
        assert labels[1][-1] == "market-exit" and labels[2][-1] == "sl-loosen"
        for m in (exits, loose, both):
            D.verify_job(m[key])
    # A different kind set is another experiment.
    key = ("mid_ds", "control")
    assert by_key(manifest(market_exit=("exit",)))[key]["fingerprint"] != exits[key]["fingerprint"]


def test_kind_order_is_canonical():
    assert manifest(market_exit=("tp", "exit", "stop")) == manifest(market_exit=KINDS)


# --------------------------------------------------------------------------- placement and templates
def _catch_all_index(rules):
    return next((i for i, r in enumerate(rules) if MC.terminal_catch_all(r)), len(rules))


def test_each_job_gets_the_templates_for_its_direction_at_the_right_place():
    plain = by_key(manifest())
    for key, job in by_key(manifest(market_exit=KINDS)).items():
        family = key[0]
        bt = job["optimization_config"]["backtest"]
        direction = "short" if key[1].startswith("short") else "long"
        templates = market_exit_rules(f"research-{family}-exit", ("ohlcv-v1", "ta-structure-v1"), direction)
        original = plain[key]["strategy"]["exit_rules"]
        at = _catch_all_index(original)
        assert (at < len(original)) == (family in CATCH_ALL), key
        assert bt["market_exit"]["insert_index"] == at
        assert job["strategy"]["exit_rules"] == original[:at] + templates + original[at:], key
        assert job["strategy"]["entry_rules"] == plain[key]["strategy"]["entry_rules"]
        assert [r["id"].rsplit("-mkt-", 1)[1] for r in templates] == [
            "exit-structure", "exit-slope", "stop", "tp"]
        assert all(r["enabled"] is False and r["toggle_optimize"] for r in templates)
        record = bt["market_exit"]
        assert record["kinds"] == list(KINDS) and record["direction"] == direction
        assert record["rules"] == [r["id"] for r in templates] and record["default"] == "off"
        extra = sorted(set(space(job)) - set(space(plain[key])))
        assert record["genes"] == extra and record["gene_count"] == 9
        # Everything else about the job is untouched.
        old = plain[key]["optimization_config"]
        assert job["optimization_config"]["expert_params"] == old["expert_params"]
        assert bt["experts"] == old["backtest"]["experts"]
        assert bt["market_condition"] == old["backtest"]["market_condition"]
        assert not job["fixed"] and job["optimization_type"] == "genetic"


def test_every_generated_exit_rule_passes_the_market_action_check():
    for profile in ("ohlcv-v1", "ta-structure-v1", BOTH):
        kinds = {"ohlcv-v1": ("exit", "tp"), "ta-structure-v1": ("exit", "stop"), BOTH: KINDS}[profile]
        for job in manifest(profile, market_exit=kinds)["jobs"]:
            exits = job["strategy"]["exit_rules"]
            assert_market_rule_actions(exits, job["name"])
            for rule in exits[-len(job["optimization_config"]["backtest"]["market_exit"]["rules"]):]:
                assert_market_rule_actions([rule], rule["id"])


def test_a_short_pullback_rsi_job_gets_short_templates():
    jobs = by_key(manifest(families=("pullback_rsi",), market_exit=KINDS))
    for variant, direction, against in (("long_sma5", "long", "bear"), ("short_sma5", "short", "bull"),
                                        ("short_spy", "short", "bull")):
        job = jobs[("pullback_rsi", variant)]
        assert job["optimization_config"]["backtest"]["market_exit"]["direction"] == direction
        rules = {r["id"].rsplit("-mkt-", 1)[1]: r for r in job["strategy"]["exit_rules"] if "-mkt-" in r["id"]}
        for name in ("exit-structure", "stop"):
            [leaf] = rules[name]["conditions"]["conditions"]
            assert leaf["value"] == float(STRUCTURE_STATE_CODES[against]) and leaf["mode"] == against
        [slope] = rules["exit-slope"]["conditions"]["conditions"]
        assert slope["op"] == ("<" if direction == "long" else ">")


# --------------------------------------------------------------------------- partial profiles
@pytest.mark.parametrize("profile,kinds,rules", [
    ("ta-structure-v1", ("exit",), ["exit-structure"]),
    ("ohlcv-v1", ("exit",), ["exit-slope"]),
    ("ta-structure-v1", ("exit", "stop"), ["exit-structure", "stop"]),
    ("ohlcv-v1", ("exit", "tp"), ["exit-slope", "tp"]),
])
def test_a_single_profile_serves_one_exit_variant(profile, kinds, rules):
    for job in manifest(profile, families=("pullback_rsi", "mid_ds"), market_exit=kinds)["jobs"]:
        record = job["optimization_config"]["backtest"]["market_exit"]
        assert [r.rsplit("-mkt-", 1)[1] for r in record["rules"]] == rules


@pytest.mark.parametrize("profile,kind,needs", [
    ("ohlcv-v1", "stop", "ta-structure-v1"), ("ta-structure-v1", "tp", "ohlcv-v1")])
def test_an_unserved_kind_is_refused(profile, kind, needs):
    with pytest.raises(ValueError, match=f"--market-exit {kind}: .* it needs {needs}"):
        manifest(profile, market_exit=("exit", kind))


# --------------------------------------------------------------------------- refusals
def test_the_flag_without_a_profile_is_refused():
    with pytest.raises(ValueError, match="requires --market-condition-profile"):
        P.build_manifest(search="genetic", market_exit=("exit",))


def test_the_flag_without_genetic_search_is_refused():
    with pytest.raises(ValueError, match="requires --search genetic"):
        manifest(market_condition_mode="all-off", search="grid", market_exit=("exit",))
    with pytest.raises(ValueError, match="all-off"):
        manifest(market_condition_mode="all-off", market_exit=("exit",))


@pytest.mark.parametrize("kinds", [("exits",), ("exit", "exit"), ("close",)])
def test_unknown_or_repeated_kinds_are_refused(kinds):
    with pytest.raises(ValueError, match="distinct kinds"):
        manifest(market_exit=kinds)


@pytest.mark.parametrize("family", sorted(CATCH_ALL))
def test_templates_sit_immediately_before_the_terminal_catch_all(family):
    jobs = manifest(families=(family,), market_exit=KINDS)["jobs"]
    assert jobs
    for job in jobs:
        exits = job["strategy"]["exit_rules"]
        record = job["optimization_config"]["backtest"]["market_exit"]
        at, n = record["insert_index"], len(record["rules"])
        assert [r["id"] for r in exits[at:at + n]] == record["rules"]
        # The catch-all follows directly, still last, still stopping processing.
        assert exits[at + n]["id"] == CATCH_ALL[family] and exits[at + n] is exits[-1]
        assert MC.terminal_catch_all(exits[at + n])
        assert not any(MC.terminal_catch_all(r) for r in exits[:at + n])


_HELD = {"id": "h", "field": "has_position", "op": "is_true"}


@pytest.mark.parametrize("rule,expected", [
    ({"conditions": {"type": "AND", "conditions": [_HELD]}}, True),
    ({"conditions": {"type": "AND", "conditions": [_HELD]}, "continue_processing": False}, True),
    ({"conditions": {"type": "AND", "conditions": []}}, True),
    ({"conditions": {}}, True),
    ({}, True),
    ({"conditions": {"type": "OR", "conditions": [_HELD, {"type": "AND", "conditions": [_HELD]}]}}, True),
    ({"conditions": {"type": "AND", "conditions": [_HELD]}, "continue_processing": True}, False),
    ({"conditions": {"type": "AND", "conditions": [
        _HELD, {"id": "d", "field": "days_opened", "op": ">", "value": 5}]}}, False),
    ({"conditions": {"type": "AND", "conditions": [{**_HELD, "op": "is_false"}]}}, False),
    ({"conditions": {"type": "NOT", "conditions": [_HELD]}}, False),
    ({"conditions": {"type": "AND", "conditions": [{"id": "b", "field": "bearish", "op": "is_true"}]}}, False),
])
def test_terminal_catch_all_is_exact(rule, expected):
    assert MC.terminal_catch_all({"id": "r", "actions": [{"action_type": "adjust_stop_loss"}], **rule}) is expected


def test_a_gated_stop_rule_is_not_a_catch_all_and_templates_go_after_it():
    job, bt = _job([["buy"]])
    gated = {"id": "gated_stop", "conditions": {"type": "AND", "conditions": [
        _HELD, {"id": "g", "field": "days_opened", "op": ">", "value": 3}]},
        "actions": [{"action_type": "adjust_stop_loss", "action_value": -5.0}], "continue_processing": False}
    job["strategy"]["exit_rules"].append(gated)
    MC.attach_exits(job, bt, ("ta-structure-v1",), ("exit",))
    assert [r["id"] for r in job["strategy"]["exit_rules"]] == [
        "research_timeout", "gated_stop", "research-mid_ds-exit-mkt-exit-structure"]
    assert bt["market_exit"]["insert_index"] == 2


def _job(actions, family="mid_ds", direction=None):
    settings = {} if direction is None else {"direction": direction}
    return ({"family": family, "variant": "v",
             "strategy": {"entry_rules": [{"id": f"e{i}", "conditions": {"type": "AND", "conditions": [
                 {"id": f"e{i}-bull", "field": "bullish", "op": "is_true"}]},
                 "actions": [{"action_type": a} for a in group]} for i, group in enumerate(actions)],
                 "exit_rules": [P.time_exit()]}},
            {"experts": [{"settings": settings}]})


@pytest.mark.parametrize("actions,family,direction,error", [
    ([["buy"], ["sell"]], "mid_ds", None, "both buy and sell"),
    ([["buy", "sell"]], "pullback_rsi", "long", "both buy and sell"),
    ([["adjust_stop_loss"]], "mid_ds", None, "direction is unknown"),
    ([["sell"]], "mid_ds", None, "long-only family"),
    ([["sell"]], "pullback_rsi", "long", "disagrees"),
    ([["buy"]], "pullback_rsi", "short", "disagrees"),
])
def test_mixed_or_unknown_direction_is_refused(actions, family, direction, error):
    job, bt = _job(actions, family, direction)
    with pytest.raises(ValueError, match=error):
        MC.job_direction(job, bt)
    with pytest.raises(ValueError, match=error):
        MC.attach_exits(job, bt, ("ta-structure-v1",), ("exit",))


def test_direction_is_derived_and_a_wrong_one_is_refused():
    job, bt = _job([["buy"]])
    assert MC.job_direction(job, bt) == "long"
    job, bt = _job([["sell"]], "pullback_rsi", "short")
    assert MC.job_direction(job, bt) == "short"
    with pytest.raises(ValueError, match="direction 'long' but the job is short"):
        MC.attach_exits(job, bt, ("ta-structure-v1",), ("exit",), "long")
    MC.attach_exits(job, bt, ("ta-structure-v1",), ("exit",), "short")
    assert bt["market_exit"]["direction"] == "short"


def test_colliding_ids_are_refused():
    job, bt = _job([["buy"]])
    # An entry leaf with a template leaf's id would share its genes.
    job["strategy"]["entry_rules"][0]["conditions"]["conditions"].append(
        {"id": "research-mid_ds-exit-mkt-exit-structure-state", "field": "days_opened", "op": ">", "value": 1})
    with pytest.raises(ValueError, match="duplicate rule/condition ids .*exit-structure-state"):
        MC.attach_exits(job, bt, ("ta-structure-v1",), ("exit",))
    job, bt = _job([["buy"]])
    job["strategy"]["exit_rules"].append({"id": "research-mid_ds-exit-mkt-exit-structure",
                                          "conditions": {"type": "AND", "conditions": [
                                              {"id": "late", "field": "days_opened", "op": ">", "value": 9}]},
                                          "actions": [{"action_type": "close"}]})
    with pytest.raises(ValueError, match="exit_rules:research-mid_ds-exit-mkt-exit-structure"):
        MC.attach_exits(job, bt, ("ta-structure-v1",), ("exit",))
    MC.assert_unique_ids(_job([["buy"]])[0]["strategy"], "clean")


def test_every_generated_job_has_unique_ids():
    for job in manifest(market_exit=KINDS)["jobs"]:
        MC.assert_unique_ids(job["strategy"], job["name"])


# --------------------------------------------------------------------------- the real decoder, end to end
def _genome(job, toggles):
    out = {}
    for key, rng in space(job).items():
        if key.endswith(":mode"):
            out[key] = "off"
        elif key.endswith(":enabled"):
            out[key] = toggles
        else:
            out[key] = rng["min"]
    return out


def test_all_off_genome_decodes_to_the_original_exit_rules():
    plain = by_key(manifest())
    for key, job in by_key(manifest(market_exit=KINDS)).items():
        genome = _genome(job, 0)
        original = {k: v for k, v in genome.items() if k in space(plain[key])}
        decoded = decode_params(strategy(job), genome)
        expected = decode_params(strategy(plain[key]), original)
        assert decoded["exit_rules"] == expected["exit_rules"], key
        assert decoded["entry_rules"] == expected["entry_rules"], key
        # No gene at all (an unsearched run) also leaves the templates out.
        assert decode_params(strategy(job), {})["exit_rules"] == plain[key]["strategy"]["exit_rules"]


def test_all_on_genome_exports_one_trigger_per_template_leaf_in_the_intended_order():
    plain = by_key(manifest())
    for key, job in by_key(manifest(market_exit=KINDS)).items():
        rule_ids = job["optimization_config"]["backtest"]["market_exit"]["rules"]
        exits = decode_params(strategy(job), _genome(job, 1))["exit_rules"]
        market = [r for r in exits if r["id"] in rule_ids]
        assert [r["id"] for r in market] == rule_ids, key
        assert all("enabled" not in r for r in market)
        (ruleset,) = trade_rules_to_live_export(exit_rules=exits)["rulesets"]
        # Live order is the intended one: original rules before the insert point, the market
        # rules, then the rest (a catch-all stays last).
        original = [r["id"] for r in plain[key]["strategy"]["exit_rules"]]
        at = job["optimization_config"]["backtest"]["market_exit"]["insert_index"]
        assert [r["id"] for r in exits] == original[:at] + rule_ids + original[at:], key
        # One live rule per decoded rule, in that order (unnamed rules get the positional name).
        assert [r["name"] for r in ruleset["rules"]] == [
            r.get("name") or f"backtest-strategy-open_positions-{i}" for i, r in enumerate(exits)], key
        assert [r["order_index"] for r in ruleset["rules"]] == list(range(len(exits)))
        if key[0] in CATCH_ALL:
            assert exits[-1]["id"] == CATCH_ALL[key[0]]
            last = ruleset["rules"][-1]
            assert last["continue_processing"] is False
            assert {t["event_type"] for t in last["triggers"].values()} == {"has_position"}
        live = {r["name"]: r for r in ruleset["rules"]}
        for rule in market:
            leaves = rule["conditions"]["conditions"]
            triggers = list(live[rule["id"]]["triggers"].values())
            assert triggers == [{"event_type": leaf["field"], "operator": leaf["op"], "value": leaf["value"]}
                                for leaf in leaves], (key, rule["id"])
            assert live[rule["id"]]["continue_processing"] is rule["continue_processing"]


# --------------------------------------------------------------------------- --allow-sl-loosen
def test_sl_loosen_sets_the_setting_on_every_expert():
    for job in manifest(allow_sl_loosen=True, families=P.ALL_FAMILIES)["jobs"]:
        experts = job["optimization_config"]["backtest"]["experts"]
        assert experts and all(e["settings"][SETTING] is True for e in experts)
        # execute_ready's "Unknown expert settings" check: an interface setting of every expert.
        assert SETTING in R.expert_class(job["expert"]).get_merged_settings_definitions()
    for job in P.build_manifest(families=P.ALL_FAMILIES)["jobs"]:
        assert SETTING not in job["optimization_config"]["backtest"]["experts"][0]["settings"]


def test_interface_settings_keeps_the_setting():
    from ba2_experts.PullbackReversion import PullbackReversion
    assert SETTING not in PullbackReversion.get_settings_definitions()
    assert P.interface_settings({SETTING: True, "w_technical": 0.5}, PullbackReversion) == {SETTING: True}


@pytest.mark.parametrize("family,variant", [("pullback_rsi", "long_sma5"), ("mid_ds", "control"),
                                            ("etf_trend", "top1"), ("mid_insider", "control")])
def test_sl_loosen_survives_the_trial_config(family, variant):
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings, _run_facts
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from ba2_common.core.deploy_parity import forced_expert_settings
    job = by_key(manifest(families=(family,), market_exit=("exit",), allow_sl_loosen=True))[(family, variant)]
    bt = job["optimization_config"]["backtest"]
    if "screener_opt" in bt:  # a static one-symbol universe: no metric store is read
        del bt["screener_opt"]
        bt["experts"][0]["settings"]["instrument_selection_method"] = "static"
        bt["enabled_instruments"] = ["AAA"]
    bt["backtest_id"] = f"market-exit-{family}"
    config = _build_daily_trial_config(bt, decode_params(strategy(job), _genome(job, 1)),
                                       option_trade_records=False)
    [expert] = config["experts"]
    assert expert["settings"][SETTING] is True
    assert SETTING not in forced_expert_settings(_run_facts(config, expert["settings"]))
    cls = R.expert_class(job["expert"])
    assert _expert_decision_settings(cls, expert["settings"])[SETTING] is True
    # The decoded (all-on) templates are the trial's exit rules.
    at, rules = bt["market_exit"]["insert_index"], bt["market_exit"]["rules"]
    assert [r["id"] for r in config["exit_rules"]][at:at + len(rules)] == rules


# --------------------------------------------------------------------------- CLI
def test_cli_dry_run_with_both_flags(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview attempted execution or DB access")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    argv = ["--families", "pullback_rsi", "mid_ds", "--search", "genetic",
            "--market-condition-profile", BOTH,
            "--market-condition-manifest", ",".join(f"{p}={d}" for p, d in PINS.items()),
            "--market-exit", "tp, exit,stop", "--allow-sl-loosen", "--dry-run", "--output-dir", str(tmp_path)]
    assert D.main(argv) == 0
    out = capsys.readouterr().out
    written = json.loads((tmp_path / "manifest.json").read_text())
    assert written == manifest(families=("pullback_rsi", "mid_ds"), market_exit=KINDS, allow_sl_loosen=True)
    assert "Market exits: exit,stop,tp; ruleset SL loosen: on" in out
    assert out.count("market_exit=exit,stop,tp direction=") == len(written["jobs"])
    assert "direction=short" in out and "direction=long" in out


def test_cli_prints_both_off_and_refuses_a_bad_selection(monkeypatch, tmp_path, capsys):
    assert D.main(["--families", "mid_ds", "--dry-run", "--output-dir", str(tmp_path)]) == 0
    assert "Market exits: none; ruleset SL loosen: off" in capsys.readouterr().out
    assert D.main(["--families", "mid_ds", "--market-exit", "exit", "--dry-run",
                   "--output-dir", str(tmp_path)]) == 1
    assert "requires --market-condition-profile" in capsys.readouterr().err
    assert D.parser().parse_args([]).market_exit == "" and not D.parser().parse_args([]).allow_sl_loosen


_NO_BACKEND_SCRIPT = """
import sys
from pathlib import Path
import pandas as pd
root, cache = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from tools.strategy_research.exploration import profiles as P, runtime as R
R.add_source_paths()
from ba2_providers.screener import metric_store as ms
ms.load_store = lambda _: pd.DataFrame({"date": ["2020-01-02", "2025-12-31"]})
ms.screened_symbol_union = lambda *args: ["AAA"]
R.code_signature = lambda: "source-version"
for symbol, intervals in (("AAA", ("1d", "5min")), ("SPY", ("1d",))):
    for interval in intervals:
        pd.DataFrame({"Date": pd.to_datetime(["2018-01-01", "2020-01-02", "2025-12-31"], utc=True)}
                     ).to_parquet(cache / f"{symbol}_{interval}.parquet")
P.build_manifest(families=["pullback_rsi"], search="genetic", market_condition_profile="ohlcv-v1",
                 market_condition_manifest="a" * 64, market_exit=("exit", "tp"), allow_sl_loosen=True)
job = next(j for j in P.build_manifest(families=["pullback_rsi"], allow_sl_loosen=True)["jobs"]
           if j["variant"] == "long_sma5")
job["optimization_config"]["backtest"]["screener_opt"]["store"] = str(cache)
ready = R.preflight(job, cache)
assert ready["optimization_config"]["backtest"]["experts"][0]["settings"]["allow_ruleset_sl_loosen"] is True
loaded = sorted(m for m in sys.modules if m == "app" or m.startswith("app."))
print("BACKEND:", loaded)
"""


def test_building_and_preflighting_with_the_flags_never_imports_the_backend(tmp_path):
    """A fresh interpreter, so this module's own app imports cannot mask one."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    done = subprocess.run([sys.executable, "-c", _NO_BACKEND_SCRIPT, str(ROOT), str(tmp_path)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr[-4000:]
    assert "BACKEND: []" in done.stdout, done.stdout[-2000:]
