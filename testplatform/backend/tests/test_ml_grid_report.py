"""Stage-1 gate, report.md and dashboard.html of the ML exploration v2 grid (app/services/ml_grid_report.py).

The rows are synthetic ``results.jsonl`` rows: only the fields the gate reads (primary, regimes, controls, coverage,
identity) are filled, with the shapes ``ml_grid.run_cell_seed`` writes."""
import json
import re
from pathlib import Path

import pytest

from app.services import ml_grid as G
from app.services import ml_grid_report as R

R6 = ("2019", "2020crash", "2021", "2022bear", "2023-24", "2025-26")


def mk(key, seed=1, *, track="selection", kind=None, horizon=5, family="lgbm", groups=("t",), months=12, reg=0.05,
       overall=None, p=0.01, null=-0.01, store="S", symbols=None, sig="sig1", seeds_run=3, cost=7.0, fitted=31,
       blocks=31, low_iter=0.1, status="ok"):
    """One results row. ``reg``: one number for all six regimes or a {regime: value} dict; ``overall``: the primary
    (default the mean of the regimes); ``null``: the mean of the matching shuffle control."""
    kind = kind or ("rel_return" if track == "selection" else "up")
    symbols = symbols or ("ndq30_dev" if track == "selection" else "timing")
    regs = dict(reg) if isinstance(reg, dict) else {r: reg for r in R6}
    over = overall if overall is not None else sum(regs.values()) / len(regs)
    name = G.PRIMARY[track]
    ctl = "shuffle_symbols" if track == "selection" else "shuffle_time"
    return {"cell_key": key, "seed": seed, "track": track, "label": f"{track} {kind} h{horizon} {family}",
            "status": status, "n_blocks": blocks, "n_blocks_fitted": fitted, "seeds_run": seeds_run,
            "low_iter_share": low_iter if family not in ("naive", "linear") else None,
            "primary": {"name": name, "value": over},
            "regimes": {name: {"all": {"mean": over, "t": 1.0, "n": 100},
                               **{r: {"mean": v, "t": 1.0, "n": 50} for r, v in regs.items()}}},
            "controls": {"primary": ctl, "p_value": p, "p_values": {ctl: p}, ctl: {"mean": null, "std": 0.01,
                                                                                   "rank": 1, "p_value": p}},
            "config": {"track": track, "store": store, "symbols": symbols, "groups": list(groups),
                       "target": {"kind": kind, "horizon": horizon, "ratio": 1.0}, "family": family, "params": {},
                       "train_months": months, "blocks": "Q"},
            "identity": {"settings": {"cost_bps": cost}, "store_signature": sig, "daily_fingerprint": "x"},
            "versions": {"code": "abc123def456"}}


def baselines(track="selection", naive=0.01, linear=0.02, **kw):
    """The naive-momentum and linear cells of the same track / store / target / horizon (one seed each)."""
    return [mk("naive_" + track, 1, track=track, family="naive", reg=naive, seeds_run=1, **kw),
            mk("lin_" + track, 1, track=track, family="linear", reg=linear, seeds_run=1, **kw)]


def cand(key, reg=0.05, overall=None, seeds=(1, 2, 3), **kw):
    """A candidate cell: one row per seed, ``overall`` may be a per-seed list."""
    vals = overall if isinstance(overall, (list, tuple)) else [overall] * len(seeds)
    return [mk(key, s, reg=reg, overall=v, **kw) for s, v in zip(seeds, vals)]


def cells_of(res):
    return {c["key"]: c for c in res["cells"]}


# ------------------------------------------------------------------ the gate
def test_a_cell_that_beats_both_baselines_and_its_shuffle_in_every_regime_passes():
    res = R.gate(baselines() + cand("good"))
    c = cells_of(res)["good"]
    assert c["passed"] and c["survivor"] and c["reasons"] == []
    assert c["regimes_beaten"] == 6 and c["state"] == "survivor"
    assert c["mean"] == pytest.approx(0.05) and c["std"] == pytest.approx(0.0) and c["score"] == pytest.approx(0.05)
    assert [s["key"] for s in res["survivors"]] == ["good"]
    assert set(c["regimes"]) == set(R6)
    assert c["regimes"]["2022bear"]["naive"] == pytest.approx(0.01) and c["regimes"]["2022bear"]["linear"] == pytest.approx(0.02)


