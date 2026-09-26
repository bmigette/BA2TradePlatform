"""The REAL FRED DGS3MO series (2019-2025) as a hermetic test fixture.

Option backtests read their Black-Scholes risk-free rate from the FRED disk cache
(``ba2_providers.macro.risk_free_rate``) and refuse without it. Tests that run the real
wiring install this extract into their temporary CACHE_FOLDER; tests that only need a
``RiskFreeRate`` object read it straight from the fixture file.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

DGS3MO_FIXTURE = Path(__file__).with_name("fred_DGS3MO_2019_2025.json")


def install_dgs3mo(cache_folder) -> str:
    """Copy the fixture to ``<cache_folder>/fred/DGS3MO.json`` (where ``fred_series.cache_path``
    looks when its ``CACHE_FOLDER`` is ``cache_folder``). Returns the path."""
    dest = os.path.join(str(cache_folder), "fred", "DGS3MO.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(DGS3MO_FIXTURE, dest)
    return dest


def dgs3mo_rate(start, end):
    """The fixture series as the ``RiskFreeRate`` a run over ``[start, end]`` would read."""
    from ba2_providers.macro.risk_free_rate import fred_dgs3mo_rate

    return fred_dgs3mo_rate(start, end, path=str(DGS3MO_FIXTURE))
