"""Without any of the goal2027 opt-in flags the capband driver's job names are byte-identical to the ones the code
BEFORE the goal2027 DS/SQM/explr/preset work produced (2a7c2192). The golden file was generated from that commit; a
changed name would resume or skip the wrong completed job."""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRIVER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))), "tools", "run_screener_capband_matrix.py")
_GOLDEN = os.path.join(_HERE, "data", "golden_capband_names_pre_goal2027.txt")

_CASES = [
    "--name-suffix goal2027 --strategies S1,S2,S3",
    "--name-suffix goal2027-sizegene --search-sizing-mode --strategies S1,S6 --skip-experts FactorRanker",
    "--name-suffix x --strategies S2 --bands large,small --stress-spread-bps large=4,small=28",
]


def _names(args):
    r = subprocess.run([sys.executable, _DRIVER, *args.split(), "--dry-run"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    return [ln.split()[1] for ln in r.stdout.splitlines() if ln.strip().startswith(("TODO", "DONE"))]


def _golden():
    out, cur = {}, None
    for ln in open(_GOLDEN, encoding="utf-8").read().splitlines():
        if ln.startswith("## "):
            cur = ln[3:]
            out[cur] = []
        elif ln.strip():
            out[cur].append(ln.strip())
    return out


def test_flag_off_names_are_the_pre_goal2027_names():
    golden = _golden()
    assert set(golden) == set(_CASES)
    for case in _CASES:
        got = _names(case)
        assert got == golden[case], case
        assert len(got) > 5


def test_the_new_flags_change_the_names_they_apply_to_and_only_those():
    base = _names("--name-suffix goal2027-dsfw --strategies S1")
    flagged = _names("--name-suffix goal2027-dsfw --strategies S1 --search-ds-factor-weights")
    ds = [n for n in base if "DeterministicScorer" in n]
    ds_f = [n for n in flagged if "DeterministicScorer" in n]
    assert ds and ds != ds_f
    assert [n for n in base if "DeterministicScorer" not in n] == [n for n in flagged if "DeterministicScorer" not in n]
