"""Which OHLCV history a backtest of THIS platform reads (``fmp`` | ``alpaca``), at the host.

The pure mapping and the stamp rules live in ``ba2_common.core.ohlcv_history``; this module is
the testplatform's thin side of it, so every switch point asks ONE place:

* ``job_history_provider(config)``  -- the raw OHLCV provider of a job / trial config
  (``get_provider("ohlcv", history_registry_key(resolve_job_history_source(config)))``);
* ``job_history_cache_dir(config)`` -- the cache folder that provider reads;
* ``stored_history_source(backtest, session)`` -- the source of a STORED row (re-run, what-if,
  chart): the row's own stamp, else its optimization's, else the legacy ``fmp`` (a row from
  before the switch has no stamp);
* ``refuse_option_job_on_unsupported_source`` -- an OPTION job stamped ``alpaca`` raises
  ``OptionHistorySourceUnsupported`` (ThetaData strikes are as-traded and the split-basis
  converter still reads FMP's calendar; plan Task 12 lifts this);
* ``verify_alpaca_cache_certified(cache_dir)`` -- an ``alpaca`` run refuses (``AlpacaCacheNotCertified``) unless the cache
  carries a PASS gate certificate of its CURRENT build manifest AND suspension events (every GA worker runs on its own copy of
  the cache: an uncertified or older copy would silently trade without the suspension liquidations the master has).
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional

from ba2_common.core.ohlcv_history import (
    DEFAULT_BT_HISTORY_SOURCE,
    LEGACY_HISTORY_SOURCE,
    OHLCV_HISTORY_SOURCE_KEY,
    check_history_source,
    history_cache_dirname,
    history_registry_key,
    resolve_job_history_source,
)

#: The only source an option job may run on until the split-basis converter reads the verified table.
OPTION_SUPPORTED_SOURCE = "fmp"


class AlpacaCacheNotCertified(RuntimeError):
    """An Alpaca-source backtest was launched on a cache the gate has not certified (or whose files changed since)."""


class OptionHistorySourceUnsupported(ValueError):
    """An option job was stamped with a history source its option data cannot be joined to."""


def job_history_provider(config: Mapping) -> Any:
    """The raw OHLCV provider of ``config``'s history source (an unstamped config reads ``fmp``)."""
    from ba2_providers import get_provider

    return get_provider("ohlcv", history_registry_key(resolve_job_history_source(config)))


def job_history_cache_dir(config: Mapping) -> str:
    """``CACHE_FOLDER/<provider dir>`` of ``config``'s history source."""
    from ba2_common.config import CACHE_FOLDER

    return os.path.join(CACHE_FOLDER, history_cache_dirname(resolve_job_history_source(config)))


def refuse_option_job_on_unsupported_source(config: Mapping, *, uses_options: bool, where: str) -> None:
    """Raise ``OptionHistorySourceUnsupported`` for an option job whose source is not ``fmp``."""
    if not uses_options:
        return
    source = resolve_job_history_source(config)
    if source != OPTION_SUPPORTED_SOURCE:
        raise OptionHistorySourceUnsupported(
            f"{where}: an option job cannot run on OHLCV history source {source!r}: ThetaData strikes "
            f"are as-traded and the split-basis converter still reads FMP's split calendar. Use "
            f"{OHLCV_HISTORY_SOURCE_KEY}={OPTION_SUPPORTED_SOURCE!r} for option jobs.")


