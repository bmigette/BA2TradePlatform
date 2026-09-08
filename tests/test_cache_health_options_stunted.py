"""The options-cache check must see a symbol the fetcher ABANDONED partway.

WHY THIS EXISTS. On 2026-09-06 a ThetaData server crash (`StatusCode.INTERNAL`, a two-hour
burst) abandoned 14 underlyings mid-warm. Each kept the 13 (underlying, expiry) partitions it
had already finished, against the 349 every completed symbol carries -- 96% of their history
gone. `tools/cache_health_check.py --check-options` reported the store as healthy, and the
grid would have optimised over the remains without a word.

Both existing checks miss it BY DESIGN, which is why a third was needed rather than a tweak:

  * `shallow` compares the partition count against an absolute floor of 3. Thirteen sails past.
  * the expiry-GAP check looks only INSIDE the span between a symbol's earliest and latest
    completed expiry, because a Friday outside that span means "not caught up yet" rather than
    "queued and lost". An abandonment leaves no interior hole at all -- everything missing is
    after the last partition it finished.

The oracle is the PEER LADDER. `--discovery synthetic` plans the same Friday grid for every
underlying and records an EMPTY manifest where a chain genuinely does not exist (pre-IPO
expiries included), so a finished store holds one partition count, not a distribution.
"""
import json
import os
import time

import pytest

import tools.cache_health_check as H


def _store(root, symbol, expiries, *, age_seconds=0):
    """A symbol directory holding ``expiries`` completed partitions."""
    base = os.path.join(root, symbol)
    for i in range(expiries):
        d = os.path.join(base, f"exp=2020-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "_manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"contracts": 1, "empty_contracts": [], "complete": True}, f)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(base, (old, old))
    return base


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A store where most symbols carry a 10-expiry ladder. Patched at the seam the checker
    reads, so this exercises the real classification and not a re-implementation of it."""
    root = tmp_path / "ThetaDataOptionsProvider"
    root.mkdir()

    def build(counts, ages=None):
        ages = ages or {}
        for sym, n in counts.items():
            _store(str(root), sym, n, age_seconds=ages.get(sym, 24 * 3600))

        class _Store:
            def __init__(self, **kw):
                self.root = str(root)

            def underlyings(self):
                return sorted(counts)

            def completed_expiries(self, sym):
                # DATE objects, as the real store returns -- the expiry-gap check compares
                # these against expiry_calendar() and a string silently breaks that ordering.
                from datetime import date as _date
                d = os.path.join(str(root), sym)
                if not os.path.isdir(d):
                    return []
                return sorted(_date.fromisoformat(x[len("exp="):])
                              for x in os.listdir(d) if x.startswith("exp="))

            def read_underlying(self, sym):
                return None

            def disk_bytes(self):
                return 0

        monkeypatch.setattr(
            "ba2_providers.options.parquet_store.OptionHistoryParquetStore", _Store)
        universe = tmp_path / "universe.txt"
        universe.write_text("\n".join(sorted(counts)), encoding="utf-8")
        return H.check_options_cache(str(universe), iv_sample=0, provider="thetadata")

    return build


#: 22 healthy symbols -- above the 20-symbol quorum the check needs before it will trust a
#: peer ladder at all.
FULL = {f"SYM{i:02d}": 10 for i in range(22)}


def test_an_abandoned_symbol_is_reported(cache):
    res = cache({**FULL, "DEAD": 1})
    assert res["expected_expiries"] == 10
    assert "DEAD" in res["stunted"]


def test_it_is_the_only_check_that_sees_it(cache):
    """THE DEFECT, pinned from the other side: 4 of 10 clears the absolute `shallow` floor of
    3, and leaves no interior expiry gap. Without the peer comparison the store reads clean."""
    res = cache({**FULL, "DEAD": 4})
    assert res["stunted"] == {"DEAD": 4}
    assert res["shallow"] == {}, "the absolute floor cannot catch this and never could"


def test_a_healthy_store_reports_nothing(cache):
    res = cache(FULL)
    assert res["stunted"] == {}
    assert res["expected_expiries"] == 10


def test_a_symbol_being_written_right_now_is_not_damage(cache):
    """A warm in progress is short of the ladder for the most ordinary reason there is.
    Reporting it as abandoned sends the operator on a pointless recovery run and buries the
    symbols that really were lost. Measured 2026-09-08, the two are 53 HOURS apart."""
    res = cache({**FULL, "BUSY": 2}, ages={"BUSY": 5})
    assert res["stunted"] == {}
    assert res["stunted_in_flight"] == {"BUSY": 2}


def test_a_stale_symbol_and_a_live_one_are_told_apart_in_the_same_run(cache):
    res = cache({**FULL, "DEAD": 2, "BUSY": 2},
                ages={"DEAD": 60 * 3600, "BUSY": 5})
    assert set(res["stunted"]) == {"DEAD"}
    assert set(res["stunted_in_flight"]) == {"BUSY"}


def test_too_few_symbols_means_no_verdict_rather_than_a_guess(cache):
    """Below the quorum there is no peer group. Inventing one from three symbols would report
    a store early in its first warm as entirely broken."""
    res = cache({"A": 10, "B": 10, "C": 1})
    assert res["expected_expiries"] is None
    assert res["stunted"] == {}


def test_no_majority_means_no_verdict(cache):
    """A store mid-warm has no single ladder yet: every symbol a different count. The mode of
    that is noise, so the check declines to rule."""
    res = cache({f"S{i:02d}": i + 1 for i in range(25)})
    assert res["expected_expiries"] is None
    assert res["stunted"] == {}