def test_the_gate_takes_a_path_to_results_jsonl(tmp_path):
    p = tmp_path / "results.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in baselines() + cand("good")), encoding="utf-8")
    assert [s["key"] for s in R.gate(p)["survivors"]] == ["good"]
    assert [s["key"] for s in R.gate(str(p))["survivors"]] == ["good"]


def test_a_missing_baseline_fails_and_names_which_one():
    only_linear = [baselines()[1]] + cand("a")
    c = cells_of(R.gate(only_linear))["a"]
    assert not c["passed"] and any("missing baseline" in r and "naive" in r for r in c["reasons"])
    assert not any("linear" in r for r in c["reasons"] if "missing" in r)
    none = cells_of(R.gate(cand("a")))["a"]
    assert sum("missing baseline" in r for r in none["reasons"]) == 2          # both named


def test_a_baseline_must_match_track_store_target_and_horizon():
    for other in ({"horizon": 10}, {"store": "OTHER", "sig": "sig2"}, {"kind": "beats"}, {"symbols": "ndq30_x"}):
        rows = baselines(**other) + cand("a")
        c = cells_of(R.gate(rows))["a"]
        assert not c["passed"] and any("missing baseline" in r for r in c["reasons"]), other
    assert cells_of(R.gate(baselines(track="timing") + cand("a")))["a"]["passed"] is False      # other track's baselines


def test_too_few_regimes_beaten_fails_with_the_count():
    reg = {"2019": 0.05, "2020crash": 0.05, "2021": 0.05, "2022bear": 0.05, "2023-24": 0.0, "2025-26": 0.0}
    c = cells_of(R.gate(baselines(naive=0.01, linear=0.02) + cand("a", reg=reg, overall=0.05)))["a"]
    assert c["regimes_beaten"] == 4 and c["passed"]                           # exactly 4 is enough
    reg["2021"] = 0.0
    c = cells_of(R.gate(baselines() + cand("a", reg=reg, overall=0.05)))["a"]
    assert not c["passed"] and c["regimes_beaten"] == 3
    assert any("3 of 6" in r and "4" in r for r in c["reasons"])


def test_four_regimes_must_include_2022bear_or_2020crash():
    four = {"2019": 0.05, "2020crash": 0.0, "2021": 0.05, "2022bear": 0.0, "2023-24": 0.05, "2025-26": 0.05}
    c = cells_of(R.gate(baselines() + cand("a", reg=four, overall=0.05)))["a"]
    assert c["regimes_beaten"] == 4 and not c["passed"]
    assert any("2022bear" in r and "2020crash" in r for r in c["reasons"])
    assert not any("of 6" in r for r in c["reasons"])                         # the count itself is fine
    for hard in ("2022bear", "2020crash"):
        ok = dict(four, **{hard: 0.05, "2025-26": 0.0})
        assert cells_of(R.gate(baselines() + cand("a", reg=ok, overall=0.05)))["a"]["passed"], hard


def test_a_regime_counts_only_when_both_baselines_are_beaten():
    lin_wins = {"2019": 0.015, "2020crash": 0.05, "2021": 0.015, "2022bear": 0.05, "2023-24": 0.015, "2025-26": 0.015}
    c = cells_of(R.gate(baselines(naive=0.01, linear=0.02) + cand("a", reg=lin_wins, overall=0.05)))["a"]
    assert c["regimes_beaten"] == 2 and not c["passed"]                       # above naive everywhere, above linear in 2
    assert c["regimes"]["2019"]["beats_baseline"] is False and c["regimes"]["2022bear"]["beats_baseline"] is True


def test_a_regime_must_also_beat_the_shuffle_control_level():
    # the shuffle null of this cell sits at +0.06: a regime under it does not count even above both baselines
    c = cells_of(R.gate(baselines() + cand("a", reg=0.05, overall=0.05, null=0.06)))["a"]
    assert c["regimes_beaten"] == 0 and not c["passed"]
    assert c["regimes"]["2019"]["beats_baseline"] is True and c["regimes"]["2019"]["beats_control"] is False


