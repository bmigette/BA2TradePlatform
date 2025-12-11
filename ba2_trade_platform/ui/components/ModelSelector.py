"""
Model Selector - A reusable UI component for selecting LLM models.

This component displays a table of all supported LLM models with filtering
by labels and providers. The selection result is formatted as provider/model_name.

Usage:
    from ba2_trade_platform.ui.components.ModelSelector import ModelSelector
    
    # Create and render the selector
    selector = ModelSelector(on_selection_change=my_callback)
    selector.render()
    
    # Get the selected model
    selection = selector.get_selected_model()  # Returns "nagaai/gpt5" or None
"""

from nicegui import ui
from typing import Optional, Dict, List, Callable, Any
from ...core.models_registry import (
    MODELS, PROVIDER_CONFIG, 
    get_all_labels, get_all_providers, get_model_display_info,
    format_model_string, get_model_for_provider,
    LABEL_LOW_COST, LABEL_HIGH_COST, LABEL_THINKING, LABEL_WEBSEARCH,
    LABEL_FAST, LABEL_VISION, LABEL_CODING, LABEL_TOOL_CALLING
)
from ...logger import logger


# Label display configuration with colors
LABEL_DISPLAY = {
    LABEL_LOW_COST: {"color": "green", "icon": "attach_money", "display": "Low Cost"},
    LABEL_HIGH_COST: {"color": "red", "icon": "monetization_on", "display": "High Cost"},
    LABEL_THINKING: {"color": "purple", "icon": "psychology", "display": "Thinking"},
    LABEL_WEBSEARCH: {"color": "blue", "icon": "search", "display": "Web Search"},
    LABEL_FAST: {"color": "orange", "icon": "speed", "display": "Fast"},
    LABEL_VISION: {"color": "teal", "icon": "visibility", "display": "Vision"},
    LABEL_CODING: {"color": "indigo", "icon": "code", "display": "Coding"},
    LABEL_TOOL_CALLING: {"color": "cyan", "icon": "build", "display": "Tool Calling"},
}


