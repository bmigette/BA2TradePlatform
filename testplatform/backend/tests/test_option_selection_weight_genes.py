"""The SelectionPolicy weight genes: domains first (Task 7), wiring second (Task 10).

TASK 7 — THE ``w_premium`` SIGN FIX (design 2026-08-29 §8). §9.5 originally gave
``w_premium`` the domain 0.0–2.0 on the claim that premium richness has an unambiguous
good direction. It does not: a premium SELLER wants rich premium and a BUYER wants cheap —
the identical asymmetry that made ``w_iv`` the one signed weight. Unsigned, a debit member
can only ever express "prefer richer", so the gene is half dead across the entire debit
half. The domain here is the behaviour change, pinned before any gene is emitted.
"""
import importlib.util
import os
import sys

import pytest

_LAUNCHER_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_selw", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_selw"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


@pytest.fixture(scope="module")
def L():
    return _launcher()


# ------------------------------------------------------------------------------------------ #
# Task 7 — the domains, and the sign
# ------------------------------------------------------------------------------------------ #
def test_w_premium_domain_is_signed_so_the_debit_half_can_prefer_cheap(L):
    """THE SIGN FIX. -2.0 .. 2.0, per the design's §9.5 correction (§8). An unsigned domain
    would leave a long-call arm unable to express 'prefer the cheaper contract' at all."""
    lo, hi, step = L._OPTION_SELECTION_WEIGHT_BANDS["w_premium"]
    assert (lo, hi) == (-2.0, 2.0)
    assert step > 0


def test_w_iv_stays_signed(L):
    """Sellers want rich vol, buyers cheap vol — the design's original signed weight."""
    lo, hi, _ = L._OPTION_SELECTION_WEIGHT_BANDS["w_iv"]
    assert (lo, hi) == (-2.0, 2.0)


def test_w_rvol_is_unsigned_because_nobody_wants_an_illiquid_contract(L):
    lo, hi, _ = L._OPTION_SELECTION_WEIGHT_BANDS["w_rvol"]
    assert (lo, hi) == (0.0, 2.0)


def test_every_band_samples_zero_exactly_so_the_GA_can_switch_a_weight_off(L):
    """0.0 is the no-op level (``score_all`` skips zero weights entirely); a lattice that
    cannot land on it exactly would deny the GA the control arm every other option gene
    has, and would make the un-searched run a configuration no trial can reproduce."""
    for name, (lo, hi, step) in L._OPTION_SELECTION_WEIGHT_BANDS.items():
        levels = [round(lo + i * step, 10) for i in range(int(round((hi - lo) / step)) + 1)]
        assert 0.0 in levels, f"{name}: 0.0 is not on the sampled lattice {levels}"
        assert hi in levels, f"{name}: the top of the band is unreachable"


def test_the_bands_cover_exactly_the_emitted_weights(L):
    """w_spread / w_profit / w_rr are deliberately NOT here — each is withheld on recorded
    evidence (see the table's comment and the Task 10 tests). A band with no gene would be
    dead configuration; a gene with no band would crash collection."""
    assert set(L._OPTION_SELECTION_WEIGHT_BANDS) == {"w_premium", "w_iv", "w_rvol"}