def test_the_result_must_be_positive_after_costs():
    rows = baselines(naive=-0.09, linear=-0.08) + cand("a", reg=-0.05, overall=-0.05, null=-0.2)
    c = cells_of(R.gate(rows))["a"]
    assert c["regimes_beaten"] == 6                                           # better than both baselines, still a loss
    assert not c["passed"] and any("positive after" in r and "7" in r for r in c["reasons"])


def test_a_run_that_charged_less_than_seven_bps_cannot_pass():
    c = cells_of(R.gate(baselines(cost=3.0) + cand("a", cost=3.0)))["a"]
    assert not c["passed"] and any("3" in r and "bps" in r for r in c["reasons"])


def test_the_shuffle_p_value_must_be_at_most_the_threshold():
    c = cells_of(R.gate(baselines() + cand("a", p=0.2)))["a"]
    assert not c["passed"] and any("p=0.2" in r.replace(" ", "") or "0.200" in r for r in c["reasons"])
    assert c["p_value"] == pytest.approx(0.2)
    assert cells_of(R.gate(baselines() + cand("a", p=0.05)))["a"]["passed"]          # <=, inclusive
    assert not cells_of(R.gate(baselines() + cand("a", p=0.06)))["a"]["passed"]
    assert cells_of(R.gate(baselines() + cand("a", p=0.06), max_p=0.10))["a"]["passed"]


def test_the_p_value_is_the_median_over_seeds_like_the_factor_table():
    rows = baselines() + [mk("a", 1, p=0.01), mk("a", 2, p=0.02), mk("a", 3, p=0.9)]
    c = cells_of(R.gate(rows))["a"]
    assert c["p_value"] == pytest.approx(0.02) and c["passed"]


def test_every_failed_condition_gets_its_own_reason():
    rows = [mk("a", s, reg=-0.05, overall=-0.05, p=0.5, null=0.0) for s in (1, 2, 3)]
    c = cells_of(R.gate(rows))["a"]
    text = " | ".join(c["reasons"])
    assert "missing baseline" in text and "positive after" in text and "p=" in text.replace(" ", "")
    assert len(c["reasons"]) >= 4


def test_cells_with_low_block_coverage_are_excluded_with_that_reason():
    rows = baselines() + cand("thin", fitted=20) + cand("ok")
    res = R.gate(rows)
    cs = cells_of(res)
    assert cs["thin"]["state"] == "excluded" and not cs["thin"]["passed"]
    assert any("coverage" in r for r in cs["thin"]["reasons"])
    assert cs["ok"]["passed"] and [s["key"] for s in res["survivors"]] == ["ok"]
    assert set(res["excluded"]["selection"]["cell_key"]) == {"thin"}
    assert cells_of(R.gate(rows, min_coverage=0.0))["thin"]["passed"]


def test_near_constant_models_are_flagged_not_excluded_by_default():
    rows = baselines() + cand("flat", low_iter=0.9) + cand("fit", low_iter=0.2)
    res = R.gate(rows)
    cs = cells_of(res)
    assert R.GATE_MAX_LOW_ITER_SHARE is None
    assert cs["flat"]["passed"] and cs["flat"]["survivor"]                   # a flag, not a verdict
    assert cs["flat"]["low_iter_share"] == pytest.approx(0.9) and cs["flat"]["near_constant"] is True
    assert cs["fit"]["near_constant"] is False and cs["fit"]["low_iter_share"] == pytest.approx(0.2)
    assert cs["naive_selection"]["near_constant"] is False and cs["naive_selection"]["low_iter_share"] is None
    assert res["excluded"]["selection"].empty
    assert "flat" in set(res["effects"]["selection"]["cells"]["cell_key"])


def test_the_low_iteration_exclusion_is_still_available_as_a_parameter():
    rows = baselines() + cand("flat", low_iter=0.9) + cand("ok")
    res = R.gate(rows, max_low_iter_share=0.5)
    c = cells_of(res)["flat"]
    assert c["state"] == "excluded" and any("low_iter_share" in r for r in c["reasons"])
    assert c["mean"] == pytest.approx(0.05) and c["near_constant"] is True                # still shown and flagged
    assert "flat" in set(res["effects"]["selection"]["cells"]["cell_key"])               # factor tables keep it


