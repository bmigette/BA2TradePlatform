"""
Job Manager for BA2 Trade Platform

This module manages scheduled and manual analysis jobs using APScheduler.
It handles:
- Reading expert settings and creating scheduled analysis jobs
- Accepting manual analysis job requests
- Managing job lifecycle and queue interaction
"""

import threading
import time
import json
import queue
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.job import Job

from ..core.utils import get_expert_instance_from_id
from ..logger import logger
from .db import get_all_instances, get_instance, get_setting
from .models import ExpertInstance, ExpertSetting, Instrument
from .WorkerQueue import get_worker_queue, AnalysisTask
from .types import WorkerTaskStatus
from .types import AnalysisUseCase


class ControlMessageType(Enum):
    """Types of control messages for JobManager."""
    REFRESH_EXPERT_SCHEDULES = "refresh_expert_schedules"
    SHUTDOWN = "shutdown"


@dataclass
class ControlMessage:
    """Control message for JobManager operations."""
    message_type: ControlMessageType
    expert_instance_id: Optional[int] = None
    data: Optional[Dict[str, Any]] = None


class JobManager:
    """
    Manages scheduled and manual analysis jobs for expert instances.
    """
    
    def __init__(self):
        """Initialize the JobManager."""
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()
        self._running = False
        self._scheduled_jobs: Dict[str, Job] = {}  # Maps job_id to APScheduler Job
        self._lock = threading.Lock()
        
        # Control queue for asynchronous operations
        self._control_queue = queue.Queue()
        self._control_thread = None
        self._control_thread_running = False
        
        logger.info("JobManager initialized")
        
    def start(self):
        """Start the job manager and schedule all expert analysis jobs."""
        if self._running:
            logger.warning("JobManager is already running")
            return
            
        with self._lock:
            self._running = True
            
        logger.info("Starting JobManager...")
        
        # Start control thread for asynchronous operations
        self._start_control_thread()
        
        # Schedule all expert jobs
        self._schedule_all_expert_jobs()
        
        # Schedule account refresh job
        self._schedule_account_refresh_job()
        
        logger.info("JobManager started successfully")
        
    def stop(self):
        """Stop the job manager and clear all scheduled jobs."""
        if not self._running:
            logger.warning("JobManager is not running")
            return
            
        logger.info("Stopping JobManager...")
        
        # Stop control thread
        self._stop_control_thread()
        
        with self._lock:
            self._running = False
            
        # Remove all scheduled jobs
        self._scheduler.remove_all_jobs()
        self._scheduled_jobs.clear()
        
        logger.info("JobManager stopped")
    
    def refresh_expert_schedules(self, expert_instance_id: int = None):
        """
        Request refresh of scheduled jobs for a specific expert or all experts.
        This method is non-blocking and queues the request for asynchronous processing.
        
        Args:
            expert_instance_id: If provided, only refresh this expert's schedule.
                              If None, refresh all expert schedules.
        """
        if not self._running:
            logger.warning("JobManager is not running - cannot refresh schedules")
            return
            
        logger.info(f"Queuing schedule refresh request for expert {expert_instance_id or 'all experts'}")
        
        # Queue the refresh request for asynchronous processing
        control_message = ControlMessage(
            message_type=ControlMessageType.REFRESH_EXPERT_SCHEDULES,
            expert_instance_id=expert_instance_id
        )
        
        try:
            self._control_queue.put_nowait(control_message)
            logger.debug(f"Schedule refresh request queued for expert {expert_instance_id or 'all experts'}")
        except queue.Full:
            logger.error("Control queue is full - cannot queue schedule refresh request", exc_info=True)
    
    def _refresh_expert_schedules_sync(self, expert_instance_id: int = None):
        """
        Internal method to synchronously refresh scheduled jobs.
        This runs on the control thread to avoid blocking the UI.
        
        Args:
            expert_instance_id: If provided, only refresh this expert's schedule.
                              If None, refresh all expert schedules.
        """
        logger.info(f"Refreshing expert schedules for expert {expert_instance_id or 'all experts'}")
        
        with self._lock:
            if expert_instance_id:
                # Remove existing jobs for this expert by checking job_id pattern
                logger.debug(f"Current _scheduled_jobs before removal: {list(self._scheduled_jobs.keys())}")
                jobs_to_remove = [job_id for job_id in self._scheduled_jobs.keys() 
                                if job_id.startswith(f"expert_{expert_instance_id}_")]
                logger.debug(f"Jobs to remove for expert {expert_instance_id}: {jobs_to_remove}")
                
                for job_id in jobs_to_remove:
                    try:
                        self._scheduler.remove_job(job_id)
                        del self._scheduled_jobs[job_id]
                        logger.debug(f"Removed existing job {job_id} for expert {expert_instance_id}")
                    except Exception as e:
                        logger.warning(f"Error removing job {job_id}: {e}")
                
                logger.debug(f"_scheduled_jobs after removal: {list(self._scheduled_jobs.keys())}")

                # Re-schedule this expert's jobs
                logger.debug(f"Re-scheduling jobs for expert {expert_instance_id}")
                self._schedule_expert_analysis(expert_instance_id)
                logger.debug(f"_scheduled_jobs after re-scheduling: {list(self._scheduled_jobs.keys())}")
            else:
                # Refresh all schedules
                self._scheduler.remove_all_jobs()
                self._scheduled_jobs.clear()
                self._schedule_all_expert_jobs()
        
        logger.info("Expert schedules refreshed successfully")
    
    def _schedule_expert_analysis(self, expert_instance_id: int):
        """Schedule analysis jobs for a specific expert instance by ID."""
        try:
            expert_instance = get_instance(ExpertInstance, expert_instance_id)
            if not expert_instance:
                logger.warning(f"Expert instance {expert_instance_id} not found")
                return
                
            if not expert_instance.enabled:
                logger.debug(f"Expert instance {expert_instance_id} is disabled - skipping scheduling")
                return
                
            self._schedule_expert_jobs(expert_instance)
            
        except Exception as e:
            logger.error(f"Error scheduling jobs for expert instance {expert_instance_id}: {e}", exc_info=True)
        
    def shutdown(self):
        """Shutdown the job manager completely."""
        self.stop()
        self._scheduler.shutdown()
        logger.info("JobManager shutdown complete")
        
    def submit_market_analysis(self, expert_instance_id: int, symbol: str, subtype: str = AnalysisUseCase.ENTER_MARKET, priority: int = 0) -> str:
        """
        Submit a manual analysis job to the worker queue.
        
        Args:
            expert_instance_id: The expert instance ID
            symbol: The symbol to analyze
            subtype: Analysis use case (AnalysisUseCase.ENTER_MARKET or AnalysisUseCase.OPEN_POSITIONS)
            priority: Task priority (lower = higher priority)
            
        Returns:
            Task ID for tracking
            
        Raises:
            ValueError: If expert instance not found or duplicate task exists
            RuntimeError: If worker queue is not running
        """
        
        
        # Validate expert instance exists
        expert_instance = get_instance(ExpertInstance, expert_instance_id)
        if not expert_instance:
            raise ValueError(f"Expert instance {expert_instance_id} not found")
            
        if not expert_instance.enabled:
            raise ValueError(f"Expert instance {expert_instance_id} is disabled")
        
        # Validate subtype
        try:
            analysis_use_case = AnalysisUseCase(subtype)
        except ValueError:
            raise ValueError(f"Invalid subtype '{subtype}'. Must be one of: {[e.value for e in AnalysisUseCase]}")
        
        # For ENTER_MARKET analysis, check if existing orders exist for this expert and symbol
        if analysis_use_case == AnalysisUseCase.ENTER_MARKET:
            from .utils import has_existing_orders_for_expert_and_symbol
            from .types import OrderStatus
            
            # Check for orders in states that indicate the expert has already entered the market
            relevant_statuses = [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.FULFILLED]
            
            if has_existing_orders_for_expert_and_symbol(expert_instance_id, symbol, relevant_statuses):
                logger.info(f"Skipping ENTER_MARKET analysis for expert {expert_instance_id}, symbol {symbol}: existing orders found in states {[s.value for s in relevant_statuses]}")
                return None
            
        # Submit to worker queue with subtype
        worker_queue = get_worker_queue()
        task_id = worker_queue.submit_analysis_task(
            expert_instance_id=expert_instance_id,
            symbol=symbol,
            subtype=subtype,
            priority=priority
        )
        
        logger.info(f"Manual analysis job submitted: expert={expert_instance_id}, symbol={symbol}, subtype={subtype}, task_id={task_id}")
        return task_id
        
    def get_job_status(self, task_id: str) -> Optional[AnalysisTask]:
        """Get the status of a specific job/task."""
        worker_queue = get_worker_queue()
        return worker_queue.get_task_status(task_id)
        
    def cancel_job(self, task_id: str) -> bool:
        """Cancel a pending job. Running jobs cannot be cancelled."""
        worker_queue = get_worker_queue()
        return worker_queue.cancel_task(task_id)
        
    def get_all_jobs(self) -> Dict[str, AnalysisTask]:
        """Get all jobs (pending, running, completed, failed)."""
        worker_queue = get_worker_queue()
        return worker_queue.get_all_tasks()
        
    def get_pending_jobs(self) -> Dict[str, AnalysisTask]:
        """Get all pending jobs."""
        worker_queue = get_worker_queue()
        return worker_queue.get_pending_tasks()
        
    def get_running_jobs(self) -> Dict[str, AnalysisTask]:
        """Get all running jobs."""
        worker_queue = get_worker_queue()
        return worker_queue.get_running_tasks()
    
    def clear_running_analysis_on_startup(self):
        """
        Mark all running analysis as failed on startup.
        This handles cases where the application was stopped while analysis was running.
        """
        logger.info("Clearing running analysis on startup...")
        
        try:
            from sqlmodel import select, Session
            from .db import get_db
            from .models import MarketAnalysis
            from .types import MarketAnalysisStatus
            
            with Session(get_db().bind) as session:
                # Find all analysis with RUNNING status
                statement = select(MarketAnalysis).where(
                    MarketAnalysis.status == MarketAnalysisStatus.RUNNING
                )
                running_analyses = session.exec(statement).all()
                
                if running_analyses:
                    logger.info(f"Found {len(running_analyses)} running analysis to mark as failed")
                    
                    for analysis in running_analyses:
                        # Update status to failed
                        analysis.status = MarketAnalysisStatus.FAILED
                        
                        # Update state to include failure reason
                        if not analysis.state:
                            analysis.state = {}
                        
                        analysis.state.update({
                            'startup_cleanup': True,
                            'failure_reason': 'Application was restarted while analysis was running',
                            'cleanup_timestamp': datetime.now().isoformat()
                        })
                        
                        # Explicitly mark the state field as modified for SQLAlchemy
                        from sqlalchemy.orm import attributes
                        attributes.flag_modified(analysis, "state")
                        
                        session.add(analysis)
                        logger.debug(f"Marked analysis {analysis.id} (symbol: {analysis.symbol}) as failed")
                    
                    session.commit()
                    logger.info(f"Successfully marked {len(running_analyses)} running analysis as failed")
                else:
                    logger.info("No running analysis found to clean up")
                    
        except Exception as e:
            logger.error(f"Error clearing running analysis on startup: {e}", exc_info=True)
        
    def refresh_scheduled_jobs(self):
        """Refresh all scheduled jobs by re-reading expert settings."""
        if not self._running:
            logger.warning("JobManager is not running, cannot refresh scheduled jobs")
            return
            
        logger.info("Refreshing scheduled jobs...")
        
        # Remove existing scheduled jobs
        with self._lock:
            for job_id in list(self._scheduled_jobs.keys()):
                self._remove_scheduled_job(job_id)
                
        # Re-schedule all jobs
        self._schedule_all_expert_jobs()
        logger.info("Scheduled jobs refreshed")
        
    def _schedule_all_expert_jobs(self):
        """Schedule analysis jobs for all enabled expert instances."""
        try:
            expert_instances = get_all_instances(ExpertInstance)
            
            for expert_instance in expert_instances:
                if not expert_instance.enabled:
                    continue
                    
                self._schedule_expert_jobs(expert_instance)
                
        except Exception as e:
            logger.error(f"Error scheduling expert jobs: {e}", exc_info=True)
    
    def _schedule_account_refresh_job(self):
        """Schedule the account refresh job based on the app setting."""
        try:
            # Get the account refresh interval from AppSettings (in minutes)
            refresh_interval_str = get_setting("account_refresh_interval")
            refresh_interval_minutes = 5  # Default to 5 minutes
            
            if refresh_interval_str:
                try:
                    refresh_interval_minutes = int(refresh_interval_str)
                except ValueError:
                    logger.warning(f"Invalid account_refresh_interval setting: {refresh_interval_str}, using default of 5 minutes")
            else:
                # Create the setting with default value if it doesn't exist
                from .models import AppSetting
                from .db import add_instance
                setting = AppSetting(
                    key="account_refresh_interval",
                    value_str="5"
                )
                add_instance(setting)
                logger.info("Created account_refresh_interval AppSetting with default value: 5 minutes")
            
            # Create interval trigger
            trigger = IntervalTrigger(minutes=refresh_interval_minutes)
            
            # Schedule the job
            job_id = "account_refresh_job"
            job = self._scheduler.add_job(
                func=self._execute_account_refresh,
                trigger=trigger,
                id=job_id,
                name="Account Refresh Job",
                replace_existing=True,
                max_instances=1,  # Prevent job overlap
                coalesce=True     # Coalesce multiple missed executions
            )
            
            self._scheduled_jobs[job_id] = job
            logger.info(f"Account refresh job scheduled to run every {refresh_interval_minutes} minutes")
            
        except Exception as e:
            logger.error(f"Error scheduling account refresh job: {e}", exc_info=True)
    
    def execute_account_refresh_immediately(self):
        """
        Execute account refresh as an immediate job without blocking the main thread.
        This submits the account refresh to run asynchronously in the background.
        """
        try:
            logger.info("Scheduling immediate account refresh job")
            
            # Add a one-time job that runs immediately
            job_id = f"account_refresh_immediate_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            
            job = self._scheduler.add_job(
                func=self._execute_account_refresh,
                trigger='date',  # Run once at the specified time
                run_date=datetime.now(),  # Run immediately
                id=job_id,
                name="Immediate Account Refresh Job",
                replace_existing=False,
                max_instances=1,  # Prevent job overlap
                coalesce=True     # Coalesce multiple missed executions
            )
            
            logger.info(f"Immediate account refresh job scheduled with ID: {job_id}")
            return job_id
            
        except Exception as e:
            logger.error(f"Error scheduling immediate account refresh job: {e}", exc_info=True)
            return None
    
    def _execute_account_refresh(self):
        """Execute the account refresh job."""
        try:
            logger.info("Executing scheduled account refresh")
            
            # Import here to avoid circular imports
            from .TradeManager import get_trade_manager
            
            # Get the trade manager and call refresh_accounts
            trade_manager = get_trade_manager()
            trade_manager.refresh_accounts()
            
            logger.info("Scheduled account refresh completed")
            
        except Exception as e:
            logger.error(f"Error executing account refresh: {e}", exc_info=True)
            
    def _schedule_expert_jobs(self, expert_instance: ExpertInstance):
        """Schedule jobs for a specific expert instance."""
        try:
            logger.debug(f"Starting job scheduling for expert instance {expert_instance.id}")
            
            # Get enabled instruments for this expert
            enabled_instruments = self._get_enabled_instruments(expert_instance.id)
            if not enabled_instruments:
                logger.debug(f"No enabled instruments found for expert instance {expert_instance.id}")
                return
            
            logger.debug(f"Found {len(enabled_instruments)} enabled instruments for expert {expert_instance.id}: {enabled_instruments}")
            
            # Schedule jobs for enter_market
            enter_market_schedule = self._get_expert_setting(expert_instance.id, "execution_schedule_enter_market")
            if enter_market_schedule:
                logger.debug(f"Found execution_schedule_enter_market for expert {expert_instance.id}: {enter_market_schedule}")
                logger.debug(f"Creating {len(enabled_instruments)} enter_market jobs...")
                
                for i, instrument in enumerate(enabled_instruments):
                    logger.debug(f"Creating enter_market job {i+1}/{len(enabled_instruments)} for instrument {instrument}")
                    self._create_scheduled_job(expert_instance, instrument, enter_market_schedule, AnalysisUseCase.ENTER_MARKET)
                    logger.debug(f"Completed enter_market job creation for instrument {instrument}")
            else:
                logger.debug(f"No execution_schedule_enter_market setting found for expert instance {expert_instance.id}")
            
            # Schedule jobs for open_positions
            open_positions_schedule = self._get_expert_setting(expert_instance.id, "execution_schedule_open_positions")
            if open_positions_schedule:
                logger.debug(f"Found execution_schedule_open_positions for expert {expert_instance.id}: {open_positions_schedule}")
                logger.debug(f"Creating {len(enabled_instruments)} open_positions jobs...")
                
                for i, instrument in enumerate(enabled_instruments):
                    logger.debug(f"Creating open_positions job {i+1}/{len(enabled_instruments)} for instrument {instrument}")
                    self._create_scheduled_job(expert_instance, instrument, open_positions_schedule, AnalysisUseCase.OPEN_POSITIONS)
                    logger.debug(f"Completed open_positions job creation for instrument {instrument}")
            else:
                logger.debug(f"No execution_schedule_open_positions setting found for expert instance {expert_instance.id}")
            
            logger.debug(f"Completed job scheduling for expert instance {expert_instance.id}")
                
        except Exception as e:
            logger.error(f"Error scheduling jobs for expert instance {expert_instance.id}: {e}", exc_info=True)
            
    def _get_expert_setting(self, instance_id: int, key: str) -> Optional[str]:
        """Get a specific setting for an expert instance."""
        try:
            expert = get_expert_instance_from_id(instance_id)
            if key in expert.settings:
                return expert.settings[key]
            else:
                return None                
        except Exception as e:
            logger.error(f"Error getting expert setting {key} for instance {instance_id}: {e}", exc_info=True)
            return None
            
    def _get_enabled_instruments(self, instance_id: int) -> List[str]:
        """Get list of enabled instruments for an expert instance."""
        try:
            # Look for instrument settings (assuming pattern like "instrument_{symbol}_enabled")
            
            enabled_symbols = self._get_expert_setting(instance_id, "enabled_instruments")
            enabled_symbols = list(enabled_symbols.keys()) if enabled_symbols else []                            
            return enabled_symbols
        except Exception as e:
            logger.error(f"Error getting enabled instruments for instance {instance_id}: {e}", exc_info=True)
            return []
            
    def _create_scheduled_job(self, expert_instance: ExpertInstance, symbol: str, schedule_setting: str, subtype: str = AnalysisUseCase.ENTER_MARKET):
        """Create a scheduled job for an expert instance and symbol."""
        try:
            job_id = f"expert_{expert_instance.id}_symbol_{symbol}_subtype_{subtype}"
            
            logger.debug(f"Creating scheduled job: {job_id} with schedule: {schedule_setting}")
            
            # Parse schedule setting (e.g., "daily_9:30", "hourly", "cron:0 9 * * 1-5")
            trigger = self._parse_schedule(schedule_setting)
            if not trigger:
                logger.warning(f"Invalid schedule setting '{schedule_setting}' for expert {expert_instance.id}")
                return
                
            logger.debug(f"Parsed trigger for {job_id}: {trigger}")
            
            # Create the scheduled job with error handling
            try:
                job = self._scheduler.add_job(
                    func=self._execute_scheduled_analysis,
                    args=[expert_instance.id, symbol, subtype],
                    trigger=trigger,
                    id=job_id,
                    name=f"Analysis: Expert {expert_instance.id}, Symbol {symbol}, Subtype {subtype}",
                    replace_existing=True,
                    max_instances=1,  # Prevent job overlap
                    coalesce=True     # Coalesce multiple missed executions
                )
                
                # Note: No lock needed here since caller already holds the lock
                self._scheduled_jobs[job_id] = job
                    
                logger.info(f"Scheduled job created: {job_id} with schedule '{schedule_setting}' for subtype '{subtype}'")
                
            except Exception as scheduler_error:
                logger.error(f"APScheduler error creating job {job_id}: {scheduler_error}", exc_info=True)
                return
            
        except Exception as e:
            logger.error(f"Error creating scheduled job for expert {expert_instance.id}, symbol {symbol}: {e}", exc_info=True)
            
    def _parse_schedule(self, schedule_setting: str) -> Optional[Any]:
        """Parse schedule setting into APScheduler trigger."""
        try:
            # Handle dict schedule configuration (JSON format)
            if isinstance(schedule_setting, dict) and 'days' in schedule_setting and 'times' in schedule_setting:
                logger.debug(f"Parsing JSON schedule: {schedule_setting}")
                
                days = schedule_setting.get('days', {})
                times = schedule_setting.get('times', ['09:30'])
                
                # Convert day names to numbers (Monday=0, Sunday=6)
                day_mapping = {
                    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                    'friday': 4, 'saturday': 5, 'sunday': 6
                }
                
                # Get enabled days
                enabled_days = []
                for day_name, enabled in days.items():
                    if enabled and day_name.lower() in day_mapping:
                        enabled_days.append(day_mapping[day_name.lower()])
                
                if not enabled_days:
                    logger.warning("No days enabled in schedule")
                    return None
                
                if not times:
                    logger.warning("No times specified in schedule")
                    return None
                
                # Create triggers for each time on enabled days
                # For multiple times, we'll use the first time for now
                # TODO: In the future, we could create multiple jobs for different times
                first_time = times[0]
                hour, minute = map(int, first_time.split(':'))
                
                # Create day_of_week string for APScheduler
                day_of_week = ','.join(map(str, sorted(enabled_days)))
                
                logger.info(f"Creating cron trigger: hour={hour}, minute={minute}, day_of_week={day_of_week}")
                return CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week)
            
            # Handle other schedule formats (string-based) - can be extended as needed
            else:
                logger.warning(f"Unsupported schedule format: {type(schedule_setting)} - {schedule_setting}")
                return None
            
        except Exception as e:
            logger.error(f"Error parsing schedule '{schedule_setting}': {e}", exc_info=True)
            return None
            
    def _execute_scheduled_analysis(self, expert_instance_id: int, symbol: str, subtype: str = AnalysisUseCase.ENTER_MARKET):
        """Execute a scheduled analysis job."""
        try:
            logger.info(f"Executing scheduled analysis: expert={expert_instance_id}, symbol={symbol}, subtype={subtype}")
            
            # For OPEN_POSITIONS analysis, only proceed if there are actual open transactions for this symbol
            if subtype == AnalysisUseCase.OPEN_POSITIONS:
                if not self._has_open_transactions_for_symbol(expert_instance_id, symbol):
                    logger.debug(f"Skipping OPEN_POSITIONS analysis for expert {expert_instance_id}, symbol {symbol}: no open transactions found")
                    return
            
            # Submit to worker queue with low priority (higher number = lower priority)
            task_id = self.submit_market_analysis(
                expert_instance_id=expert_instance_id,
                symbol=symbol,
                subtype=subtype,
                priority=10  # Lower priority for scheduled jobs
            )
            
            logger.debug(f"Scheduled analysis submitted with task_id: {task_id}")
            
        except ValueError as e:
            # Handle the case where ENTER_MARKET analysis is skipped due to existing orders
            if "existing orders found" in str(e):
                logger.info(f"Scheduled analysis skipped: {e}")
            else:
                logger.error(f"ValueError in scheduled analysis for expert {expert_instance_id}, symbol {symbol}, subtype {subtype}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Error executing scheduled analysis for expert {expert_instance_id}, symbol {symbol}, subtype {subtype}: {e}", exc_info=True)
    
    def _has_open_transactions_for_symbol(self, expert_instance_id: int, symbol: str) -> bool:
        """Check if there are open transactions for a specific expert and symbol."""
        try:
            from sqlmodel import select, Session
            from .db import get_db
            from .models import Transaction
            from .types import TransactionStatus
            logger.debug(f"Checking for open transactions for expert {expert_instance_id}, symbol {symbol}")
            with Session(get_db().bind) as session:
                # Check for transactions in WAITING or OPENED state for this expert and symbol
                statement = select(Transaction).where(
                    Transaction.symbol == symbol,
                    Transaction.expert_id == expert_instance_id,
                    Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED])
                )
                transactions = session.exec(statement).all()
                
                has_open_transactions = len(transactions) > 0
                
                logger.debug(f"Transaction check for expert {expert_instance_id}, symbol {symbol}: found {len(transactions)} open transactions")
                return has_open_transactions
                
        except Exception as e:
            logger.error(f"Error checking open transactions for expert {expert_instance_id}, symbol {symbol}: {e}", exc_info=True)
            return False
            
    def _remove_scheduled_job(self, job_id: str):
        """Remove a scheduled job."""
        try:
            if job_id in self._scheduled_jobs:
                self._scheduler.remove_job(job_id)
                del self._scheduled_jobs[job_id]
                logger.debug(f"Removed scheduled job: {job_id}")
        except Exception as e:
            logger.error(f"Error removing scheduled job {job_id}: {e}", exc_info=True)
    
    def _start_control_thread(self):
        """Start the control thread for processing asynchronous operations."""
        if self._control_thread and self._control_thread.is_alive():
            logger.warning("Control thread is already running")
            return
            
        self._control_thread_running = True
        self._control_thread = threading.Thread(
            target=self._control_thread_worker,
            name="JobManager-Control",
            daemon=True
        )
        self._control_thread.start()
        logger.info("JobManager control thread started")
    
    def _stop_control_thread(self):
        """Stop the control thread."""
        if not self._control_thread_running:
            return
            
        logger.info("Stopping JobManager control thread...")
        
        # Signal shutdown to control thread
        shutdown_message = ControlMessage(message_type=ControlMessageType.SHUTDOWN)
        try:
            self._control_queue.put_nowait(shutdown_message)
        except queue.Full:
            logger.warning("Control queue full during shutdown - forcing stop")
            
        self._control_thread_running = False
        
        # Wait for control thread to finish
        if self._control_thread and self._control_thread.is_alive():
            self._control_thread.join(timeout=5.0)
            if self._control_thread.is_alive():
                logger.warning("Control thread did not stop within timeout")
            else:
                logger.info("JobManager control thread stopped")
    
    def _control_thread_worker(self):
        """Worker method for the control thread that processes control messages."""
        logger.info("JobManager control thread worker started")
        
        while self._control_thread_running:
            try:
                # Get control message with timeout
                try:
                    message = self._control_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Process the control message
                if message.message_type == ControlMessageType.SHUTDOWN:
                    logger.debug("Control thread received shutdown signal")
                    break
                elif message.message_type == ControlMessageType.REFRESH_EXPERT_SCHEDULES:
                    logger.debug(f"Processing schedule refresh for expert {message.expert_instance_id}")
                    self._refresh_expert_schedules_sync(message.expert_instance_id)
                else:
                    logger.warning(f"Unknown control message type: {message.message_type}")
                
                # Mark task as done
                self._control_queue.task_done()
                
            except Exception as e:
                logger.error(f"Error in control thread worker: {e}", exc_info=True)
        
        logger.info("JobManager control thread worker stopped")


# Global job manager instance
_job_manager_instance: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Get the global job manager instance."""
    global _job_manager_instance
    if _job_manager_instance is None:
        _job_manager_instance = JobManager()
    return _job_manager_instance


def initialize_job_manager():
    """Initialize and start the global job manager."""
    job_manager = get_job_manager()
    if not job_manager._running:
        job_manager.start()


def shutdown_job_manager():
    """Shutdown the global job manager."""
    global _job_manager_instance
    if _job_manager_instance:
        _job_manager_instance.shutdown()
        _job_manager_instance = None