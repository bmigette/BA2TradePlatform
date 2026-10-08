"""Part (ii) of the permanent screener parity test: RECORDED live days.

``fixtures/screener_recorded_days.json`` holds, for a few real days, the symbols the prod screener actually returned
(``StockScreener LIVE SELECTION`` log lines, PRE-FIX live code) and the data the simulation needs to re-derive them:
for the picks, every near-miss around the thresholds, the 32 daily bars before the day, the AS-TRADED share count
(vendor history x split factor) and "now" (the open of the 09:30 five-minute bar).  It was extracted from the REAL
build path, and it is NOT selection-biased: days the simulation does not reproduce exactly are pinned with their known
residuals (``known_live_only`` / ``known_sim_only``: names within a quote tick of a threshold).

A change to ANY criterion, the order of operations or the tie-break that moves a recorded day fails here and names the
symbols.  For the enabled prod instances (floors at zero, rvol_min > 0) the pre-fix and the post-fix live behaviour
select IDENTICALLY, asserted below, so the recording also pins what jobs simulate.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ba2_providers.screener import live_sim as ls

FIXTURE = Path(__file__).parent / "fixtures" / "screener_recorded_days.json"


def _panel_for(day_rec):
    cands = day_rec["candidates"]
    syms = sorted(cands)
    dates = sorted({d for c in cands.values() for d in c["d"]} | {day_rec["day"]})
    didx = {d: i for i, d in enumerate(dates)}
    T = len(dates)
    bars = {}
    shares = np.zeros((len(syms), T))
    fac = np.ones((len(syms), T))
    for k, s in enumerate(syms):
        c = cands[s]
        idx = np.array([didx[d] for d in c["d"]])
        bars[s] = (idx, np.array(c["o"]), np.array(c["h"]), np.array(c["l"]), np.array(c["c"]), np.array(c["v"]))
        shares[k] = c["shares"]
        fac[k] = c["fac"]
    arrays = ls.build_panel_arrays(bars, dates, shares, syms, fac=fac)
    return ls.DailyPanel(syms, dates, arrays, {}), np.array([cands[s]["open"] for s in syms])


def _run(rec, beh):
    panel, opens = _panel_for(rec)
    vol = np.asarray(panel.arrays["v"][panel.pos(rec["day"])])
    return panel.select(rec["day"], rec["settings"], beh, now=opens, vol_today=lambda idx: vol[idx])


def test_the_gate_returns_the_recorded_live_picks_with_exactly_the_pinned_residuals():
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["days"]) >= 4 and any(d["known_live_only"] or d["known_sim_only"] for d in fx["days"]), \
        "the fixture must contain at least one day that is NOT reproduced exactly"
    for rec in fx["days"]:
        got = _run(rec, ls.LEGACY_CURRENT)
        assert rec["live_picks"], rec["day"]
        live_only = sorted(set(rec["live_picks"]) - set(got))
        sim_only = sorted(set(got) - set(rec["live_picks"]))
        assert live_only == rec["known_live_only"] and sim_only == rec["known_sim_only"], (
            f"{rec['day']} instance {rec['instance']}: live-only {live_only} (pinned {rec['known_live_only']}), "
            f"simulation-only {sim_only} (pinned {rec['known_sim_only']})")


def test_post_fix_and_pre_fix_live_select_identically_for_the_enabled_prod_instances():
    """Floors at zero and rvol_min > 0: the three behaviours that changed in live cannot matter."""
    fx = json.loads(FIXTURE.read_text())
    for rec in fx["days"]:
        st = rec["settings"]
        assert float(st["volume_min"]) == 0 and float(st["float_min"]) == 0 and float(st["relative_volume_min"]) > 0
        assert _run(rec, ls.POST_FIX) == _run(rec, ls.LEGACY_CURRENT), rec["day"]


def test_the_recorded_days_survive_a_shuffle_of_the_candidates():
    fx = json.loads(FIXTURE.read_text())
    rec = fx["days"][0]
    items = list(rec["candidates"].items())
    np.random.default_rng(0).shuffle(items)
    assert _run(dict(rec, candidates=dict(items)), ls.LEGACY_CURRENT) == _run(rec, ls.LEGACY_CURRENT)