def _blob(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def find_history_stamp(node: Any) -> Optional[str]:
    """The first ``ohlcv_history_source`` value in a persisted config blob (any depth), else None."""
    if isinstance(node, dict):
        if OHLCV_HISTORY_SOURCE_KEY in node:
            return node[OHLCV_HISTORY_SOURCE_KEY]
        for value in node.values():
            found = find_history_stamp(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_history_stamp(value)
            if found is not None:
                return found
    return None


def stored_history_source(backtest: Any, session: Any = None) -> str:
    """The history source a STORED backtest row ran on.

    Order: the row's ``strategy_params`` blob, then the ``optimization_config`` of its
    ``optimization_id`` (needs ``session``), else ``LEGACY_HISTORY_SOURCE`` (the row predates
    the stamp). A stamp that is present but unknown refuses (``UnknownHistorySource``)."""
    stamp = find_history_stamp(_blob(getattr(backtest, "strategy_params", None)))
    optimization_id = getattr(backtest, "optimization_id", None)
    if stamp is None and optimization_id is not None:
        if session is None:
            raise ValueError(
                f"backtest row belongs to optimization {optimization_id} but no session was given to "
                f"read its history-source stamp; refusing to assume {LEGACY_HISTORY_SOURCE!r}")
        from app.models.strategy_optimization import StrategyOptimization

        optimization = session.get(StrategyOptimization, optimization_id)
        if optimization is not None:
            stamp = find_history_stamp(_blob(getattr(optimization, "optimization_config", None)))
    return LEGACY_HISTORY_SOURCE if stamp is None else check_history_source(stamp)


def load_job_store(store_dir: str, config: Mapping) -> Any:
    """``metric_store.load_store(store_dir)`` for a job whose history source is ``config``'s.

    A job never reads a metric store built from the other OHLCV history: the store's build manifest
    must carry the job's source (``require_store_history_source``; a store without one is legacy fmp)."""
    from ba2_providers.screener import metric_store as ms

    ms.require_store_history_source(store_dir, resolve_job_history_source(config))
    return ms.load_store(store_dir)


def new_job_history_source(stated: Optional[str], *, uses_options: bool, where: str) -> str:
    """The source a NEW backtest / optimization is stamped with (the launcher and the API share this).

    Stated -> as stated (an unknown name refuses: ``UnknownHistorySource``); not stated ->
    ``DEFAULT_BT_HISTORY_SOURCE`` for an equity job and ``fmp`` for an option job. An option job
    resolved to anything but fmp refuses (``OptionHistorySourceUnsupported``)."""
    if stated is not None:
        source = check_history_source(stated)
    else:
        source = OPTION_SUPPORTED_SOURCE if uses_options else DEFAULT_BT_HISTORY_SOURCE
    refuse_option_job_on_unsupported_source({OHLCV_HISTORY_SOURCE_KEY: source}, uses_options=uses_options, where=where)
    return source


#: Process memo of the verified certificate, keyed on the identity (path, mtime_ns, size) of the three files it covers, so the
#: sha256 of the tens-of-MB manifest is computed once per process and a rebuilt / re-certified / hand-edited file re-verifies.
_CERTIFIED_MEMO: dict = {}


def verify_alpaca_cache_certified(cache_dir: str) -> dict:
    """The PASS gate certificate of ``cache_dir`` (``ba2_providers...verify.read_gate_certificate``), else
    ``AlpacaCacheNotCertified``; also refuses a ``suspensions.json`` whose ``build`` is not the certificate's ``build_run_id``.
    Memoized per process (see ``_CERTIFIED_MEMO``); a failure is never memoized."""
    from ba2_providers.ohlcv.alpaca_history.verify import (
        BUILD_MANIFEST_NAME, GATE_CERTIFICATE_NAME, SUSPENSIONS_NAME, GateCertificateError, read_gate_certificate)

    meta = os.path.join(cache_dir, "_meta")
    paths = [os.path.join(meta, n) for n in (BUILD_MANIFEST_NAME, SUSPENSIONS_NAME, GATE_CERTIFICATE_NAME)]

    def identity() -> Optional[tuple]:
        try:
            return tuple((p, st.st_mtime_ns, st.st_size) for p, st in ((p, os.stat(p)) for p in paths))
        except FileNotFoundError:
            return None                                  # a missing file is refused by read_gate_certificate below

    key = identity()
    if key is not None and key in _CERTIFIED_MEMO:
        return _CERTIFIED_MEMO[key]
    try:
        cert = read_gate_certificate(meta)
    except GateCertificateError as e:
        raise AlpacaCacheNotCertified(
            f"the Alpaca history cache {cache_dir} is not certified, refusing to run an alpaca-source backtest on it (this "
            f"machine's copy may differ from the master's): {e}") from e
    with open(paths[1], encoding="utf-8") as f:
        build = json.load(f)["build"]
    if build != cert["build_run_id"]:
        raise AlpacaCacheNotCertified(f"{paths[1]} is of build {build!r}, the gate certified {cert['build_run_id']!r}: rebuild and re-run the gate")
    _CERTIFIED_MEMO.clear()
    _CERTIFIED_MEMO[identity()] = cert
    return cert