class ModelSelector:
    """
    A reusable UI component for selecting LLM models.
    
    Features:
    - Display all models in a filterable table
    - Filter by labels (low_cost, thinking, etc.)
    - Filter by provider (OpenAI, NagaAI, etc.)
    - Single selection mode
    - Returns selection in format: provider/friendly_name
    """
    
    def __init__(
        self, 
        on_selection_change: Optional[Callable[[Optional[str]], None]] = None,
        default_provider: str = "native",
        show_native_option: bool = True,
        allowed_providers: Optional[List[str]] = None,
        allowed_labels: Optional[List[str]] = None,
        required_labels: Optional[List[str]] = None,
    ):
        """
        Initialize the ModelSelector component.
        
        Args:
            on_selection_change: Callback when selection changes, receives "provider/model" or None
            default_provider: Default provider to use ("native", "nagaai", "openai", etc.)
            show_native_option: Whether to show "Native" as a provider option
            allowed_providers: Optional list to restrict available providers
            allowed_labels: Optional list to restrict shown labels in filter UI
            required_labels: Optional list of labels that models MUST have to be shown
                           (e.g., ["websearch"] to only show web search capable models)
        """
        logger.debug('Initializing ModelSelector component')
        self.on_selection_change = on_selection_change
        self.default_provider = default_provider.lower()
        self.show_native_option = show_native_option
        self.allowed_providers = [p.lower() for p in allowed_providers] if allowed_providers else None
        self.allowed_labels = allowed_labels
        self.required_labels = set(required_labels) if required_labels else None
        
        # Filter state
        self.search_filter = ''
        self.provider_filter = 'All'
        self.label_filters: set = set()  # Multiple labels can be selected
        
        # Selection state
        self.selected_model: Optional[str] = None  # Friendly name
        self.selected_provider: str = default_provider
        
        # UI components
        self.table = None
        self.search_input = None
        self.provider_select = None
        self.label_checkboxes: Dict[str, Any] = {}
        self.provider_dropdown = None
        self.selected_display = None
        self.result_display = None
        
    def _get_available_providers(self) -> List[str]:
        """Get list of available providers for the dropdown."""
        providers = []
        
        if self.show_native_option:
            providers.append("native")
        
        for provider in get_all_providers():
            if self.allowed_providers is None or provider in self.allowed_providers:
                providers.append(provider)
        
        return providers
    
    def _get_available_labels(self) -> List[str]:
        """Get list of available labels for filtering."""
        all_labels = get_all_labels()
        if self.allowed_labels:
            return [l for l in all_labels if l in self.allowed_labels]
        return sorted(list(all_labels))
    
    def _get_filtered_models(self) -> List[Dict]:
        """Get models filtered by current filter settings."""
        filtered = []
        
        for friendly_name, model_info in MODELS.items():
            # Apply required_labels filter FIRST (hard filter - models must have these labels)
            if self.required_labels:
                model_labels = set(model_info.get("labels", []))
                if not self.required_labels.issubset(model_labels):
                    continue
            
            # Apply search filter
            if self.search_filter:
                search_lower = self.search_filter.lower()
                display_name = model_info.get("display_name", friendly_name).lower()
                description = model_info.get("description", "").lower()
                if search_lower not in display_name and search_lower not in description and search_lower not in friendly_name.lower():
                    continue
            
            # Apply provider filter (check if model is available for the provider)
            available_providers = list(model_info.get("provider_names", {}).keys())
            if self.provider_filter != 'All':
                filter_provider = self.provider_filter.lower()
                if filter_provider == "native":
                    # For native, show all models that have their native provider available
                    native_provider = model_info.get("native_provider", "")
                    if native_provider not in available_providers:
                        continue
                elif filter_provider not in available_providers:
                    continue
            
            # Apply label filter (AND logic - model must have all selected labels from UI checkboxes)
            if self.label_filters:
                model_labels = set(model_info.get("labels", []))
                if not self.label_filters.issubset(model_labels):
                    continue
            
            # Build row data
            labels_display = model_info.get("labels", [])
            
            filtered.append({
                'id': friendly_name,
                'display_name': model_info.get("display_name", friendly_name),
                'description': model_info.get("description", ""),
                'native_provider': PROVIDER_CONFIG.get(model_info.get("native_provider", ""), {}).get("display_name", model_info.get("native_provider", "")),
                'native_provider_key': model_info.get("native_provider", ""),
                'available_providers': ", ".join([PROVIDER_CONFIG.get(p, {}).get("display_name", p) for p in available_providers]),
                'labels': labels_display,
                'labels_display': self._format_labels_display(labels_display),
            })
        
        return filtered
    
    def _format_labels_display(self, labels: List[str]) -> str:
        """Format labels for display with colors."""
        # Return a simple comma-separated list for the table
        display_parts = []
        for label in labels:
            label_info = LABEL_DISPLAY.get(label, {"display": label})
            display_parts.append(label_info.get("display", label))
        return ", ".join(display_parts)
    
    def _on_search_change(self):
        """Handle search filter change."""
        logger.debug(f'Search filter changed to: {self.search_filter}')
        self._update_table()
    
    def _on_provider_filter_change(self):
        """Handle provider filter change."""
        logger.debug(f'Provider filter changed to: {self.provider_filter}')
        self._update_table()
    
    def _on_label_filter_change(self, label: str, checked: bool):
        """Handle label filter checkbox change."""
        if checked:
            self.label_filters.add(label)
        else:
            self.label_filters.discard(label)
        logger.debug(f'Label filters changed to: {self.label_filters}')
        self._update_table()
    
    def _on_selection_change(self, e):
        """Handle table row selection change."""
        if e.selection:
            # Single selection - get the first selected row
            row = e.selection[0]
            self.selected_model = row['id']
            logger.debug(f'Model selected: {self.selected_model}')
        else:
            self.selected_model = None
            logger.debug('Model selection cleared')
        
        self._notify_selection_change()
    
    def _on_provider_dropdown_change(self):
        """Handle provider dropdown change for selected model."""
        logger.debug(f'Selected provider changed to: {self.selected_provider}')
        self._notify_selection_change()
    
    def _notify_selection_change(self):
        """Notify callback of selection change."""
        if self.on_selection_change:
            selection = self.get_selected_model()
            self.on_selection_change(selection)
    
    def _update_table(self):
        """Update table with filtered data."""
        if self.table:
            filtered_models = self._get_filtered_models()
            self.table.rows = filtered_models
            logger.debug(f'Table updated with {len(filtered_models)} models')
    
    def _get_providers_for_model(self, friendly_name: str) -> Dict[str, str]:
        """Get available providers for a specific model as options for dropdown.
        
        Returns:
            Dict mapping provider value to display label, e.g. {"native": "Native (OpenAI)", "nagaai": "NagaAI"}
        """
        model_info = MODELS.get(friendly_name)
        if not model_info:
            return {}
        
        options = {}
        available_providers = model_info.get("provider_names", {}).keys()
        
        # Add native option if enabled
        if self.show_native_option:
            native_provider = model_info.get("native_provider", "")
            if native_provider in available_providers:
                native_display = PROVIDER_CONFIG.get(native_provider, {}).get("display_name", native_provider)
                options["native"] = f"Native ({native_display})"
        
        # Add all available providers
        for provider in available_providers:
            if self.allowed_providers is None or provider in self.allowed_providers:
                provider_display = PROVIDER_CONFIG.get(provider, {}).get("display_name", provider)
                options[provider] = provider_display
        
        return options
    
    def render(self):
        """Render the ModelSelector component."""
        logger.debug('Rendering ModelSelector component')
        
        with ui.card().classes('w-full'):
            ui.label('Model Selection').classes('text-h6')
            
            # Filter controls row
            with ui.row().classes('w-full gap-4 mb-4 items-end'):
                # Search input
                self.search_input = ui.input(
                    label='Search models',
                    placeholder='Enter model name...'
                ).classes('flex-1')
                self.search_input.bind_value(self, 'search_filter')
                self.search_input.on('update:model-value', lambda: self._on_search_change())
                
                # Provider filter dropdown
                provider_options = ['All'] + [
                    PROVIDER_CONFIG.get(p, {}).get("display_name", p) if p != "native" else "Native"
                    for p in self._get_available_providers()
                ]
                provider_values = ['All'] + self._get_available_providers()
                
                self.provider_select = ui.select(
                    options={v: l for v, l in zip(provider_values, provider_options)},
                    label='Filter by Provider',
                    value='All'
                ).bind_value(self, 'provider_filter').classes('w-48')
                self.provider_select.on('update:model-value', lambda: self._on_provider_filter_change())
            
            # Label filter row
            with ui.expansion('Filter by Labels', icon='filter_list').classes('w-full mb-4'):
                with ui.row().classes('gap-4 flex-wrap'):
                    for label in self._get_available_labels():
                        label_info = LABEL_DISPLAY.get(label, {"display": label, "color": "grey", "icon": "label"})
                        with ui.row().classes('items-center gap-1'):
                            cb = ui.checkbox(
                                label_info.get("display", label),
                                on_change=lambda e, l=label: self._on_label_filter_change(l, e.value)
                            )
                            self.label_checkboxes[label] = cb
            
            # Models table
            columns = [
                {'name': 'display_name', 'label': 'Model', 'field': 'display_name', 'sortable': True, 'align': 'left'},
                {'name': 'description', 'label': 'Description', 'field': 'description', 'align': 'left'},
                {'name': 'native_provider', 'label': 'Native Provider', 'field': 'native_provider', 'sortable': True, 'align': 'left'},
                {'name': 'available_providers', 'label': 'Available Providers', 'field': 'available_providers', 'align': 'left'},
                {'name': 'labels_display', 'label': 'Labels', 'field': 'labels_display', 'align': 'left'},
            ]
            
            # Define the selection handler (for checkbox clicks)
            def handle_table_selection(e):
                """Handle table checkbox selection."""
                logger.debug(f'Table selection event: selection={e.selection}')
                if e.selection:
                    # Single selection - get the first selected row
                    row = e.selection[0]
                    self.selected_model = row['id']
                    logger.debug(f'Model selected via checkbox: {self.selected_model}')
                else:
                    self.selected_model = None
                    logger.debug('Model selection cleared')
                
                # Update displays
                self._update_selection_displays()
                
                # Notify callback
                if self.on_selection_change:
                    selection = self.get_selected_model()
                    self.on_selection_change(selection)
            
            # Define the row click handler for selection
            def handle_row_click(e):
                """Handle clicking anywhere on a row to select it."""
                row_data = e.args[1]  # Second argument is the row data
                if row_data:
                    row_id = row_data.get('id')
                    if row_id:
                        self.selected_model = row_id
                        logger.debug(f'Model selected via row click: {self.selected_model}')
                        
                        # Update the table's selected property for visual feedback
                        for row in self.table.rows:
                            if row['id'] == row_id:
                                self.table.selected = [row]
                                break
                        
                        # Update displays
                        self._update_selection_displays()
                        
                        # Notify callback
                        if self.on_selection_change:
                            selection = self.get_selected_model()
                            self.on_selection_change(selection)
            
            # Create table with single selection - clicking row OR checkbox will select it
            self.table = ui.table(
                columns=columns,
                rows=self._get_filtered_models(),
                row_key='id',
                selection='single',
                on_select=handle_table_selection,
            ).classes('w-full cursor-pointer').style('max-height: 400px; overflow-y: auto')
            
            # Make entire row clickable (not just checkbox)
            self.table.props('dense')
            self.table.on('row-click', handle_row_click)
            
            # Selection details
            ui.separator().classes('my-4')
            
            with ui.row().classes('w-full gap-4 items-end'):
                # Selected model display
                with ui.column().classes('flex-1'):
                    ui.label('Selected Model:').classes('text-caption')
                    # Show current model name if already selected
                    initial_display = 'None selected'
                    if self.selected_model:
                        model_info = MODELS.get(self.selected_model)
                        if model_info:
                            initial_display = model_info.get("display_name", self.selected_model)
                    self.selected_display = ui.label(initial_display).classes('text-body1')
                
                # Provider selection for the model
                # Build initial options based on currently selected model (if any)
                if self.selected_model:
                    initial_options = self._get_providers_for_model(self.selected_model)
                else:
                    initial_options = {}
                
                # Ensure we always have at least one option
                if not initial_options:
                    initial_options = {"_placeholder_": "(Select a model first)"}
                    dropdown_value = "_placeholder_"
                else:
                    # Ensure selected_provider is valid for these options
                    if self.selected_provider in initial_options:
                        dropdown_value = self.selected_provider
                    else:
                        dropdown_value = list(initial_options.keys())[0]
                
                # Provider dropdown change handler
                def handle_provider_change():
                    """Handle provider dropdown change."""
                    logger.debug(f'Provider changed to: {self.selected_provider}')
                    result = self.get_selected_model()
                    if self.result_display:
                        self.result_display.text = result if result else ''
                    if self.on_selection_change:
                        self.on_selection_change(result)
                
                with ui.column().classes('w-48'):
                    ui.label('Use Provider:').classes('text-caption')
                    # Create dropdown WITHOUT bind_value first, then bind after
                    self.provider_dropdown = ui.select(
                        options=initial_options,
                        value=dropdown_value,
                        label=''
                    ).classes('w-full')
                    # Now sync the property and set up binding
                    self.selected_provider = dropdown_value
                    self.provider_dropdown.bind_value(self, 'selected_provider')
                    self.provider_dropdown.on('update:model-value', handle_provider_change)
            
            # Result display
            with ui.row().classes('w-full mt-4 items-center'):
                ui.label('Result:').classes('text-caption mr-2')
                # Show current result if model is already selected
                initial_result = ''
                if self.selected_model:
                    initial_result = self.get_selected_model() or ''
                self.result_display = ui.label(initial_result).classes('text-body1 font-mono bg-grey-2 px-2 py-1 rounded')
        
        logger.debug('ModelSelector component rendered')
    
    def _update_selection_displays(self):
        """Update the selection display elements based on current state."""
        if self.selected_model:
            model_info = MODELS.get(self.selected_model)
            if model_info:
                if self.selected_display:
                    self.selected_display.text = model_info.get("display_name", self.selected_model)
                
                # Update provider dropdown options
                provider_options = self._get_providers_for_model(self.selected_model)
                if self.provider_dropdown:
                    self.provider_dropdown.options = provider_options
                
                # Set to native if available, otherwise first provider
                if provider_options:
                    if "native" in provider_options and self.show_native_option:
                        self.selected_provider = "native"
                    else:
                        self.selected_provider = list(provider_options.keys())[0]
        else:
            if self.selected_display:
                self.selected_display.text = 'None selected'
            if self.provider_dropdown:
                self.provider_dropdown.options = {"_placeholder_": "(Select a model first)"}
            self.selected_provider = "_placeholder_"
        
        # Update result display
        result = self.get_selected_model()
        if self.result_display:
            self.result_display.text = result if result else ''
    
    def get_selected_model(self) -> Optional[str]:
        """
        Get the currently selected model in provider/model format.
        
        Returns:
            String in format "provider/friendly_name" (e.g., "nagaai/gpt5")
            or None if no model is selected
        """
        if not self.selected_model:
            return None
        
        return format_model_string(self.selected_model, self.selected_provider)
    
    def set_selected_model(self, model_string: Optional[str]):
        """
        Set the selected model from a provider/model string.
        
        Args:
            model_string: String like "nagaai/gpt5" or just "gpt5" (uses default provider)
        """
        if not model_string:
            self.selected_model = None
            self.selected_provider = self.default_provider
            return
        
        if "/" in model_string:
            provider, friendly_name = model_string.split("/", 1)
            self.selected_provider = provider.lower()
            self.selected_model = friendly_name
        else:
            self.selected_model = model_string
            self.selected_provider = self.default_provider
        
        # Update table selection if rendered
        if self.table:
            # Find the row that matches
            for row in self.table.rows:
                if row['id'] == self.selected_model:
                    self.table.selected = [row]
                    break
        
        # Update display elements if rendered
        if self.selected_display and self.selected_model:
            model_info = MODELS.get(self.selected_model)
            if model_info:
                self.selected_display.text = model_info.get("display_name", self.selected_model)
        
        # Update provider dropdown if rendered
        if self.provider_dropdown and self.selected_model:
            provider_options = self._get_providers_for_model(self.selected_model)
            if provider_options:
                self.provider_dropdown.options = provider_options
                # Ensure selected_provider is valid
                if self.selected_provider not in provider_options:
                    self.selected_provider = list(provider_options.keys())[0]
                self.provider_dropdown.value = self.selected_provider
        
        # Update result display if rendered
        if hasattr(self, 'result_display') and self.result_display:
            result = self.get_selected_model()
            self.result_display.text = result if result else ''
    
    def refresh(self):
        """Refresh the component data."""
        self._update_table()
        logger.debug('ModelSelector refreshed')