def test_survivors_are_ranked_by_mean_minus_std_and_capped_at_ten_per_track():
    rows = baselines() + baselines("timing")
    for i in range(12):
        rows += cand(f"s{i:02d}", overall=0.05 + 0.001 * i, groups=("t", f"g{i}"))
    for i in range(3):
        rows += cand(f"t{i}", track="timing", overall=0.05 + 0.001 * i)
    res = R.gate(rows)
    sel = [s["key"] for s in res["by_track"]["selection"]]
    assert sel == [f"s{i:02d}" for i in range(11, 1, -1)]                      # best first, ten of them
    assert [s["key"] for s in res["by_track"]["timing"]] == ["t2", "t1", "t0"]
    assert len(res["survivors"]) == 13                                        # the cap is per track
    cs = cells_of(res)
    for k in ("s00", "s01"):                                                  # they pass every condition, but are over the cap
        assert cs[k]["passed"] and not cs[k]["survivor"] and cs[k]["state"] == "capped" and cs[k]["note"]
    assert cs["s11"]["rank"] == 1 and cs["s02"]["rank"] == 10


def test_the_ranking_uses_mean_minus_std_not_the_mean():
    rows = baselines() + cand("wild", overall=[0.02, 0.18, 0.10]) + cand("steady", overall=[0.06, 0.06, 0.06])
    res = R.gate(rows)
    assert [s["key"] for s in res["survivors"]] == ["steady", "wild"]
    wild = cells_of(res)["wild"]
    assert wild["mean"] == pytest.approx(0.10) and wild["std"] == pytest.approx(0.08)
    assert wild["score"] == pytest.approx(0.02)


def test_baseline_families_volatility_models_and_unfinished_cells_are_not_candidates():
    rows = baselines() + cand("a", seeds=(1, 2))                             # 2 of the 3 planned seeds finished
    rows += [mk("vol", 1, kind="volatility", horizon=5)]
    cs = cells_of(R.gate(rows))
    assert cs["naive_selection"]["state"] == "baseline" and not cs["naive_selection"]["passed"]
    assert cs["lin_selection"]["state"] == "baseline"
    assert cs["a"]["state"] == "pending" and any("2 of 3 seeds" in r for r in cs["a"]["reasons"])
    assert not cs["a"]["survivor"]
    assert cs["vol"]["state"] == "not gated" and any("volatility" in r for r in cs["vol"]["reasons"])


def test_a_single_seed_has_no_std_and_cannot_be_ranked():
    c = cells_of(R.gate(baselines() + cand("a", seeds=(1,))))["a"]
    assert not c["passed"] and c["reasons"]
    one = cells_of(R.gate(baselines() + [mk("a", 1, seeds_run=1)]))["a"]
    assert not one["passed"] and any("std" in r or "seed" in r for r in one["reasons"])


def test_the_best_matching_baseline_is_used_preferring_the_same_groups():
    rows = [mk("lin_t", 1, family="linear", groups=("t",), reg=0.04, seeds_run=1),
            mk("lin_other", 1, family="linear", groups=("t", "x"), reg=0.09, seeds_run=1),
            baselines()[0]] + cand("a", groups=("t",), reg=0.05)
    c = cells_of(R.gate(rows))["a"]
    assert c["regimes"]["2019"]["linear"] == pytest.approx(0.04)             # the one with the cell's own groups
    assert c["baseline_keys"]["linear"] == "lin_t"
    rows[2:] = cand("b", groups=("m",), reg=0.05)                            # nobody shares its groups: the strongest
    c = cells_of(R.gate(rows))["b"]
    assert c["regimes"]["2019"]["linear"] == pytest.approx(0.09) and c["baseline_keys"]["linear"] == "lin_other"


def test_a_nan_regime_is_never_counted():
    reg = {"2019": None, "2020crash": 0.05, "2021": 0.05, "2022bear": 0.05, "2023-24": 0.05, "2025-26": 0.05}
    rows = baselines() + cand("a", reg=reg, overall=0.05)
    for r in rows:
        if r["cell_key"] == "a":
            r["regimes"][G.PRIMARY["selection"]]["2019"]["mean"] = None
    c = cells_of(R.gate(rows))["a"]
    assert c["regimes_beaten"] == 5 and c["regimes"]["2019"]["value"] is None and c["passed"]


