from nicegui import ui
from typing import Optional, Dict, List, Callable
from sqlmodel import select
from ...core.models import Instrument
from ...core.db import get_all_instances, get_db
from ...logger import logger


class InstrumentSelector:
    """
    A reusable UI component for selecting and configuring instruments.
    
    Features:
    - Display all instruments in a table
    - Enable/disable instruments via checkboxes
    - Set weight for each instrument (default 100)
    - Filter by category, label, and search text
    - Meant to be imported and used from other pages/components
    """
    
    def __init__(self, on_selection_change: Optional[Callable] = None, instrument_list: Optional[List[str]] = None, hide_weights: bool = False):
        """
        Initialize the InstrumentSelector component.
        
        Args:
            on_selection_change: Optional callback function called when selection changes
            instrument_list: Optional list of instrument names to display. If None, displays all instruments.
            hide_weights: If True, hides weight-related controls and columns. Default is False.
        """
        logger.debug('Initializing InstrumentSelector component')
        self.on_selection_change = on_selection_change
        self.instrument_list = instrument_list
        self.hide_weights = hide_weights
        
        # Filter state
        self.search_filter = ''
        self.category_filter = 'All'
        self.label_filter = 'All'
        
        # Selection state - store selected instrument IDs and their weights
        self.selected_instrument_ids: set = set()
        self.instrument_weights: Dict[int, float] = {}  # maps instrument_id to weight
        
        # UI components
        self.table = None
        self.search_input = None
        self.category_select = None
        self.label_select = None
        
        self._load_instruments()
        
    def _load_instruments(self):
        """Load instruments from database and initialize configuration."""
        logger.debug('Loading instruments for selector')
        try:
            all_instruments = get_all_instances(Instrument)
            
            # Filter instruments if instrument_list is provided
            if self.instrument_list is not None:
                self.instruments = [inst for inst in all_instruments if inst.name in self.instrument_list]
                logger.debug(f'Filtered to {len(self.instruments)} instruments from provided list of {len(self.instrument_list)}')
            else:
                self.instruments = all_instruments
                logger.debug(f'Loaded all {len(self.instruments)} instruments')
            
            # Initialize weights for all instruments
            for instrument in self.instruments:
                if instrument.id not in self.instrument_weights:
                    self.instrument_weights[instrument.id] = 100.0
            
        except Exception as e:
            logger.error(f'Error loading instruments: {e}', exc_info=True)
            self.instruments = []
    
    def _get_unique_categories(self) -> List[str]:
        """Get unique categories from all instruments."""
        categories = set()
        for instrument in self.instruments:
            if instrument.categories:
                categories.update(instrument.categories)
        return sorted(list(categories))
    
    def _get_unique_labels(self) -> List[str]:
        """Get unique labels from all instruments."""
        labels = set()
        for instrument in self.instruments:
            if instrument.labels:
                labels.update(instrument.labels)
        return sorted(list(labels))
    
    def _get_filtered_instruments(self) -> List[Dict]:
        """Get instruments filtered by current filter settings."""
        filtered = []
        
        for instrument in self.instruments:
            # Apply search filter
            if self.search_filter and self.search_filter.lower() not in instrument.name.lower():
                continue
                
            # Apply category filter
            if self.category_filter != 'All':
                if not instrument.categories or self.category_filter not in instrument.categories:
                    continue
                    
            # Apply label filter
            if self.label_filter != 'All':
                if not instrument.labels or self.label_filter not in instrument.labels:
                    continue
            
            # Get current weight
            weight = self.instrument_weights.get(instrument.id, 100.0)
            
            filtered.append({
                'id': instrument.id,
                'name': instrument.name,
                'company_name': getattr(instrument, 'company_name', ''),
                'instrument_type': instrument.instrument_type,
                'categories': ', '.join(instrument.categories) if instrument.categories else '',
                'labels': ', '.join(instrument.labels) if instrument.labels else '',
                'weight': weight
            })
        
        return filtered
    
    def _on_search_change(self):
        """Handle search filter change."""
        logger.debug(f'Search filter changed to: {self.search_filter}')
        self._update_table()
    
    def _on_category_change(self):
        """Handle category filter change."""
        logger.debug(f'Category filter changed to: {self.category_filter}')
        self._update_table()
    
    def _on_label_change(self):
        """Handle label filter change."""
        logger.debug(f'Label filter changed to: {self.label_filter}')
        self._update_table()
    
    def _on_selection_change(self, e):
        """Handle table selection change."""
        # Extract IDs from the selected row objects
        self.selected_instrument_ids = set(row['id'] for row in e.selection)
        logger.debug(f'Selection changed: {len(self.selected_instrument_ids)} instruments selected')
        
        if self.on_selection_change:
            self.on_selection_change(self.get_selected_instruments())
    
    def _on_weight_change(self, instrument_id: int, weight: float):
        """Handle instrument weight change."""
        logger.debug(f'Instrument {instrument_id} weight changed to: {weight}')
        self.instrument_weights[instrument_id] = weight
        
        if self.on_selection_change:
            self.on_selection_change(self.get_selected_instruments())
    
    def _set_weight_to_all_displayed(self, weight: float):
        """Set the same weight to all currently displayed instruments."""
        logger.debug(f'Setting weight {weight} to all displayed instruments')
        
        # Get currently filtered instruments
        filtered_instruments = self._get_filtered_instruments()
        
        # Update weights for all displayed instruments
        for instrument in filtered_instruments:
            self.instrument_weights[instrument['id']] = weight
        
        # Update the table to reflect the changes
        self._update_table()
        
        # Notify of selection change if there are selected instruments
        if self.on_selection_change and self.selected_instrument_ids:
            self.on_selection_change(self.get_selected_instruments())
    
    def _update_table(self):
        """Update table with filtered data."""
        if self.table:
            filtered_instruments = self._get_filtered_instruments()
            self.table.rows = filtered_instruments
            # Force table to update its display
            self.table.update()
            logger.debug(f'Table updated with {len(filtered_instruments)} instruments')
    
    def render(self):
        """Render the InstrumentSelector component."""
        logger.debug('Rendering InstrumentSelector component')
        
        with ui.card().classes('w-full'):
            ui.label('Instrument Selection').classes('text-h6')
            
            # Filter controls
            with ui.row().classes('w-full gap-4 mb-4'):
                self.search_input = ui.input(
                    label='Search instruments', 
                    placeholder='Enter instrument name...'
                )#.bind_value(self, 'filter').classes('flex-1')
                #self.search_input.on('input', lambda: self._on_search_change())
                
                categories = ['All'] + self._get_unique_categories()
                self.category_select = ui.select(
                    categories,
                    label='Category',
                    value='All'
                ).bind_value(self, 'category_filter').classes('w-48')
                self.category_select.on('update:model-value', lambda: self._on_category_change())
                
                labels = ['All'] + self._get_unique_labels()
                self.label_select = ui.select(
                    labels,
                    label='Label',
                    value='All'
                ).bind_value(self, 'label_filter').classes('w-48')
                self.label_select.on('update:model-value', lambda: self._on_label_change())
            
            # Weight control row (only show if weights are not hidden)
            if not self.hide_weights:
                with ui.row().classes('w-full gap-4 mb-4'):
                    ui.label('Set weight for all displayed instruments:').classes('self-center')
                    self.bulk_weight_input = ui.input(
                        label='Weight',
                        value='100',
                        placeholder='Enter weight...'
                    ).classes('w-32')
                    ui.button(
                        'Apply to All Displayed',
                        on_click=lambda: self._set_weight_to_all_displayed(
                            float(self.bulk_weight_input.value) if self.bulk_weight_input.value else 100.0
                        )
                    ).props('color=primary size=sm')
            
            # Table with scrollbar
            # Define base columns
            columns = [
                {'name': 'name', 'label': 'Symbol', 'field': 'name', 'sortable': True},
                {'name': 'company_name', 'label': 'Company', 'field': 'company_name', 'sortable': True},
                {'name': 'instrument_type', 'label': 'Type', 'field': 'instrument_type', 'sortable': True},
                {'name': 'categories', 'label': 'Categories', 'field': 'categories'},
                {'name': 'labels', 'label': 'Labels', 'field': 'labels'},
            ]
            
            # Add weight column only if weights are not hidden
            if not self.hide_weights:
                columns.append({'name': 'weight', 'label': 'Weight', 'field': 'weight', 'align': 'center'})
            
            self.table = ui.table(
                columns=columns,
                rows=self._get_filtered_instruments(),
                row_key='id',
                selection='multiple',
                on_select=self._on_selection_change
            ).classes('w-full').style('max-height: 300px; overflow-y: auto')
            
            
            # Add weight slot and event handler only if weights are not hidden
            if not self.hide_weights:
                self.table.add_slot('body-cell-weight', '''
                    <q-td :props="props">
                        <q-input 
                            :model-value="props.value" 
                            @update:model-value="(val) => $parent.$emit('weightChange', props.row.id, parseFloat(val) || 100)"
                            type="number" 
                            min="0" 
                            step="10"
                            dense
                            style="width: 80px"
                        />
                    </q-td>
                ''')
                
                # Handle events from table slots
                self.table.on('weightChange', lambda e: self._on_weight_change(e.args[0], e.args[1]))
            self.search_input.bind_value(self.table, 'filter')
        logger.debug('InstrumentSelector component rendered')
    
    def get_selected_instruments(self) -> List[Dict]:
        """Get list of selected instruments with their configuration."""
        selected = []
        for instrument_id in self.selected_instrument_ids:
            # Find the instrument details
            instrument = next((i for i in self.instruments if i.id == instrument_id), None)
            if instrument:
                weight = self.instrument_weights.get(instrument_id, 100.0)
                selected.append({
                    'id': instrument_id,
                    'name': instrument.name,
                    'company_name': getattr(instrument, 'company_name', ''),
                    'instrument_type': instrument.instrument_type,
                    'weight': weight,
                    'categories': instrument.categories,
                    'labels': instrument.labels
                })
        return selected
    
    def set_selected_instruments(self, instrument_configs: Dict[int, Dict]):
        """Set the selected instruments and their configuration."""
        # Extract selected IDs and weights
        self.selected_instrument_ids = set()
        for instrument_id, config in instrument_configs.items():
            if config.get('enabled', False):
                self.selected_instrument_ids.add(instrument_id)
                self.instrument_weights[instrument_id] = config.get('weight', 100.0)
        
        # Update table selection if table exists
        if self.table:
            # Find the row objects that correspond to selected IDs
            selected_rows = []
            for row in self.table.rows:
                if row['id'] in self.selected_instrument_ids:
                    selected_rows.append(row)
            self.table.selected = selected_rows
        
        self._update_table()
    
    def refresh(self):
        """Refresh the component by reloading instruments from database."""
        self._load_instruments()
        
        # Update filter options
        if self.category_select:
            categories = ['All'] + self._get_unique_categories()
            self.category_select.options = categories
            
        if self.label_select:
            labels = ['All'] + self._get_unique_labels()
            self.label_select.options = labels
        
        self._update_table()
        logger.debug('InstrumentSelector refreshed')