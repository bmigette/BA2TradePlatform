"""Part (ii) of the permanent screener parity test: RECORDED live days.

``fixtures/screener_recorded_days.json`` holds, for a few real days, the symbols the prod screener actually returned
(``StockScreener LIVE SELECTION`` log lines, pre-fix live code) and the data the simulation needs to re-derive them:
for the picks, every near-miss around the thresholds (rvol / drop within a few points) the 70 daily bars before the
day, the share count and the opening print.  ``tools/screener_parity_report.py`` extracts new days; only days on which
the simulation reproduces the live list EXACTLY are recorded (the measured residuals of the other days are quote-time
noise within 1.8 % of a threshold, documented in docs/plans/2026-10-08-screener-live-sim.md).

A change to ANY criterion, the order of operations or the tie-break that makes a recorded day come out differently
fails here and names the symbols.
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
    for k, s in enumerate(syms):
        c = cands[s]
        idx = np.array([didx[d] for d in c["d"]])
        bars[s] = (idx, np.array(c["o"]), np.array(c["h"]), np.array(c["l"]), np.array(c["c"]), np.array(c["v"]))
        shares[k] = c["shares"]
    arrays = ls.build_panel_arrays(bars, dates, shares, syms)
    return ls.DailyPanel(syms, dates, arrays, {}), np.array([cands[s]["open"] for s in syms])


@pytest.mark.parametrize("beh", [ls.LEGACY_CURRENT], ids=["pre-fix live (the code that produced the recording)"])
def test_the_gate_returns_exactly_the_recorded_live_picks(beh):
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["days"]) >= 2
    for rec in fx["days"]:
        panel, opens = _panel_for(rec)
        st = rec["settings"]
        vol = np.asarray(panel.arrays["v"][panel.pos(rec["day"])])
        got = panel.select(rec["day"], st, beh, now=opens, vol_today=lambda idx: vol[idx])
        assert rec["live_picks"], rec["day"]
        assert set(got) == set(rec["live_picks"]), (
            f"{rec['day']} instance {rec['instance']}: live-only {sorted(set(rec['live_picks']) - set(got))}, "
            f"simulation-only {sorted(set(got) - set(rec['live_picks']))}")
        assert len(got) == len(rec["live_picks"])


def test_the_recorded_days_survive_a_shuffle_of_the_candidates():
    """The simulation's answer does not depend on the order candidates are stored in (stable, symbol-ascending
    tie-break and an explicit sort), so a re-ordered panel selects the same list."""
    fx = json.loads(FIXTURE.read_text())
    rec = fx["days"][0]
    items = list(rec["candidates"].items())
    rng = np.random.default_rng(0)
    rng.shuffle(items)
    rec2 = dict(rec, candidates=dict(items))
    p1, o1 = _panel_for(rec)
    p2, o2 = _panel_for(rec2)
    v1 = np.asarray(p1.arrays["v"][p1.pos(rec["day"])]); v2 = np.asarray(p2.arrays["v"][p2.pos(rec["day"])])
    st = rec["settings"]
    a = p1.select(rec["day"], st, ls.LEGACY_CURRENT, now=o1, vol_today=lambda i: v1[i])
    b = p2.select(rec["day"], st, ls.LEGACY_CURRENT, now=o2, vol_today=lambda i: v2[i])
    assert a == b
