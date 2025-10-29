"""
Activity Monitor Page

Displays comprehensive activity log with filtering and auto-refresh capabilities.
Tracks all significant system operations including transactions, TP/SL changes, 
risk manager runs, analysis execution, and more.
"""

from nicegui import ui
from sqlmodel import select, or_, and_, Session
from datetime import datetime, timezone
from typing import Optional
import asyncio

from ...core.db import get_db, get_instance
from ...core.models import ActivityLog, ExpertInstance, AccountDefinition
from ...core.types import ActivityLogSeverity, ActivityLogType
from ...logger import logger


class ActivityMonitorPage:
    def __init__(self):
        self.refresh_task: Optional[asyncio.Task] = None
        self.auto_refresh = False
        self.refresh_interval = 5  # seconds
        
        # Filter state
        self.filter_type: Optional[ActivityLogType] = None
        self.filter_severity: Optional[ActivityLogSeverity] = None
        self.filter_expert_id: Optional[int] = None
        self.filter_text: str = ""
        
        # UI components (will be set during render)
        self.activities_table = None
        self.auto_refresh_switch = None
        self.refresh_interval_input = None
        
    async def refresh_activities(self):
        """Refresh the activities table with current filters."""
        if not self.activities_table:
            return
            
        try:
            with Session(get_db().bind) as session:
                # Build query with filters
                query = select(ActivityLog).order_by(ActivityLog.created_at.desc())
                
                filters = []
                
                if self.filter_type:
                    filters.append(ActivityLog.type == self.filter_type)
                    
                if self.filter_severity:
                    # Filter for selected severity and higher (order: DEBUG < INFO < WARNING < SUCCESS < FAILURE)
                    severity_order = [
                        ActivityLogSeverity.DEBUG,
                        ActivityLogSeverity.INFO,
                        ActivityLogSeverity.WARNING,
                        ActivityLogSeverity.SUCCESS,
                        ActivityLogSeverity.FAILURE
                    ]
                    severity_index = severity_order.index(self.filter_severity)
                    filters.append(ActivityLog.severity.in_(severity_order[severity_index:]))
                    
                if self.filter_expert_id and self.filter_expert_id > 0:
                    filters.append(ActivityLog.source_expert_id == self.filter_expert_id)
                    
                if self.filter_text:
                    filters.append(ActivityLog.description.contains(self.filter_text))
                
                if filters:
                    query = query.where(and_(*filters))
                
                # Limit to last 1000 activities for performance
                query = query.limit(1000)
                
                activities = session.exec(query).all()
                
                # Format rows for table
                rows = []
                for activity in activities:
                    # Format expert display
                    expert_display = ""
                    if activity.source_expert_id:
                        expert = get_instance(ExpertInstance, activity.source_expert_id)
                        if expert:
                            if expert.alias:
                                expert_display = expert.alias
                            else:
                                expert_display = f"{expert.expert} #{expert.id}"
                    
                    # Format account display
                    account_display = ""
                    if activity.source_account_id:
                        account = get_instance(AccountDefinition, activity.source_account_id)
                        if account:
                            account_display = account.name
                    
                    rows.append({
                        "id": activity.id,
                        "timestamp": activity.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "severity": activity.severity.value,
                        "type": activity.type.value.replace("_", " ").title(),
                        "expert": expert_display,
                        "account": account_display,
                        "description": activity.description,
                        "data": str(activity.data) if activity.data else ""
                    })
                
                self.activities_table.rows = rows
                
        except Exception as e:
            logger.error(f"Error refreshing activities: {e}", exc_info=True)
            ui.notify(f"Error refreshing activities: {str(e)}", type="negative")
    
    async def auto_refresh_loop(self):
        """Background task for auto-refreshing activities."""
        while self.auto_refresh:
            await self.refresh_activities()
            await asyncio.sleep(self.refresh_interval)
    
    def toggle_auto_refresh(self, enabled: bool):
        """Toggle auto-refresh on/off."""
        self.auto_refresh = enabled
        
        if enabled:
            # Start refresh task
            if not self.refresh_task or self.refresh_task.done():
                self.refresh_task = asyncio.create_task(self.auto_refresh_loop())
                ui.notify("Auto-refresh enabled", type="positive")
        else:
            # Stop refresh task
            if self.refresh_task and not self.refresh_task.done():
                self.refresh_task.cancel()
                ui.notify("Auto-refresh disabled", type="info")
    
    def update_refresh_interval(self, value: str):
        """Update the refresh interval."""
        try:
            interval = int(value)
            if interval > 0:
                self.refresh_interval = interval
                ui.notify(f"Refresh interval set to {interval}s", type="info")
        except ValueError:
            ui.notify("Invalid refresh interval", type="warning")
    
    async def apply_filters(self):
        """Apply filters and refresh the table."""
        await self.refresh_activities()
    
    async def clear_filters(self):
        """Clear all filters and refresh."""
        self.filter_type = None
        self.filter_severity = None
        self.filter_expert_id = None
        self.filter_text = ""
        
        # Reset UI components
        if hasattr(self, 'type_select'):
            self.type_select.value = None
        if hasattr(self, 'severity_select'):
            self.severity_select.value = None
        if hasattr(self, 'expert_select'):
            self.expert_select.value = None
        if hasattr(self, 'text_input'):
            self.text_input.value = ""
        
        await self.refresh_activities()
        ui.notify("Filters cleared", type="info")
    
    def render(self):
        """Render the activity monitor page."""
        ui.markdown("## 📋 Activity Monitor")
        ui.markdown("Track all significant system operations in real-time")
        
        with ui.row().classes("w-full gap-4"):
            # Auto-refresh controls
            with ui.card().classes("p-4"):
                ui.label("Auto-Refresh").classes("font-bold")
                with ui.row().classes("gap-2 items-center"):
                    self.auto_refresh_switch = ui.switch("Enabled", value=False, on_change=lambda e: self.toggle_auto_refresh(e.value))
                    ui.label("Interval (seconds):")
                    self.refresh_interval_input = ui.input(
                        value=str(self.refresh_interval),
                        on_change=lambda e: self.update_refresh_interval(e.value)
                    ).classes("w-20")
                    ui.button("Refresh Now", on_click=self.refresh_activities, icon="refresh").props("outline")
        
        # Filters
        with ui.expansion("🔍 Filters", icon="filter_list").classes("w-full"):
            with ui.grid(columns=4).classes("w-full gap-4 p-4"):
                # Type filter
                with ui.column():
                    ui.label("Activity Type")
                    self.type_select = ui.select(
                        options={None: "All Types", **{t: t.value.replace("_", " ").title() for t in ActivityLogType}},
                        value=None,
                        on_change=lambda e: setattr(self, 'filter_type', e.value)
                    ).classes("w-full")
                
                # Severity filter
                with ui.column():
                    ui.label("Minimum Severity")
                    self.severity_select = ui.select(
                        options={None: "All Severities", **{s: s.value.title() for s in ActivityLogSeverity}},
                        value=None,
                        on_change=lambda e: setattr(self, 'filter_severity', e.value)
                    ).classes("w-full")
                
                # Expert filter
                with ui.column():
                    ui.label("Expert")
                    expert_options = {None: "All Experts"}
                    try:
                        with Session(get_db().bind) as session:
                            experts = session.exec(select(ExpertInstance)).all()
                            for expert in experts:
                                display = expert.alias or f"{expert.expert} #{expert.id}"
                                expert_options[expert.id] = display
                    except Exception as e:
                        logger.error(f"Error loading experts: {e}", exc_info=True)
                    
                    self.expert_select = ui.select(
                        options=expert_options,
                        value=None,
                        on_change=lambda e: setattr(self, 'filter_expert_id', e.value)
                    ).classes("w-full")
                
                # Text search
                with ui.column():
                    ui.label("Text Search")
                    self.text_input = ui.input(
                        placeholder="Search description...",
                        on_change=lambda e: setattr(self, 'filter_text', e.value)
                    ).classes("w-full")
            
            # Filter action buttons
            with ui.row().classes("p-4 gap-2"):
                ui.button("Apply Filters", on_click=self.apply_filters, icon="check").props("color=primary")
                ui.button("Clear Filters", on_click=self.clear_filters, icon="clear").props("outline")
        
        # Activities table
        with ui.card().classes("w-full"):
            self.activities_table = ui.table(
                columns=[
                    {"name": "timestamp", "label": "Timestamp", "field": "timestamp", "align": "left", "sortable": True},
                    {"name": "severity", "label": "Severity", "field": "severity", "align": "center", "sortable": True},
                    {"name": "type", "label": "Type", "field": "type", "align": "left", "sortable": True},
                    {"name": "expert", "label": "Expert", "field": "expert", "align": "left", "sortable": True},
                    {"name": "account", "label": "Account", "field": "account", "align": "left", "sortable": True},
                    {"name": "description", "label": "Description", "field": "description", "align": "left"},
                ],
                rows=[],
                row_key="id",
                pagination={"rowsPerPage": 50, "sortBy": "timestamp", "descending": True}
            ).classes("w-full")
            
            # Add custom styling for severity column
            self.activities_table.add_slot('body-cell-severity', '''
                <q-td :props="props">
                    <q-badge 
                        :color="props.value === 'success' ? 'green' : 
                                props.value === 'failure' ? 'red' : 
                                props.value === 'warning' ? 'orange' :
                                props.value === 'info' ? 'blue' : 'grey'"
                        :label="props.value"
                    />
                </q-td>
            ''')
        
        # Initial load
        asyncio.create_task(self.refresh_activities())


def render():
    """Entry point for the activity monitor page."""
    page = ActivityMonitorPage()
    page.render()
