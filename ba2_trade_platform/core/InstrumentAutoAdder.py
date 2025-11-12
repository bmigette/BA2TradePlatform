#!/usr/bin/env python3
"""
Instrument Auto-Addition Service

This service automatically adds instruments to the database when they are recommended
by experts or selected by AI, running in background to avoid blocking execution.
"""

import asyncio
import threading
from typing import List, Optional, Dict, Any
from sqlmodel import select
from ..core.models import Instrument
from ..core.db import get_db, add_instance, get_instance
from ..logger import logger
import yfinance as yf
from datetime import datetime, timezone


class InstrumentAutoAdder:
    """Service to automatically add instruments to database with proper labels and categories."""
    
    def __init__(self):
        self._task_queue = asyncio.Queue()
        self._worker_task = None
        self._running = False
        self._worker_loop = None
    
    def start(self):
        """Start the background worker."""
        if not self._running:
            self._running = True
            # Start the worker in a separate thread to avoid blocking
            self._worker_thread = threading.Thread(target=self._run_worker, daemon=True)
            self._worker_thread.start()
            logger.info("InstrumentAutoAdder service started")
    
    def stop(self):
        """Stop the background worker."""
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)
        logger.info("InstrumentAutoAdder service stopped")
    
    def _run_worker(self):
        """Run the async worker in a separate thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop = loop  # Store reference for queue operations
        try:
            loop.run_until_complete(self._worker())
        finally:
            self._worker_loop = None
            loop.close()
    
    async def _worker(self):
        """Background worker to process instrument addition tasks."""
        while self._running:
            try:
                # Wait for a task with timeout
                task = await asyncio.wait_for(self._task_queue.get(), timeout=1.0)
                await self._process_task(task)
            except asyncio.TimeoutError:
                continue  # Check if still running
            except Exception as e:
                logger.error(f"Error in InstrumentAutoAdder worker: {e}", exc_info=True)
    
    async def _process_task(self, task: Dict[str, Any]):
        """Process a single instrument addition task."""
        try:
            symbols = task['symbols']
            expert_shortname = task['expert_shortname']
            source = task['source']  # 'expert' or 'ai'
            
            logger.info(f"Processing instrument auto-addition: {len(symbols)} symbols from {source}")
            
            for symbol in symbols:
                await self._add_instrument_if_missing(symbol, expert_shortname, source)
                
        except Exception as e:
            logger.error(f"Error processing instrument auto-addition task: {e}", exc_info=True)
    
    async def _add_instrument_if_missing(self, symbol: str, expert_shortname: str, source: str):
        """Add instrument to database if it doesn't exist."""
        try:
            # Check if instrument already exists
            with get_db() as session:
                stmt = select(Instrument).where(Instrument.name == symbol)
                existing = session.exec(stmt).first()
                
                if existing:
                    logger.debug(f"Instrument {symbol} already exists in database")
                    # Add labels to existing instrument if not already present
                    if expert_shortname and expert_shortname not in existing.labels:
                        existing.labels.append(expert_shortname)
                        session.add(existing)
                        session.commit()
                        logger.debug(f"Added expert label '{expert_shortname}' to existing instrument {symbol}")
                    return
            
            # Instrument doesn't exist - create it
            logger.info(f"Auto-adding instrument {symbol} to database (source: {source})")
            
            # Fetch instrument data from Yahoo Finance
            instrument_data = await self._fetch_instrument_data(symbol)
            
            if not instrument_data:
                logger.warning(f"Could not fetch data for instrument {symbol}, creating with minimal info")
                instrument_data = {
                    'name': symbol,
                    'category': 'Unknown',
                    'description': f'Auto-added instrument from {source}'
                }
            
            # Create instrument with labels
            labels = ['auto_added']
            if expert_shortname:
                labels.append(expert_shortname)
            if source == 'ai_dynamic':
                labels.append('ai_selected')
            elif source == 'expert':
                labels.append('expert_selected')
            
            instrument = Instrument(
                name=symbol,
                category=instrument_data.get('category', 'Unknown'),
                enabled=True,
                description=instrument_data.get('description', f'Auto-added from {source}'),
                labels=labels,
                created_at=datetime.now(timezone.utc)
            )
            
            # Add to database
            instrument_id = add_instance(instrument)
            if instrument_id:
                logger.info(f"Successfully added instrument {symbol} with ID {instrument_id}")
            else:
                logger.error(f"Failed to add instrument {symbol} to database")
                
        except Exception as e:
            logger.error(f"Error adding instrument {symbol}: {e}", exc_info=True)
    
    async def _fetch_instrument_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch instrument data from Yahoo Finance."""
        try:
            # Run yfinance in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(None, yf.Ticker, symbol)
            info = await loop.run_in_executor(None, lambda: ticker.info)
            
            if not info or 'symbol' not in info:
                return None
            
            # Determine category based on sector/industry
            category = self._determine_category(info)
            
            # Create description
            description = info.get('longName', symbol)
            if info.get('sector'):
                description += f" - {info['sector']}"
            if info.get('industry'):
                description += f" ({info['industry']})"
            
            return {
                'name': symbol,
                'category': category,
                'description': description,
                'sector': info.get('sector', ''),
                'industry': info.get('industry', ''),
                'market_cap': info.get('marketCap', 0)
            }
            
        except Exception as e:
            logger.warning(f"Could not fetch Yahoo Finance data for {symbol}: {e}")
            return None
    
    def _determine_category(self, info: dict) -> str:
        """Determine instrument category based on Yahoo Finance info."""
        sector = info.get('sector', '').lower()
        industry = info.get('industry', '').lower()
        
        # Map sectors to categories
        if 'technology' in sector:
            return 'Technology'
        elif 'healthcare' in sector or 'health' in sector:
            return 'Healthcare'
        elif 'financial' in sector or 'bank' in sector:
            return 'Financial'
        elif 'energy' in sector:
            return 'Energy'
        elif 'consumer' in sector:
            if 'discretionary' in sector:
                return 'Consumer Discretionary'
            else:
                return 'Consumer Staples'
        elif 'industrial' in sector:
            return 'Industrial'
        elif 'materials' in sector or 'basic materials' in sector:
            return 'Materials'
        elif 'utilities' in sector:
            return 'Utilities'
        elif 'real estate' in sector:
            return 'Real Estate'
        elif 'communication' in sector or 'telecommunications' in sector:
            return 'Communication'
        elif 'crypto' in industry or 'bitcoin' in industry:
            return 'Cryptocurrency'
        else:
            return 'Equity'  # Default for stocks
    
    def queue_instruments_for_addition(self, symbols: List[str], expert_shortname: str, source: str = 'expert'):
        """
        Queue instruments for addition to database.
        
        Args:
            symbols: List of instrument symbols to add
            expert_shortname: Short name of the expert (e.g., 'tradingagents-1')
            source: Source of the symbols ('expert' or 'ai')
        """
        if not symbols:
            return
        
        task = {
            'symbols': symbols,
            'expert_shortname': expert_shortname,
            'source': source
        }
        
        # Add task to queue (thread-safe)
        if self._running and self._worker_loop and not self._worker_loop.is_closed():
            try:
                # Use asyncio.run_coroutine_threadsafe to add to queue from any thread
                asyncio.run_coroutine_threadsafe(self._task_queue.put(task), self._worker_loop)
                logger.debug(f"Queued {len(symbols)} instruments for auto-addition from {source}")
            except Exception as e:
                logger.error(f"Error queuing instruments for addition: {e}", exc_info=True)
        else:
            if not self._running:
                logger.warning("InstrumentAutoAdder not running, cannot queue instruments")
            elif not self._worker_loop or self._worker_loop.is_closed():
                logger.warning("Could not queue instruments: worker loop not available")


# Global instance
_instrument_auto_adder = None

def get_instrument_auto_adder() -> InstrumentAutoAdder:
    """Get the global InstrumentAutoAdder instance."""
    global _instrument_auto_adder
    if _instrument_auto_adder is None:
        _instrument_auto_adder = InstrumentAutoAdder()
        _instrument_auto_adder.start()
    return _instrument_auto_adder

def shutdown_instrument_auto_adder():
    """Shutdown the global InstrumentAutoAdder instance."""
    global _instrument_auto_adder
    if _instrument_auto_adder:
        _instrument_auto_adder.stop()
        _instrument_auto_adder = None