def test_gate_parameters_are_recorded_in_the_result():
    p = R.gate(baselines() + cand("a"))["params"]
    assert p["max_p"] == R.GATE_MAX_P and p["min_regimes"] == 4 and p["max_survivors"] == 10
    assert p["hard_regimes"] == ["2022bear", "2020crash"] and p["regimes"] == list(R6) and p["cost_bps"] == 7.0
    assert R.GATE_MAX_SURVIVORS == 10 and R.GATE_MIN_REGIMES == 4


def test_a_dead_end_grid_gates_to_no_survivors():
    res = R.gate(baselines() + cand("bad", reg=-0.05, overall=-0.05))
    assert res["survivors"] == [] and res["by_track"] == {"selection": [], "timing": []}


# ------------------------------------------------------------------ run state + report.md
def write_run(out: Path, rows, errors=(), summary=None):
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    if errors:
        (out / "errors.jsonl").write_text("".join(json.dumps(r) + "\n" for r in errors), encoding="utf-8")
    if summary is not None:
        (out / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")


def sample_rows():
    return (baselines() + baselines("timing") + cand("good", groups=("t", "m")) +
            cand("flop", reg=-0.05, overall=-0.05, groups=("t",), months=24) +
            cand("tgood", track="timing", groups=("t", "mtf"), kind="up") +
            cand("thin", fitted=10, groups=("x",)))


def test_report_md_holds_the_factor_tables_the_gate_table_survivors_and_the_run_summary(tmp_path):
    out = tmp_path / "grid_a"
    errs = [{"cell_key": "boom", "seed": 1, "error": "ValueError: x", "label": "l", "traceback": ""}]
    write_run(out, sample_rows(), errs, {"planned_tasks": 30, "pool_rebuilds": 2, "errors": 1, "done": 24,
                                         "cancelled": False})
    R.build_outputs(out, report=True)
    md = (out / "report.md").read_text(encoding="utf-8")
    for needle in ("# ML grid stage 1", "grid_a", "## Run summary", "pool_rebuilds", "## Survivors",
                   "## Factor effects", "selection", "timing", "## Gate", "good", "tgood", "flop"):
        assert needle in md, needle
    assert re.search(r"rows done\D+\d+", md, re.I) and re.search(r"errors\D+1\b", md, re.I)
    assert re.search(r"pool_rebuilds\D+2\b", md)
    assert re.search(r"\| excluded cells \| 1 \(selection 1, timing 0\)", md)      # 'thin': coverage
    assert "coverage" in md                                                     # the exclusion reason is shown
    assert "| train_months |" in md or "train_months" in md and "groups" in md
    surv = md.split("## Survivors")[1].split("\n## ")[0]
    assert "good" in surv and "tgood" in surv and "flop" not in surv
    gate_part = md.split("## Gate table")[1]
    assert "positive after" in gate_part                                        # flop's reason
    assert re.search(r"\b4 of 6\b|\b6\b", gate_part) and "±" in gate_part


def test_report_says_so_when_nothing_passes(tmp_path):
    out = tmp_path / "grid_b"
    write_run(out, baselines() + cand("bad", reg=-0.05, overall=-0.05))
    R.build_outputs(out, report=True)
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "No cell passed" in md and "factor-effect" in md


def test_report_only_state_works_without_a_run_summary_file(tmp_path):
    out = tmp_path / "grid_c"
    write_run(out, baselines() + cand("good"))
    R.build_outputs(out, report=True)
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "pool_rebuilds" in md and "unknown" in md                            # not recorded: said so, not invented


def test_report_counts_an_error_that_a_later_row_resolved_as_resolved(tmp_path):
    out = tmp_path / "grid_d"
    rows = baselines() + cand("good")
    errs = [{"cell_key": "good", "seed": 1, "error": "x", "label": "", "traceback": ""},
            {"cell_key": "gone", "seed": 1, "error": "y", "label": "", "traceback": ""}]
    write_run(out, rows, errs)
    st = R.collect_state(out)
    assert st["n_errors"] == 1 and st["n_rows"] == len(rows)                    # the 'good' seed 1 retry succeeded


def test_outputs_are_written_atomically(tmp_path, monkeypatch):
    out = tmp_path / "grid_e"
    write_run(out, baselines() + cand("good"))
    seen = []
    real = R.os.replace
    monkeypatch.setattr(R.os, "replace", lambda a, b: (seen.append((Path(a).name, Path(b).name)), real(a, b))[1])
    R.build_outputs(out, report=True)
    assert ("dashboard.html.tmp", "dashboard.html") in seen and ("report.md.tmp", "report.md") in seen
    assert not list(out.glob("*.tmp"))


# ------------------------------------------------------------------ dashboard.html
def dash(tmp_path, rows=None, name="grid_dash", **kw):
    out = tmp_path / name
    write_run(out, rows if rows is not None else sample_rows(), **kw)
    R.build_outputs(out, report=False)
    return (out / "dashboard.html").read_text(encoding="utf-8")


def test_dashboard_follows_the_artifact_page_skeleton(tmp_path):
    html = dash(tmp_path)
    assert html.startswith('<title>ML Grid Stage 1</title>\n<meta name="claude:viewport-fit-sides" content="cover">\n<style>')
    assert not re.search(r"<(!doctype|html|head|body)\b", html, re.I)
    assert "<style>" in html and html.index("<style>") < html.index("</style>") < html.index("<h1")
    assert not re.search(r"<script[^>]*\bsrc=|<link\b|@import|<iframe|<img", html, re.I)


def test_dashboard_has_no_external_resource(tmp_path):
    html = dash(tmp_path)
    urls = re.findall(r"""(?:https?:)?//[^\s"')<>]+""", html)
    assert all("fonts.googleapis.com" in u or "fonts.gstatic.com" in u for u in urls), urls
    assert "url(" not in html


def _css(html):
    return re.search(r"<style>(.*?)</style>", html, re.S).group(1)


def _blocks(css):
    """The three token blocks: bare :root, the dark media query and :root[data-theme="dark"]."""
    light = re.search(r"(?m)^:root\s*\{(.*?)\}", css, re.S).group(1)
    media = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme=\"light\"\]\)\s*\{(.*?)\}",
                      css, re.S).group(1)
    dark = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\}', css, re.S).group(1)
    return light, media, dark


