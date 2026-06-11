"""
Position review & monitoring phases (0, 5, 6) plus exit-condition updates for PennyMomentumTrader.

Part of the PennyMomentumTrader package split (EX-4): methods are unchanged,
they were moved verbatim out of __init__.py into focused mixin modules. The
mixin is mixed into PennyMomentumTrader (see __init__.py) and uses
``self`` attributes (settings, logger, trade manager, ...) defined there.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ....core.models import AnalysisOutput, ExpertInstance, MarketAnalysis
from ....core.db import add_instance, get_db, get_instance
from ....core.types import MarketAnalysisStatus
from ....core.ModelFactory import ModelFactory

from .conditions import ConditionEvaluator, get_condition_types_for_llm, validate_condition_set
from .tier_tracking import merge_tier_update, migrate_triggered_state
from .prompts import (
    build_conditions_fix_prompt,
    build_exit_generate_prompt,
    build_exit_update_prompt,
)


class MonitoringPhasesMixin:
    def _phase_0_review(self, market_analysis: MarketAnalysis):
        """Review existing open positions and record current state."""
        self.logger.info("Phase 0: Reviewing existing positions")
        trade_mgr = self._trade_mgr
        open_positions = trade_mgr.get_open_positions()

        self.logger.info(f"Found {len(open_positions)} open positions")

        # Check for positions exceeding max holding days
        max_holding = int(self.get_setting_with_interface_default(
            "max_holding_days", log_warning=False
        ))
        for pos in open_positions:
            try:
                with get_db() as session:
                    from ....core.models import Transaction

                    trans = session.get(Transaction, pos["transaction_id"])
                    if trans and trans.created_at:
                        created = trans.created_at.replace(tzinfo=timezone.utc) if trans.created_at.tzinfo is None else trans.created_at
                        age_days = (datetime.now(timezone.utc) - created).days
                        if age_days >= max_holding:
                            self.logger.info(
                                f"Position {pos['symbol']} held for {age_days} days "
                                f"(max {max_holding}), forcing exit"
                            )
                            trade_mgr.execute_exit(
                                pos["symbol"],
                                exit_pct=100.0,
                                reason=f"max holding period ({max_holding} days) exceeded",
                            )
            except Exception as e:
                self.logger.error(
                    f"Error checking position age for {pos['symbol']}: {e}",
                    exc_info=True,
                )

        self._update_state(
            market_analysis,
            {
                "open_positions": [
                    {
                        "symbol": p["symbol"],
                        "qty": p["qty"],
                        "entry_price": p["entry_price"],
                    }
                    for p in open_positions
                ],
            },
        )

    def _phase_5_monitor(self, market_analysis: MarketAnalysis):
        """Monitor conditions and execute trades until market close."""
        self.logger.info("Phase 5: Monitoring conditions")

        interval = self.get_setting_with_interface_default(
            "monitoring_interval_seconds", log_warning=False
        )
        market_tz_str = self.get_setting_with_interface_default(
            "market_timezone", log_warning=False
        )

        # Set up OHLCV provider for condition evaluation
        ohlcv_vendor_list = self.get_setting_with_interface_default(
            "vendor_ohlcv", log_warning=False
        )
        ohlcv_vendor = ohlcv_vendor_list[0] if ohlcv_vendor_list else "yfinance"

        from ....modules.dataproviders import get_provider

        ohlcv_provider = get_provider("ohlcv", ohlcv_vendor)

        trade_mgr = self._trade_mgr
        monitor_tick = 0

        # One-time startup check: open positions opened in a previous session may not
        # be present in this session's monitored_symbols (their status was "triggered"
        # and the phase-4 carry-over only picked up "watching" symbols at the time).
        # Inject them now so exit conditions are evaluated from the first tick.
        with get_db() as session:
            ma_init = session.get(MarketAnalysis, market_analysis.id)
            current_monitored_init = dict(ma_init.state.get("monitored_symbols", {})) if ma_init and ma_init.state else {}
        open_positions_init = trade_mgr.get_open_positions()
        missing_syms = {p["symbol"] for p in open_positions_init} - set(current_monitored_init.keys())
        if missing_syms:
            self.logger.info(
                f"Phase 5 startup: {len(missing_syms)} open position(s) not in monitored_symbols, "
                f"recovering exit conditions from previous sessions: {missing_syms}"
            )
            # Search through ALL recent previous MAs until every missing symbol is found,
            # since positions may have been entered in sessions several days ago.
            injected = {}
            remaining = set(missing_syms)
            try:
                with get_db() as session:
                    from sqlmodel import select as sql_select
                    statement = (
                        sql_select(MarketAnalysis)
                        .where(MarketAnalysis.expert_instance_id == self.instance.id)
                        .where(MarketAnalysis.id != market_analysis.id)
                        .order_by(MarketAnalysis.id.desc())
                        .limit(30)
                    )
                    for ma_prev in session.exec(statement).all():
                        if not remaining:
                            break
                        if not ma_prev.state:
                            continue
                        prev_monitored = ma_prev.state.get("monitored_symbols", {})
                        for sym in list(remaining):
                            info = prev_monitored.get(sym)
                            if info and info.get("status") == "triggered":
                                injected[sym] = info
                                remaining.discard(sym)
            except Exception as e:
                self.logger.warning(f"Phase 5 startup: error searching previous sessions: {e}")
            if injected:
                current_monitored_init.update(injected)
                self._update_state(market_analysis, {"monitored_symbols": current_monitored_init})
                self.logger.info(f"Phase 5 startup: injected {list(injected.keys())} into monitored_symbols")
            if remaining:
                self.logger.warning(
                    f"Phase 5 startup: could not recover exit conditions for {remaining} "
                    f"(not found in any recent session with status=triggered) — "
                    f"synthesizing minimal entries so exit conditions are evaluated"
                )
                # Synthesize minimal triggered entries so the exit monitoring loop
                # still evaluates these positions. entry_price comes from the live
                # transaction; exit_conditions will be populated on the next LLM
                # re-evaluation cycle.
                synthesized = {}
                for pos in open_positions_init:
                    sym = pos["symbol"]
                    if sym in remaining:
                        synthesized[sym] = {
                            "status": "triggered",
                            "entry_price": pos["entry_price"],
                            "qty": pos["qty"],
                            "exit_conditions": {},
                        }
                        self.logger.info(
                            f"Phase 5 startup: synthesized monitored entry for {sym} "
                            f"(entry_price={pos['entry_price']}, qty={pos['qty']})"
                        )
                if synthesized:
                    current_monitored_init.update(synthesized)
                    self._update_state(market_analysis, {"monitored_symbols": current_monitored_init})

        while not self._stop_event.is_set():
            # Check if market is still open
            if not self._is_market_open():
                self.logger.info("Market closed, exiting monitor loop")
                break

            # Reload monitored symbols from state
            with get_db() as session:
                ma = session.get(MarketAnalysis, market_analysis.id)
                if ma and ma.state:
                    monitored = dict(ma.state.get("monitored_symbols", {}))
                else:
                    monitored = {}

            evaluator = ConditionEvaluator(
                ohlcv_provider, market_timezone=market_tz_str
            )

            open_positions = trade_mgr.get_open_positions()
            open_position_symbols = {p["symbol"] for p in open_positions}

            active_symbols = [s for s, i in monitored.items() if i.get("status") in ("watching", "triggered")]
            # Log a summary every 10 ticks to avoid spam
            monitor_tick += 1
            if monitor_tick % 10 == 1:
                self.logger.debug(
                    f"Monitor tick {monitor_tick}: {len(active_symbols)} active symbol(s): "
                    f"{active_symbols} | open positions: {list(open_position_symbols)}"
                )

            # Batch-fetch live prices for all active symbols in one call
            live_prices = self._get_live_prices(active_symbols) if active_symbols else {}

            for symbol, info in list(monitored.items()):
                if self._stop_event.is_set():
                    break

                status = info.get("status", "")
                if status not in ("watching", "triggered"):
                    continue

                evaluator.clear_cache()

                try:
                    # Use batch-fetched price
                    current_price = live_prices.get(symbol)
                    if current_price is not None:
                        info["last_price"] = current_price
                    info["last_checked"] = datetime.now(timezone.utc).isoformat()

                    # Detect external close: position was closed outside the monitoring loop
                    # (manual close, broker liquidation, etc.) — stop monitoring it.
                    if status == "triggered" and symbol not in open_position_symbols and not info.get("pending_order_id"):
                        self.logger.info(
                            f"{symbol} position no longer open and no pending entry order — "
                            f"likely closed externally. Marking as closed."
                        )
                        info["status"] = "closed"
                        continue

                    # Cancel stale pending entry orders that never filled
                    if status == "triggered" and symbol not in open_position_symbols:
                        pending_order_id = info.get("pending_order_id")
                        triggered_at_str = info.get("triggered_at")
                        max_age_days = int(self.get_setting_with_interface_default(
                            "max_entry_age_days", log_warning=False
                        ))
                        if pending_order_id and triggered_at_str:
                            try:
                                triggered_at = datetime.fromisoformat(triggered_at_str)
                                if triggered_at.tzinfo is None:
                                    triggered_at = triggered_at.replace(tzinfo=timezone.utc)
                                age_days = (datetime.now(timezone.utc) - triggered_at).total_seconds() / 86400
                                if age_days >= max_age_days:
                                    self.logger.info(
                                        f"Cancelling stale pending entry order {pending_order_id} for {symbol} "
                                        f"(age={age_days:.1f}d >= max={max_age_days}d)"
                                    )
                                    trade_mgr.account.cancel_order(str(pending_order_id))
                                    info["status"] = "expired"
                                    info.pop("pending_order_id", None)
                                    continue
                                else:
                                    # Order is still within max age — check if it was already
                                    # cancelled by the broker (e.g. day order expired at EOD).
                                    # If so, reset to "watching" so entry conditions are
                                    # re-evaluated on the next monitoring tick.
                                    try:
                                        from ....core.models import TradingOrder as TradingOrderModel
                                        from ....core.types import OrderStatus as OS
                                        db_order = get_instance(TradingOrderModel, pending_order_id)
                                        if db_order and db_order.status in (
                                            OS.CANCELED.value, OS.EXPIRED.value,
                                            "canceled", "cancelled", "expired",
                                        ):
                                            self.logger.info(
                                                f"Day entry order {pending_order_id} for {symbol} was "
                                                f"cancelled/expired by broker — resetting monitor to 'watching'"
                                            )
                                            info["status"] = "watching"
                                            info.pop("pending_order_id", None)
                                            info.pop("triggered_at", None)
                                    except Exception as e_inner:
                                        self.logger.debug(
                                            f"Could not check order status for {pending_order_id}: {e_inner}"
                                        )
                            except Exception as e:
                                self.logger.warning(f"Error checking pending order age for {symbol}: {e}")

                    if symbol in open_position_symbols:
                        # Check exit conditions for open positions
                        pos = next(
                            p for p in open_positions if p["symbol"] == symbol
                        )
                        entry_price = pos["entry_price"]
                        info["entry_price"] = entry_price

                        exit_conds = info.get("exit_conditions", {})

                        # Ensure take-profit tiers carry stable ids and the fired set is
                        # id-based (migrates legacy index-based triggered_tp_tiers in place).
                        migrate_triggered_state(info)

                        # Hard EOD exit for intraday strategies
                        if info.get("strategy") == "intraday":
                            market_close = self._get_market_close_today()
                            market_now = self._get_market_now()
                            minutes_to_close = (market_close - market_now).total_seconds() / 60
                            if minutes_to_close <= 15:
                                eod_pnl = ((current_price - entry_price) / entry_price * 100) if current_price and entry_price else None
                                eod_pnl_str = f", P&L={eod_pnl:+.2f}%" if eod_pnl is not None else ""
                                self.logger.info(
                                    f"Intraday EOD hard-exit for {symbol} "
                                    f"({minutes_to_close:.0f}m to close"
                                    f", entry=${entry_price:.4f}, now=${current_price:.4f}{eod_pnl_str})"
                                )
                                eod_ok = trade_mgr.execute_exit(
                                    symbol, exit_pct=100.0, reason="intraday EOD hard-exit"
                                )
                                if eod_ok:
                                    info["status"] = "closed"
                                    self._record_trade(
                                        market_analysis, symbol, "exit", "intraday EOD hard-exit"
                                    )
                                else:
                                    self.logger.error(
                                        f"Intraday EOD exit failed for {symbol} — will retry next tick"
                                    )
                                continue

                        # Check stop loss (with grace period after entry)
                        stop_loss = exit_conds.get("stop_loss")
                        if stop_loss:
                            # Grace period: skip signal-based stop-loss for the first
                            # 10 minutes after entry to avoid whipsaws from opening
                            # volatility.  The hard percent_below_entry stop still
                            # fires during the grace period to cap downside.
                            skip_signal_stops = False
                            triggered_at_str = info.get("triggered_at")
                            if triggered_at_str:
                                try:
                                    triggered_at = datetime.fromisoformat(triggered_at_str)
                                    if triggered_at.tzinfo is None:
                                        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
                                    mins_since_entry = (datetime.now(timezone.utc) - triggered_at).total_seconds() / 60
                                    if mins_since_entry < 10:
                                        skip_signal_stops = True
                                except Exception:
                                    pass

                            if skip_signal_stops:
                                self.logger.debug(
                                    f"[GRACE] {symbol}: grace period active "
                                    f"({mins_since_entry:.1f}m since entry), "
                                    f"skipping signal-based stops"
                                )
                                # During grace period, only evaluate the hard
                                # percent_below_entry stop (ignore VWAP/time signals)
                                hard_stops = []
                                conditions = stop_loss.get("any", [])
                                if not conditions:
                                    conditions = stop_loss.get("all", [])
                                    if not conditions and stop_loss.get("type"):
                                        conditions = [stop_loss]
                                for cond in conditions:
                                    if isinstance(cond, dict):
                                        if cond.get("type") == "percent_below_entry":
                                            hard_stops.append(cond)
                                        elif "all" in cond or "any" in cond:
                                            # Nested composite — check inner conditions
                                            inner = cond.get("all", cond.get("any", []))
                                            if any(
                                                isinstance(c, dict) and c.get("type") == "percent_below_entry"
                                                for c in inner
                                            ):
                                                hard_stops.append(cond)
                                if hard_stops:
                                    grace_sl = {"any": hard_stops}
                                    if evaluator.evaluate(grace_sl, symbol, entry_price=entry_price):
                                        pnl_pct = ((current_price - entry_price) / entry_price * 100) if current_price and entry_price else None
                                        pnl_str = f", P&L={pnl_pct:+.2f}%" if pnl_pct is not None else ""
                                        self.logger.info(
                                            f"Hard stop loss triggered for {symbol} during grace period"
                                            f" (entry=${entry_price:.4f}, now=${current_price:.4f}{pnl_str})"
                                        )
                                        sl_ok = trade_mgr.execute_exit(
                                            symbol, exit_pct=100.0, reason="stop loss triggered (hard stop during grace)"
                                        )
                                        if sl_ok:
                                            info["status"] = "closed"
                                            self._record_trade(
                                                market_analysis, symbol, "exit", "stop loss (grace)"
                                            )
                                        else:
                                            self.logger.error(
                                                f"Hard stop loss exit failed for {symbol} — will retry next tick"
                                            )
                                        continue
                            elif evaluator.evaluate(
                                stop_loss, symbol, entry_price=entry_price
                            ):
                                pnl_pct = ((current_price - entry_price) / entry_price * 100) if current_price and entry_price else None
                                pnl_str = f", P&L={pnl_pct:+.2f}%" if pnl_pct is not None else ""
                                hold_min_str = ""
                                if triggered_at_str:
                                    try:
                                        ta = datetime.fromisoformat(triggered_at_str)
                                        if ta.tzinfo is None:
                                            ta = ta.replace(tzinfo=timezone.utc)
                                        hold_min = (datetime.now(timezone.utc) - ta).total_seconds() / 60
                                        hold_min_str = f", held={hold_min:.0f}m"
                                    except Exception:
                                        pass
                                self.logger.info(
                                    f"Stop loss triggered for {symbol}"
                                    f" (entry=${entry_price:.4f}, now=${current_price:.4f}{pnl_str}{hold_min_str})"
                                )
                                sl_ok = trade_mgr.execute_exit(
                                    symbol, exit_pct=100.0, reason="stop loss triggered"
                                )
                                if sl_ok:
                                    info["status"] = "closed"
                                    self._record_trade(
                                        market_analysis, symbol, "exit", "stop loss"
                                    )
                                else:
                                    self.logger.error(
                                        f"Stop loss exit failed for {symbol} — will retry next tick"
                                    )
                                continue

                        # Check take profit tiers (skip already-triggered tiers).
                        # Tiers are tracked by stable id, not index, so a tier fires
                        # exactly once even when the LLM rewrites the tier list.
                        take_profit = exit_conds.get("take_profit", [])
                        triggered_ids = info.get("triggered_tp_tier_ids", [])
                        for tier_idx, tp_tier in enumerate(take_profit):
                            if not isinstance(tp_tier, dict):
                                continue
                            if tp_tier.get("id") in triggered_ids:
                                continue
                            tp_condition = tp_tier.get("condition")
                            tp_exit_pct = tp_tier.get("exit_pct", 100.0)
                            if tp_condition and evaluator.evaluate(
                                tp_condition, symbol, entry_price=entry_price
                            ):
                                tp_pnl = ((current_price - entry_price) / entry_price * 100) if current_price and entry_price else None
                                tp_pnl_str = f", P&L={tp_pnl:+.2f}%" if tp_pnl is not None else ""
                                self.logger.info(
                                    f"Take profit tier {tier_idx + 1} triggered for {symbol} "
                                    f"(exit {tp_exit_pct}%{tp_pnl_str})"
                                )
                                exit_ok = trade_mgr.execute_exit(
                                    symbol,
                                    exit_pct=tp_exit_pct,
                                    reason=f"take profit tier {tier_idx + 1} ({tp_exit_pct}%)",
                                )
                                if exit_ok:
                                    triggered_ids.append(tp_tier.get("id"))
                                    info["triggered_tp_tier_ids"] = triggered_ids
                                    self._record_trade(
                                        market_analysis,
                                        symbol,
                                        "partial_exit" if tp_exit_pct < 100 else "exit",
                                        f"take profit tier {tier_idx + 1} ({tp_exit_pct}%)",
                                    )
                                    if tp_exit_pct >= 100:
                                        info["status"] = "closed"
                                else:
                                    self.logger.error(
                                        f"TP tier {tier_idx + 1} exit failed for {symbol} — "
                                        f"position remains open, will retry next tick"
                                    )
                                break

                        # Debug log and update condition status for UI (exit conditions)
                        exit_cond_status = {}
                        if stop_loss:
                            sl_details = evaluator.get_condition_details(stop_loss, symbol, entry_price)
                            sl_status = evaluator.get_condition_status(stop_loss, symbol, entry_price)
                            for k, v in sl_status.items():
                                exit_cond_status[f"SL:{k}"] = v
                            if monitor_tick % 10 == 1:
                                sl_met = [v for k, v in sl_details.items() if sl_status.get(k)]
                                sl_unmet = [v for k, v in sl_details.items() if not sl_status.get(k)]
                                self.logger.debug(
                                    f"{symbol} EXIT conditions (entry=${entry_price:.4f})\n"
                                    f"  SL   MET:   {sl_met}\n"
                                    f"  SL   UNMET: {sl_unmet}"
                                )
                        for tier_idx, tp_tier in enumerate(take_profit):
                            if not isinstance(tp_tier, dict):
                                continue
                            tp_condition = tp_tier.get("condition")
                            if not tp_condition:
                                continue
                            tp_exit_pct = tp_tier.get("exit_pct", 100.0)
                            already_triggered = tp_tier.get("id") in triggered_ids
                            tp_details = evaluator.get_condition_details(tp_condition, symbol, entry_price)
                            tp_status = evaluator.get_condition_status(tp_condition, symbol, entry_price)
                            for k, v in tp_status.items():
                                exit_cond_status[f"TP{tier_idx + 1}:{k}"] = v
                            if monitor_tick % 10 == 1:
                                prefix = " ✓" if already_triggered else ""
                                tp_met = [v for k, v in tp_details.items() if tp_status.get(k)]
                                tp_unmet = [v for k, v in tp_details.items() if not tp_status.get(k)]
                                self.logger.debug(
                                    f"  TP{tier_idx + 1} ({tp_exit_pct}%){prefix} MET:   {tp_met}\n"
                                    f"  TP{tier_idx + 1} ({tp_exit_pct}%){prefix} UNMET: {tp_unmet}"
                                )
                        info["conditions_last_eval"] = exit_cond_status

                    elif status == "watching":
                        # Daily refresh: reset stale per-day metrics on each new trading day.
                        # peak_rvol and prev_close are only meaningful within a single session —
                        # carrying them across days causes the RVOL-decay guard (peak may have
                        # been an extreme spike) and the already-moved guard (prev_close is stale)
                        # to permanently block entry on carried-over watching symbols.
                        today_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        if info.get("peak_rvol_date") != today_date_str:
                            old_peak = info.get("peak_rvol")
                            if old_peak is not None:
                                self.logger.info(
                                    f"New trading day: resetting peak_rvol for {symbol} "
                                    f"(was {old_peak:.1f}x)"
                                )
                            info["peak_rvol"] = None
                            info["peak_rvol_date"] = today_date_str

                        if info.get("prev_close_date") != today_date_str:
                            try:
                                quotes = self._fetch_quotes_chunked([symbol])
                                q = quotes.get(symbol, {})
                                new_prev_close = q.get("previousClose")
                                if new_prev_close and float(new_prev_close) > 0:
                                    old_prev = info.get("prev_close", 0) or 0
                                    info["prev_close"] = float(new_prev_close)
                                    info["prev_close_date"] = today_date_str
                                    self.logger.info(
                                        f"Refreshed prev_close for {symbol}: "
                                        f"${old_prev:.4f} -> ${float(new_prev_close):.4f}"
                                    )
                            except Exception as _e:
                                self.logger.warning(
                                    f"Failed to refresh prev_close for {symbol}: {_e}"
                                )

                        # Track peak RVOL observed for this symbol (for decay guard)
                        current_rvol = evaluator._get_rvol(symbol)
                        if current_rvol is not None:
                            prev_peak = info.get("peak_rvol")
                            if prev_peak is None or current_rvol > prev_peak:
                                info["peak_rvol"] = current_rvol

                        # Check entry conditions for watched symbols
                        entry_conds = info.get("entry_conditions", {})
                        if not entry_conds:
                            self.logger.debug(f"No entry_conditions stored for {symbol}, skipping")
                        else:
                            # Collect per-condition status for UI and debug logging
                            cond_status = evaluator.get_condition_status(entry_conds, symbol)
                            info["conditions_last_eval"] = cond_status
                            if monitor_tick % 10 == 1:
                                details = evaluator.get_condition_details(entry_conds, symbol)
                                met_parts = [v for k, v in details.items() if cond_status.get(k)]
                                unmet_parts = [v for k, v in details.items() if not cond_status.get(k)]
                                self.logger.debug(
                                    f"{symbol} conditions\n"
                                    f"  MET:   {met_parts}\n"
                                    f"  UNMET: {unmet_parts}"
                                )
                            # Use evaluate() to respect all/any composite logic
                            if evaluator.evaluate(entry_conds, symbol):
                                # RVOL decay guard: skip if momentum has faded
                                rvol_decay_threshold = float(
                                    self.get_setting_with_interface_default(
                                        "entry_rvol_decay_threshold", log_warning=False
                                    )
                                )
                                peak_rvol = info.get("peak_rvol")
                                rvol_decayed = (
                                    rvol_decay_threshold > 0
                                    and peak_rvol is not None
                                    and current_rvol is not None
                                    and current_rvol < peak_rvol * rvol_decay_threshold
                                )
                                # Already-moved guard: skip if stock is up too much from prev close
                                max_moved_pct = float(
                                    self.get_setting_with_interface_default(
                                        "max_already_moved_pct", log_warning=False
                                    )
                                )
                                prev_close = info.get("prev_close")
                                already_moved_pct = (
                                    (current_price - prev_close) / prev_close * 100
                                    if prev_close and current_price
                                    else None
                                )
                                already_moved = (
                                    max_moved_pct > 0
                                    and already_moved_pct is not None
                                    and already_moved_pct >= max_moved_pct
                                )

                                if rvol_decayed:
                                    info["entry_skip_reason"] = (
                                        f"RVOL decay: current={current_rvol:.1f}x < "
                                        f"peak={peak_rvol:.1f}x × {rvol_decay_threshold:.0%}"
                                    )
                                    self.logger.info(
                                        f"[SKIP] Entry skipped for {symbol}: RVOL decay "
                                        f"(current={current_rvol:.1f}x, peak={peak_rvol:.1f}x, "
                                        f"threshold={rvol_decay_threshold:.0%})"
                                        f" | conf={info.get('confidence', '?')}"
                                        f", strategy={info.get('strategy', '?')}"
                                    )
                                elif already_moved:
                                    info["entry_skip_reason"] = (
                                        f"Already moved {already_moved_pct:.1f}% from prev close "
                                        f"(${prev_close:.4f} → ${current_price:.4f}, "
                                        f"threshold {max_moved_pct:.0f}%)"
                                    )
                                    self.logger.info(
                                        f"[SKIP] Entry skipped for {symbol}: already moved "
                                        f"{already_moved_pct:.1f}% from prev close "
                                        f"(prev=${prev_close:.4f}, now=${current_price:.4f}, "
                                        f"threshold={max_moved_pct:.0f}%)"
                                        f" | conf={info.get('confidence', '?')}"
                                        f", strategy={info.get('strategy', '?')}"
                                    )
                                else:
                                    info.pop("entry_skip_reason", None)
                                    gap_str = f", gap={already_moved_pct:+.1f}%" if already_moved_pct is not None else ""
                                    rvol_str = f", rvol={current_rvol:.1f}x" if current_rvol is not None else ""
                                    price_str = f"${current_price:.4f}" if current_price is not None else "?"
                                    self.logger.info(
                                        f"Entry conditions met for {symbol}"
                                        f" (price={price_str}, conf={info.get('confidence', '?')}"
                                        f", strategy={info.get('strategy', '?')}{gap_str}{rvol_str})"
                                    )
                                    slippage_pct = float(
                                        self.get_setting_with_interface_default(
                                            "entry_limit_slippage_pct", log_warning=False
                                        )
                                    )
                                    qty = info.get("qty", 0)
                                    if qty and qty > 0:
                                        order_id = trade_mgr.execute_entry(
                                            symbol=symbol,
                                            qty=qty,
                                            confidence=info.get("confidence", 50),
                                            catalyst=info.get("catalyst", ""),
                                            strategy=info.get("strategy", "swing"),
                                            exit_conditions=info.get("exit_conditions"),
                                            market_analysis_id=market_analysis.id,
                                            limit_slippage_pct=slippage_pct,
                                        )
                                        if order_id:
                                            info["status"] = "triggered"
                                            info["pending_order_id"] = order_id
                                            info["triggered_at"] = datetime.now(timezone.utc).isoformat()
                                            info.pop("entry_attempts", None)
                                            self._record_trade(
                                                market_analysis,
                                                symbol,
                                                "entry",
                                                info.get("catalyst", ""),
                                            )
                                        else:
                                            attempts = info.get("entry_attempts", 0) + 1
                                            info["entry_attempts"] = attempts
                                            if attempts >= 5:
                                                self.logger.warning(
                                                    f"Entry for {symbol} failed {attempts} times, "
                                                    f"marking entry_failed to stop retries"
                                                )
                                                info["status"] = "entry_failed"
                                            else:
                                                self.logger.warning(
                                                    f"Entry order failed for {symbol} "
                                                    f"(attempt {attempts}/5), will retry next tick"
                                                )

                except Exception as e:
                    self.logger.error(
                        f"Error monitoring {symbol}: {e}", exc_info=True
                    )

            # Periodically re-evaluate exit conditions for open positions via LLM
            exit_update_interval = int(self.get_setting_with_interface_default(
                "exit_update_interval_ticks", log_warning=False
            ))
            if (
                exit_update_interval > 0
                and monitor_tick % exit_update_interval == 0
                and open_positions
            ):
                self._update_exit_conditions_via_llm(
                    monitored, open_position_symbols, market_analysis
                )

            # Persist updated monitored state
            self._update_state(market_analysis, {"monitored_symbols": monitored})

            # Wait for next interval
            if self._stop_event.wait(timeout=interval):
                break

    def _phase_6_eod(self, market_analysis: MarketAnalysis):
        """End-of-day wrap-up: mark analysis complete, update state."""
        self.logger.info("Phase 6: EOD wrap-up")

        with get_db() as session:
            ma = session.get(MarketAnalysis, market_analysis.id)
            if ma:
                from sqlalchemy.orm import attributes
                ma.status = MarketAnalysisStatus.COMPLETED
                state = ma.state or {}
                state["phase"] = "complete"
                state["completed_at"] = datetime.now(timezone.utc).isoformat()
                ma.state = state
                attributes.flag_modified(ma, "state")
                session.add(ma)
                session.commit()
                market_analysis.state = state
                market_analysis.status = MarketAnalysisStatus.COMPLETED

        self.logger.info("Pipeline completed")

    # ------------------------------------------------------------------
    # Live exit condition updates
    # ------------------------------------------------------------------

    def _update_exit_conditions_via_llm(
        self,
        monitored: Dict[str, Dict[str, Any]],
        open_position_symbols: set,
        market_analysis: MarketAnalysis,
    ):
        """
        For each open position, fetch fresh news and ask the LLM whether
        exit conditions should be adjusted (tighten stops, widen TP, etc.).
        """
        exit_model = self.get_setting_with_interface_default(
            "exit_update_llm", log_warning=False
        )

        for symbol in list(open_position_symbols):
            if self._stop_event.is_set():
                break

            info = monitored.get(symbol)
            if not info or info.get("status") not in ("triggered", "watching"):
                continue

            exit_conds = info.get("exit_conditions")
            generating_fresh = not exit_conds  # True when conditions were lost across restart

            try:
                # Gather fresh news + social (lightweight check)
                news_text = self._gather_news(symbol)
                social_text = self._gather_social(symbol)
                market_data = f"LATEST NEWS:\n{news_text}\n\nSOCIAL SENTIMENT:\n{social_text}"

                if generating_fresh:
                    # No existing conditions — generate from scratch using current position context
                    entry_price = info.get("entry_price") or 0.0
                    current_price = info.get("last_price") or entry_price
                    self.logger.info(
                        f"Generating initial exit conditions for {symbol} "
                        f"(entry=${entry_price:.4f}, current=${current_price:.4f})"
                    )
                    prompt = build_exit_generate_prompt(
                        symbol=symbol,
                        entry_price=entry_price,
                        current_price=current_price,
                        market_data=market_data,
                    )
                else:
                    self.logger.info(f"Re-evaluating exit conditions for {symbol}")
                    prompt = build_exit_update_prompt(
                        symbol=symbol,
                        current_conditions=exit_conds,
                        new_data=market_data,
                    )

                llm = ModelFactory.create_llm(
                    exit_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Exit Update",
                )
                # First call — check for NO_CHANGE before entering retry loop
                response = llm.invoke(prompt)
                raw_text = response.content if hasattr(response, "content") else str(response)
                if not generating_fresh and raw_text.strip().strip('"') == "NO_CHANGE":
                    self.logger.debug(f"Exit conditions unchanged for {symbol}")
                    continue

                # Parse and validate with retry on failure
                # Wrap the already-fetched response into the retry loop:
                # attempt 0 uses raw_text, subsequent attempts call the LLM again.
                updated = None
                current_raw = raw_text
                current_prompt = prompt
                for attempt in range(3):  # up to 2 retries after first attempt
                    parsed = self._parse_json_response(current_raw, expected_type=dict)
                    if not parsed:
                        errors = ["Response was not valid JSON or had unexpected structure"]
                    else:
                        validation_set = {
                            k: v for k, v in {
                                "stop_loss": parsed.get("stop_loss"),
                                "take_profit": parsed.get("take_profit"),
                            }.items() if v is not None
                        }
                        is_valid, errors = validate_condition_set(validation_set)
                        if is_valid:
                            updated = parsed
                            if attempt > 0:
                                self.logger.info(
                                    f"Exit conditions for {symbol} fixed after {attempt + 1} attempts"
                                )
                            break

                    if attempt < 2:
                        self.logger.warning(
                            f"[{symbol}] Exit update attempt {attempt + 1}: {errors}, retrying"
                        )
                        current_prompt = build_conditions_fix_prompt(current_raw, errors)
                        try:
                            retry_response = llm.invoke(current_prompt)
                            current_raw = (
                                retry_response.content
                                if hasattr(retry_response, "content")
                                else str(retry_response)
                            )
                            # If the retry says NO_CHANGE, accept it
                            if current_raw.strip().strip('"') == "NO_CHANGE":
                                self.logger.debug(f"Exit conditions unchanged for {symbol} after retry")
                                break
                        except Exception as e:
                            self.logger.warning(f"[{symbol}] Exit update retry LLM call failed: {e}")
                            break
                    else:
                        self.logger.warning(
                            f"[{symbol}] All exit update attempts produced invalid conditions: {errors}"
                        )

                if not updated:
                    continue

                # Apply updates
                if "stop_loss" in updated:
                    info["exit_conditions"]["stop_loss"] = updated["stop_loss"]
                if "take_profit" in updated:
                    # Merge the LLM's new tiers with the existing ones, preserving each
                    # surviving tier's stable id (and thus its fired status) by position.
                    # A tier that already fired never re-arms, even if its condition is
                    # rewritten; genuinely new tiers can still fire.
                    # Ensure existing tiers carry ids first (idempotent).
                    migrate_triggered_state(info)
                    old_tiers = info["exit_conditions"].get("take_profit", []) or []
                    next_id = info.get("_next_tier_id", 0)
                    merged, next_id = merge_tier_update(
                        old_tiers, updated["take_profit"], next_id
                    )
                    info["exit_conditions"]["take_profit"] = merged
                    info["_next_tier_id"] = next_id
                    # Keep only fired ids that still correspond to a surviving tier.
                    surviving_ids = {t["id"] for t in merged if isinstance(t, dict) and t.get("id")}
                    info["triggered_tp_tier_ids"] = [
                        tid for tid in info.get("triggered_tp_tier_ids", []) if tid in surviving_ids
                    ]

                self.logger.info(f"Exit conditions updated for {symbol}")

                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=exit_model,
                    name=f"exit_update_{symbol}_{datetime.now(timezone.utc).strftime('%H%M')}",
                    output_type="json",
                    text=json.dumps(updated),
                    symbol=symbol,
                    prompt=prompt,
                )

                from ....core.db import log_activity
                from ....core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.INFO,
                    activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                    description=f"PennyMomentumTrader updated exit conditions for {symbol} based on new market data",
                    data={"symbol": symbol, "updated_keys": list(updated.keys())},
                    source_expert_id=self.instance.id,
                )

            except Exception as e:
                self.logger.error(f"Exit condition update failed for {symbol}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Screener enrichment helpers
    # ------------------------------------------------------------------

    def _get_idle_status(self) -> Optional[str]:
        """Evaluate conditions every 15-min tick and surface results in the countdown log."""
        try:
            # Refresh condition evaluation so state stays current during idle periods
            self.evaluate_conditions_now()

            from sqlmodel import select as sql_select
            with get_db() as session:
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if not ma or not ma.state:
                    return None
                monitored = ma.state.get("monitored_symbols", {})

            watching = {s: i for s, i in monitored.items() if i.get("status") == "watching"}
            if not watching:
                return None

            max_age = int(self.get_setting_with_interface_default(
                "max_entry_age_days", log_warning=False
            ))
            now = datetime.now(timezone.utc)

            parts = []
            for sym, info in watching.items():
                # Conditions summary
                eval_result = info.get("conditions_last_eval")
                if eval_result:
                    met = sum(1 for v in eval_result.values() if v is True)
                    total = len(eval_result)
                    cond_str = f"{met}/{total}"
                else:
                    cond_str = "?"

                # Age / time remaining
                age_str = ""
                created_str = info.get("created_at")
                if created_str:
                    try:
                        created = datetime.fromisoformat(created_str)
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        age_days = (now - created).days
                        days_left = max_age - age_days
                        expire_date = (created + timedelta(days=max_age)).strftime("%m/%d")
                        age_str = f", day {age_days + 1}/{max_age}, exp {expire_date}"
                    except (ValueError, TypeError):
                        pass

                parts.append(f"{sym} ({cond_str}{age_str})")
            return f"watching: {', '.join(parts)}"
        except Exception:
            return None

    def evaluate_conditions_now(self) -> str:
        """
        Manually evaluate entry conditions for all watched/triggered symbols and
        persist results back to the MarketAnalysis state. Called via expert actions.
        """
        try:
            from sqlmodel import select as sql_select
            from sqlalchemy.orm import attributes

            # Find the most recent MarketAnalysis for this expert
            with get_db() as session:
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .order_by(MarketAnalysis.created_at.desc())  # type: ignore[union-attr]
                    .limit(1)
                )
                ma = session.exec(statement).first()
                if not ma or not ma.state:
                    return "No MarketAnalysis found"
                ma_id = ma.id
                monitored: Dict[str, Dict[str, Any]] = dict(
                    ma.state.get("monitored_symbols", {})
                )

            if not monitored:
                return "No monitored symbols"

            active_symbols = [
                s for s, i in monitored.items()
                if i.get("status") in ("watching", "triggered")
            ]
            if not active_symbols:
                return "No active symbols to evaluate"

            ohlcv_vendor_list = self.get_setting_with_interface_default(
                "vendor_ohlcv", log_warning=False
            )
            ohlcv_vendor = ohlcv_vendor_list[0] if ohlcv_vendor_list else "yfinance"
            market_tz_str = self.get_setting_with_interface_default(
                "market_timezone", log_warning=False
            )
            from ....modules.dataproviders import get_provider
            ohlcv_provider = get_provider("ohlcv", ohlcv_vendor)
            evaluator = ConditionEvaluator(ohlcv_provider, market_timezone=market_tz_str)

            live_prices = self._get_live_prices(active_symbols)
            now_iso = datetime.now(timezone.utc).isoformat()
            evaluated = 0

            for symbol in active_symbols:
                info = monitored[symbol]
                evaluator.clear_cache()
                try:
                    price = live_prices.get(symbol)
                    if price is not None:
                        info["last_price"] = price
                    info["last_checked"] = now_iso

                    entry_conds = info.get("entry_conditions", {})
                    if entry_conds:
                        status_map = evaluator.get_condition_status(
                            entry_conds, symbol, entry_price=info.get("entry_price")
                        )
                        info["conditions_last_eval"] = status_map

                    evaluated += 1
                except Exception as e:
                    self.logger.error(
                        f"evaluate_conditions_now: error for {symbol}: {e}", exc_info=True
                    )

            # Persist back
            with get_db() as session:
                ma_db = session.get(MarketAnalysis, ma_id)
                if ma_db:
                    state = ma_db.state or {}
                    state["monitored_symbols"] = monitored
                    ma_db.state = state
                    attributes.flag_modified(ma_db, "state")
                    session.add(ma_db)
                    session.commit()

            self.logger.info(f"evaluate_conditions_now: evaluated {evaluated} symbols")
            return f"Evaluated {evaluated} symbols"

        except Exception as e:
            self.logger.error(f"evaluate_conditions_now failed: {e}", exc_info=True)
            return f"Error: {e}"
