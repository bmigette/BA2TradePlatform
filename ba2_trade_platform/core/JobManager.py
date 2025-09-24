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
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.job import Job

from ..logger import logger
from .db import get_all_instances, get_instance, get_setting
from .models import ExpertInstance, ExpertSetting, Instrument
from .WorkerQueue import get_worker_queue, AnalysisTask
from .types import WorkerTaskStatus
from .types import AnalysisUseCase

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
        
        logger.info("JobManager initialized")
        
    def start(self):
        """Start the job manager and schedule all expert analysis jobs."""
        if self._running:
            logger.warning("JobManager is already running")
            return
            
        with self._lock:
            self._running = True
            
        logger.info("Starting JobManager...")
        self._schedule_all_expert_jobs()
        logger.info("JobManager started successfully")
        
    def stop(self):
        """Stop the job manager and clear all scheduled jobs."""
        if not self._running:
            logger.warning("JobManager is not running")
            return
            
        logger.info("Stopping JobManager...")
        
        with self._lock:
            self._running = False
            
        # Remove all scheduled jobs
        self._scheduler.remove_all_jobs()
        self._scheduled_jobs.clear()
        
        logger.info("JobManager stopped")
    
    def refresh_expert_schedules(self, expert_instance_id: int = None):
        """
        Refresh scheduled jobs for a specific expert or all experts.
        
        Args:
            expert_instance_id: If provided, only refresh this expert's schedule.
                              If None, refresh all expert schedules.
        """
        if not self._running:
            logger.warning("JobManager is not running - cannot refresh schedules")
            return
            
        logger.info(f"Refreshing expert schedules for expert {expert_instance_id or 'all experts'}")
        
        with self._lock:
            if expert_instance_id:
                # Remove existing jobs for this expert
                jobs_to_remove = [job_id for job_id, expert_id in self._scheduled_jobs.items() 
                                if expert_id == expert_instance_id]
                
                for job_id in jobs_to_remove:
                    try:
                        self._scheduler.remove_job(job_id)
                        del self._scheduled_jobs[job_id]
                        logger.debug(f"Removed existing job {job_id} for expert {expert_instance_id}")
                    except Exception as e:
                        logger.warning(f"Error removing job {job_id}: {e}")
                
                # Re-schedule this expert's jobs
                self._schedule_expert_analysis(expert_instance_id)
                
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
        
    def submit_manual_analysis(self, expert_instance_id: int, symbol: str, subtype: str = AnalysisUseCase.ENTER_MARKET, priority: int = 0) -> str:
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
            
    def _schedule_expert_jobs(self, expert_instance: ExpertInstance):
        """Schedule jobs for a specific expert instance."""
        try:
            # Get enabled instruments for this expert
            enabled_instruments = self._get_enabled_instruments(expert_instance.id)
            if not enabled_instruments:
                logger.debug(f"No enabled instruments found for expert instance {expert_instance.id}")
                return
            
            # Schedule jobs for enter_market
            enter_market_schedule = self._get_expert_setting(expert_instance.id, "execution_schedule_enter_market")
            if enter_market_schedule:
                logger.debug(f"Found execution_schedule_enter_market for expert {expert_instance.id}: {enter_market_schedule}")
                for instrument in enabled_instruments:
                    self._create_scheduled_job(expert_instance, instrument, enter_market_schedule, AnalysisUseCase.ENTER_MARKET)
            else:
                logger.debug(f"No execution_schedule_enter_market setting found for expert instance {expert_instance.id}")
            
            # Schedule jobs for open_positions
            open_positions_schedule = self._get_expert_setting(expert_instance.id, "execution_schedule_open_positions")
            if open_positions_schedule:
                logger.debug(f"Found execution_schedule_open_positions for expert {expert_instance.id}: {open_positions_schedule}")
                for instrument in enabled_instruments:
                    self._create_scheduled_job(expert_instance, instrument, open_positions_schedule, AnalysisUseCase.OPEN_POSITIONS)
            else:
                logger.debug(f"No execution_schedule_open_positions setting found for expert instance {expert_instance.id}")
                
        except Exception as e:
            logger.error(f"Error scheduling jobs for expert instance {expert_instance.id}: {e}", exc_info=True)
            
    def _get_expert_setting(self, instance_id: int, key: str) -> Optional[str]:
        """Get a specific setting for an expert instance."""
        try:
            from sqlmodel import select, Session
            from .db import get_db
            import json

            with Session(get_db().bind) as session:
                statement = select(ExpertSetting).where(
                    ExpertSetting.instance_id == instance_id,
                    ExpertSetting.key == key
                )
                setting = session.exec(statement).first()
                if not setting:
                    return None
                
                # Return the appropriate value based on what's populated
                if setting.value_json:
                    # For JSON settings, return the JSON string directly
                    return setting.value_json
                elif setting.value_str:
                    return setting.value_str
                elif setting.value_float is not None:
                    return str(setting.value_float)
                else:
                    return None
                
        except Exception as e:
            logger.error(f"Error getting expert setting {key} for instance {instance_id}: {e}")
            return None
            
    def _get_enabled_instruments(self, instance_id: int) -> List[str]:
        """Get list of enabled instruments for an expert instance."""
        try:
            # Look for instrument settings (assuming pattern like "instrument_{symbol}_enabled")
            from sqlmodel import select, Session
            from .db import get_db
            
            enabled_symbols = []
            
            with Session(get_db().bind) as session:
                statement = select(ExpertSetting).where(
                    ExpertSetting.instance_id == instance_id,
                    ExpertSetting.key.like("instrument_%_enabled")
                )
                settings = session.exec(statement).all()
                
                for setting in settings:
                    if setting.value_str and setting.value_str.lower() == "true":
                        # Extract symbol from key like "instrument_AAPL_enabled"
                        parts = setting.key.split("_")
                        if len(parts) >= 3:
                            symbol = parts[1]
                            enabled_symbols.append(symbol)
                            
            return enabled_symbols
            
        except Exception as e:
            logger.error(f"Error getting enabled instruments for instance {instance_id}: {e}")
            return []
            
    def _create_scheduled_job(self, expert_instance: ExpertInstance, symbol: str, schedule_setting: str, subtype: str = AnalysisUseCase.ENTER_MARKET):
        """Create a scheduled job for an expert instance and symbol."""
        try:
            job_id = f"expert_{expert_instance.id}_symbol_{symbol}_subtype_{subtype}"
            
            # Parse schedule setting (e.g., "daily_9:30", "hourly", "cron:0 9 * * 1-5")
            trigger = self._parse_schedule(schedule_setting)
            if not trigger:
                logger.warning(f"Invalid schedule setting '{schedule_setting}' for expert {expert_instance.id}")
                return
                
            # Create the scheduled job
            job = self._scheduler.add_job(
                func=self._execute_scheduled_analysis,
                args=[expert_instance.id, symbol, subtype],
                trigger=trigger,
                id=job_id,
                name=f"Analysis: Expert {expert_instance.id}, Symbol {symbol}, Subtype {subtype}",
                replace_existing=True
            )
            
            with self._lock:
                self._scheduled_jobs[job_id] = job
                
            logger.info(f"Scheduled job created: {job_id} with schedule '{schedule_setting}' for subtype '{subtype}'")
            
        except Exception as e:
            logger.error(f"Error creating scheduled job for expert {expert_instance.id}, symbol {symbol}: {e}", exc_info=True)
            
    def _parse_schedule(self, schedule_setting: str) -> Optional[Any]:
        """Parse schedule setting into APScheduler trigger."""
        try:     

            schedule_config = json.loads(schedule_setting)
            if isinstance(schedule_config, dict) and 'days' in schedule_config and 'times' in schedule_config:
                return self._parse_json_schedule(schedule_config)
            
        except Exception as e:
            logger.error(f"Error parsing schedule '{schedule_setting}': {e}")
            return None
    
    def _parse_json_schedule(self, schedule_config: dict) -> Optional[Any]:
        """Parse JSON schedule configuration into APScheduler trigger."""
        try:
            days = schedule_config.get('days', {})
            times = schedule_config.get('times', ['09:30'])
            
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
            
        except Exception as e:
            logger.error(f"Error parsing JSON schedule: {e}")
            return None
            
    def _execute_scheduled_analysis(self, expert_instance_id: int, symbol: str, subtype: str = AnalysisUseCase.ENTER_MARKET):
        """Execute a scheduled analysis job."""
        try:
            logger.info(f"Executing scheduled analysis: expert={expert_instance_id}, symbol={symbol}, subtype={subtype}")
            
            # Submit to worker queue with low priority (higher number = lower priority)
            task_id = self.submit_manual_analysis(
                expert_instance_id=expert_instance_id,
                symbol=symbol,
                subtype=subtype,
                priority=10  # Lower priority for scheduled jobs
            )
            
            logger.debug(f"Scheduled analysis submitted with task_id: {task_id}")
            
        except Exception as e:
            logger.error(f"Error executing scheduled analysis for expert {expert_instance_id}, symbol {symbol}, subtype {subtype}: {e}", exc_info=True)
            
    def _remove_scheduled_job(self, job_id: str):
        """Remove a scheduled job."""
        try:
            if job_id in self._scheduled_jobs:
                self._scheduler.remove_job(job_id)
                del self._scheduled_jobs[job_id]
                logger.debug(f"Removed scheduled job: {job_id}")
        except Exception as e:
            logger.error(f"Error removing scheduled job {job_id}: {e}")


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