def test_every_colour_is_a_token_defined_in_the_light_and_both_dark_blocks(tmp_path):
    css = _css(dash(tmp_path))
    light, media, dark = _blocks(css)
    tokens = set(re.findall(r"(--[\w-]+)\s*:", light))
    assert {"--bg", "--fg", "--muted", "--card", "--border", "--accent", "--pos", "--neg", "--neutral", "--good",
            "--bad", "--pending"} <= tokens
    for block in (media, dark):
        assert tokens <= set(re.findall(r"(--[\w-]+)\s*:", block))
        assert "color-scheme: dark" in block
    assert "color-scheme: light" in light
    assert re.search(r"body\s*\{[^}]*background:\s*var\(--bg\)[^}]*color:\s*var\(--fg\)", css, re.S) or \
        re.search(r"body\s*\{[^}]*color:\s*var\(--fg\)[^}]*background:\s*var\(--bg\)", css, re.S)
    rest = css
    for b in (light, media, dark):
        rest = rest.replace(b, "")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|oklch\(", rest), "literal colour outside the token blocks"
    assert not re.search(r":\s*(white|black|red|green|blue|gray|grey|orange|yellow)\b", rest)


def test_dashboard_layout_contract(tmp_path):
    css = _css(dash(tmp_path))
    assert "padding-left: max(16px, env(safe-area-inset-left, 0px))" in css
    assert "padding-right: max(16px, env(safe-area-inset-right, 0px))" in css
    assert "padding-block" in css and "font-variant-numeric: tabular-nums" in css
    assert "overflow-x: auto" in css


def test_dashboard_wide_tables_sit_in_scroll_containers(tmp_path):
    html = dash(tmp_path)
    assert html.count("<table") >= 3
    assert html.count('<div class="scroll"><table') == html.count("<table")


def test_dashboard_shows_tiles_heatmap_factor_bars_gate_chips_identity_and_timestamp(tmp_path):
    html = dash(tmp_path, name="grid_ident", summary={"planned_tasks": 30, "pool_rebuilds": 0})
    assert "grid_ident" in html and G.library_versions()["code"] in html and "abc123def456" in html
    assert re.search(r"Last updated[^<]*\d{4}-\d{2}-\d{2}", html)
    for needle in ("Progress", "Errors", "Survivors", "Heatmap", "Factor effects", "Gate"):
        assert needle in html, needle
    assert 'class="chip' in html and 'data-state="survivor"' in html and 'data-state="fail"' in html
    assert 'data-state="excluded"' in html and 'data-state="baseline"' in html
    assert "2022bear" in html and "2020crash" in html and "quintile_spread_net" in html
    assert 'class="hm' in html and "--m:" in html                      # heatmap cells carry their magnitude
    assert 'class="bar' in html
    assert "good" in html and "tgood" in html


def test_chips_carry_a_glyph_and_a_label_not_only_a_colour(tmp_path):
    html = dash(tmp_path)
    chips = re.findall(r'<span class="chip[^"]*"[^>]*>(.*?)</span>', html, re.S)
    assert chips and all(re.search(r"[A-Za-z]{3,}", c) and re.search(r"[^\w\s]", c) for c in chips)


def test_dashboard_escapes_every_value(tmp_path):
    evil = '<script>alert(1)</script>"&'
    rows = baselines() + cand("good", groups=("t", evil))
    rows += [mk("k<img src=x onerror=1>", 1, seeds_run=1)]
    html = dash(tmp_path, rows, name="grid_&b'x")
    assert "<script>" not in html and "onerror=1>" not in html
    assert "&lt;script&gt;" in html and "&lt;img src=x onerror=1&gt;" in html
    assert "grid_&amp;b&#x27;x" in html and "grid_&b'x" not in html


def test_dashboard_is_small_and_caps_the_heatmap_rows(tmp_path):
    rows = baselines()
    for i in range(700):
        rows += cand(f"c{i:04d}", overall=0.05 + 0.0001 * i, groups=("t", f"g{i}"), months=12)
    html = dash(tmp_path, rows, name="grid_big")
    assert len(html.encode("utf-8")) < 2_000_000
    assert re.search(r"top \d+ of \d+ cells", html)                           # the cap is announced
    assert html.count('class="hmrow') <= R.HEATMAP_MAX_ROWS * 2 + 4


def test_an_empty_run_still_renders_a_complete_page(tmp_path):
    out = tmp_path / "grid_empty"
    out.mkdir()
    R.build_outputs(out, report=True)
    html = (out / "dashboard.html").read_text(encoding="utf-8")
    assert "<title>ML Grid Stage 1</title>" in html and "No finished rows yet" in html
    assert "No cell passed" in (out / "report.md").read_text(encoding="utf-8")


def test_a_pending_cell_shows_the_pending_chip(tmp_path):
    html = dash(tmp_path, baselines() + cand("half", seeds=(1,)), name="grid_pend")
    assert 'data-state="pending"' in html


def test_run_summary_is_written_and_read_back(tmp_path):
    out = tmp_path / "grid_rs"
    out.mkdir()
    R.write_run_summary(out, {"planned_tasks": 7, "pool_rebuilds": 1})
    assert json.loads((out / "run_summary.json").read_text())["planned_tasks"] == 7
    assert R.collect_state(out)["summary"]["pool_rebuilds"] == 1


def test_the_dashboard_and_report_flag_near_constant_cells(tmp_path):
    rows = baselines() + cand("flat", low_iter=0.9) + cand("fit", low_iter=0.2, groups=("t", "m"))
    out = tmp_path / "grid_flag"
    write_run(out, rows)
    R.build_outputs(out, report=True)
    html = (out / "dashboard.html").read_text(encoding="utf-8")
    assert html.count("near-constant 90%") >= 1 and "near-constant 20%" not in html
    assert 'data-state="flag"' in html and 'data-state="survivor"' in html
    chips = re.findall(r'<span class="chip" data-state="flag">(.*?)</span>', html)
    assert chips and all(re.search(r"[^\w\s]", c) and "near-constant" in c for c in chips)
    assert '.chip[data-state="flag"]' in html
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "near-constant 90%" in md and "near-constant 20%" not in md
    assert "No cell passed" not in md
