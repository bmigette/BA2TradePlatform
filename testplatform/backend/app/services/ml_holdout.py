"""Locked holdout symbols for the walk-forward ML exploration (v2).

A fixed random third of the symbols must NEVER be used for training, search or model
selection: they are the final holdout.  Two universes:

* ``ndq30``    - the 30 Nasdaq names of the capband matrix.  10 are locked, 20 are ``dev``.
                 The split is generated ONCE by :func:`make_lists` and committed to
                 ``testplatform/ml_holdout_symbols.json``.  Never regenerate it.
* ``largecap`` - the point-in-time large-cap universe is not known up front, so the lock is a
                 pure function of the symbol (:func:`is_locked_largecap`): about a third of any
                 symbol set is locked, independent of when the universe is built.  A holdout
                 symbol must never have been seen in development, so the NDQ30 split wins over
                 the hash: NDQ30 locked symbols are always locked, NDQ30 dev symbols (which are
                 trained on) never are.

Callers (grid runner, feature builders) pass every symbol they are about to train or search on
through :func:`assert_unlocked`.
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import date
from pathlib import Path
from typing import Iterable

DEFAULT_SEED = 20261028
GENERATOR = "app.services.ml_holdout.make_lists"
LARGECAP_RULE = ("locked iff symbol in ndq30.locked, or (symbol not in ndq30.dev and "
                 "sha256(f'{seed}:{symbol}') first byte % 3 == 0)")

# testplatform/backend/app/services/ml_holdout.py -> parents[3] is testplatform/
HOLDOUT_FILE = Path(__file__).resolve().parents[3] / "ml_holdout_symbols.json"

# Canonical NDQ30 list: _PLACEHOLDER_UNIVERSE in tools/run_screener_capband_matrix.py
# (a test keeps the two in sync).
NDQ30 = tuple(
    ("AAPL,MSFT,NVDA,AMZN,META,GOOGL,AVGO,TSLA,COST,NFLX,AMD,PEP,ADBE,CSCO,TMUS,"
     "INTC,QCOM,INTU,AMAT,TXN,AMGN,ISRG,BKNG,HON,VRTX,ADP,SBUX,GILD,MU,LRCX").split(","))


class LockedSymbolError(RuntimeError):
    """A locked (holdout) symbol was about to be used for training, search or selection."""


def make_lists(seed: int = DEFAULT_SEED) -> dict:
    """One-time generator of the locked lists.  Reproducible from ``seed``."""
    ndq30 = sorted(NDQ30)
    locked = sorted(random.Random(seed).sample(ndq30, 10))
    dev = sorted(set(ndq30) - set(locked))
    return {
        "ndq30": {"seed": seed, "generator": GENERATOR, "locked": locked, "dev": dev},
        "largecap": {"rule": LARGECAP_RULE, "seed": seed},
    }


def load() -> dict:
    with open(HOLDOUT_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def is_locked_largecap(symbol: str) -> bool:
    lists = load()
    if symbol in lists["ndq30"]["locked"]:
        return True
    if symbol in lists["ndq30"]["dev"]:
        return False
    digest = hashlib.sha256(f"{lists['largecap']['seed']}:{symbol}".encode("utf-8")).digest()
    return digest[0] % 3 == 0


def assert_unlocked(symbols: Iterable[str], purpose: str) -> None:
    """Raise :class:`LockedSymbolError` if any symbol is in the holdout.

    :func:`is_locked_largecap` already includes the NDQ30 locked list and exempts NDQ30 dev, so
    this works unchanged for the NDQ30 dev runs and for the large-cap scale-up.
    """
    bad = sorted({s for s in symbols if is_locked_largecap(s)})
    if bad:
        raise LockedSymbolError(
            f"locked holdout symbols must not be used for {purpose}: {', '.join(bad)}")


def _write_once(overwrite: bool = False) -> None:
    """Generate and write the committed JSON.  Refuses to overwrite unless told to."""
    if HOLDOUT_FILE.exists() and not overwrite:
        raise SystemExit(f"{HOLDOUT_FILE} already exists; the holdout is never regenerated")
    lists = make_lists()
    stamp = date.today().isoformat()
    for section in lists.values():
        section["generated"] = stamp
    HOLDOUT_FILE.write_text(json.dumps(lists, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import sys
    _write_once(overwrite="--overwrite" in sys.argv)