class ModelSelectorInput:
    """
    A compact input component that opens a ModelSelector dialog.
    
    This provides a text input showing the current model selection with a
    button to open a full ModelSelector dialog for selection.
    
    Usage:
        model_input = ModelSelectorInput(
            label="Risk Manager Model",
            value="nagaai/gpt5",
            on_change=lambda v: print(f"Selected: {v}")
        )
        model_input.render()
        
        # Get current value
        print(model_input.value)  # "nagaai/gpt5"
    """
    
    def __init__(
        self,
        label: str = "Model",
        value: Optional[str] = None,
        on_change: Optional[Callable[[Optional[str]], None]] = None,
        default_provider: str = "nagaai",
        show_native_option: bool = True,
        allowed_providers: Optional[List[str]] = None,
        allowed_labels: Optional[List[str]] = None,
        required_labels: Optional[List[str]] = None,
        help_text: Optional[str] = None,
    ):
        """
        Initialize the ModelSelectorInput component.
        
        Args:
            label: Label for the input field
            value: Initial value in format "provider/model" (e.g., "nagaai/gpt5")
            on_change: Callback when selection changes
            default_provider: Default provider when opening selector
            show_native_option: Whether to show "Native" as a provider option
            allowed_providers: Optional list to restrict available providers
            allowed_labels: Optional list to restrict shown labels in filter UI
            required_labels: Optional list of labels that models MUST have to be shown
                           (e.g., ["websearch"] to only show web search capable models)
            help_text: Optional help text to display below the input
        """
        self.label = label
        self._value = value
        self.on_change = on_change
        self.default_provider = default_provider
        self.show_native_option = show_native_option
        self.allowed_providers = allowed_providers
        self.allowed_labels = allowed_labels
        self.required_labels = required_labels
        self.help_text = help_text
        
        # UI components
        self.input_field = None
        self.dialog = None
        self.selector = None
    
    @property
    def value(self) -> Optional[str]:
        """Get the current selected model value."""
        return self._value
    
    @value.setter
    def value(self, new_value: Optional[str]):
        """Set the current selected model value."""
        self._value = new_value
        if self.input_field:
            self.input_field.value = self._get_display_value()
    
    def _get_display_value(self) -> str:
        """Get the display value for the input field."""
        if not self._value:
            return ""
        
        # Parse and get display name
        if "/" in self._value:
            provider, friendly_name = self._value.split("/", 1)
        else:
            provider = "native"
            friendly_name = self._value
        
        model_info = get_model_display_info(friendly_name)
        display_name = model_info.get("display_name", friendly_name)
        provider_display = PROVIDER_CONFIG.get(provider, {}).get("display_name", provider)
        
        if provider == "native":
            native_provider = model_info.get("native_provider", "")
            provider_display = f"Native ({PROVIDER_CONFIG.get(native_provider, {}).get('display_name', native_provider)})"
        
        return f"{display_name} ({provider_display})"
    
    def _open_selector_dialog(self):
        """Open the model selector dialog."""
        
        def on_selection_change(selection: Optional[str]):
            """Handle selection change from the selector."""
            pass  # Will be handled on confirm
        
        def on_confirm():
            """Handle dialog confirmation."""
            if self.selector:
                new_value = self.selector.get_selected_model()
                if new_value:
                    self._value = new_value
                    if self.input_field:
                        self.input_field.value = self._get_display_value()
                    if self.on_change:
                        self.on_change(new_value)
            self.dialog.close()
        
        with ui.dialog() as self.dialog, ui.card().classes('w-full max-w-4xl'):
            ui.label(f'Select {self.label}').classes('text-h6 mb-4')
            
            # Create the full selector
            self.selector = ModelSelector(
                on_selection_change=on_selection_change,
                default_provider=self.default_provider,
                show_native_option=self.show_native_option,
                allowed_providers=self.allowed_providers,
                allowed_labels=self.allowed_labels,
                required_labels=self.required_labels,
            )
            
            # Pre-set selection BEFORE render so UI reflects current value
            if self._value:
                # Parse the value to set selected_model and selected_provider
                if "/" in self._value:
                    provider, friendly_name = self._value.split("/", 1)
                    self.selector.selected_provider = provider.lower()
                    self.selector.selected_model = friendly_name
                else:
                    self.selector.selected_model = self._value
                    self.selector.selected_provider = self.default_provider
            
            self.selector.render()
            
            # Update table selection to match
            if self._value and self.selector.table:
                for row in self.selector.table.rows:
                    if row['id'] == self.selector.selected_model:
                        self.selector.table.selected = [row]
                        break
            
            # Dialog buttons
            with ui.row().classes('w-full justify-end mt-4 gap-2'):
                ui.button('Cancel', on_click=self.dialog.close).props('flat')
                ui.button('Select', on_click=on_confirm).props('color=primary')
        
        self.dialog.open()
    
    def render(self):
        """Render the ModelSelectorInput component."""
        with ui.column().classes('w-full gap-1'):
            with ui.row().classes('w-full items-center gap-2'):
                self.input_field = ui.input(
                    label=self.label,
                    value=self._get_display_value(),
                    placeholder='Click to select a model...'
                ).classes('flex-1').props('readonly')
                
                ui.button(icon='tune', on_click=self._open_selector_dialog).props('flat dense')
            
            if self.help_text:
                ui.label(self.help_text).classes('text-body2 text-grey-7 ml-2')

