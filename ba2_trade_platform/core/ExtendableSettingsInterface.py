from typing import Any, Dict, Optional
from abc import ABC, abstractmethod
from ..logger import logger
from ..core.db import get_instance, get_db, update_instance, add_instance
from sqlmodel import select
import json

class ExtendableSettingsInterface(ABC):
    # Hidden variable for builtin settings that all implementations share
    _builtin_settings: Dict[str, Any] = {}
    
    def _determine_value_type(self, value: Any) -> str:
        """
        Determine the appropriate value type based on the actual value provided.
        
        Args:
            value: The value to analyze
            
        Returns:
            str: The determined type ('str', 'float', 'bool', 'json')
        """
        if isinstance(value, bool):
            return "bool"
        elif isinstance(value, (int, float)):
            return "float"
        elif isinstance(value, (dict, list)):
            return "json"
        else:
            return "str"
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """
        Return a dictionary defining the required configuration/settings for the implementation.
        This provides default settings that can be overridden by subclasses.

        Returns:
            Dict[str, Any]: A dictionary where keys are setting names and values are metadata such as:
                - type: The expected type (str, float, json, bool)
                - required: Whether the setting is mandatory
                - description: Human-readable description of the setting
                - default: Default value for the setting
                - valid_values: List of valid values for the setting (optional)
        """
        pass

    @classmethod
    def get_merged_settings_definitions(cls) -> Dict[str, Any]:
        """
        Return merged settings definitions including both builtin and implementation-specific settings.
        
        Returns:
            Dict[str, Any]: Merged dictionary of all available settings
        """
        # Ensure builtin settings are initialized (for MarketExpertInterface subclasses)
        if hasattr(cls, '_ensure_builtin_settings'):
            cls._ensure_builtin_settings()
        
        # Start with builtin settings
        merged = cls._builtin_settings.copy()
        
        # Add implementation-specific settings (these can override builtin ones if needed)
        implementation_settings = cls.get_settings_definitions()
        if implementation_settings:
            merged.update(implementation_settings)
            
        return merged

    def _save_single_setting(self, session, key: str, value: Any, setting_type: Optional[str] = None):
        """
        Helper method to save a single setting to the database.
        
        Args:
            session: Database session
            key: The setting key
            value: The setting value
            setting_type: Optional type override when no definitions exist
        """
        setting_model = type(self).SETTING_MODEL
        lk_field = type(self).SETTING_LOOKUP_FIELD
        definitions = type(self).get_merged_settings_definitions()
        
        definition = definitions.get(key, {})
        value_type = definition.get("type", None)
        
        # If no definition exists, use setting_type or determine type from the value itself
        if value_type is None:
            if setting_type is not None:
                value_type = setting_type
                logger.debug(f"No definition found for setting '{key}', using provided type: {value_type}")
            else:
                value_type = self._determine_value_type(value)
                logger.debug(f"No definition found for setting '{key}', determined type: {value_type}")
        
        # Find existing setting
        where_kwargs = {lk_field: self.id, "key": key}
        stmt = select(setting_model).filter_by(**where_kwargs)
        setting = session.exec(stmt).first()
        
        # Handle different value types
        if value_type == "json":
            # Validate that JSON values are dict or list objects
            if not isinstance(value, (dict, list)):
                raise ValueError(f"JSON setting '{key}' must be a dict or list, got {type(value).__name__}: {repr(value)}")
            
            if setting:
                setting.value_json = value
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_json": value})
                add_instance(setting, session)
                
        elif value_type == "bool":
            json_value = json.dumps(value)
            if setting:
                setting.value_json = json_value
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_json": json_value})
                add_instance(setting, session)
                
        elif value_type == "float":
            if setting:
                setting.value_float = float(value)
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_float": float(value)})
                add_instance(setting, session)
        else:
            # Default to string
            if setting:
                setting.value_str = str(value)
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_str": str(value)})
                add_instance(setting, session)

    def save_setting(self, key: str, value: Any, setting_type: Optional[str] = None):
        """
        Save a single account setting to the database, converting bool to JSON for storage.
        
        Args:
            key: The setting key
            value: The setting value
            setting_type: Optional type override when no definitions exist.
                         Should not be used to override existing definitions.
                         If not provided and no definition exists, will use _determine_value_type.
        """
        lk_field = type(self).SETTING_LOOKUP_FIELD
        session = get_db()
        
        try:
            self._save_single_setting(session, key, value, setting_type)
            session.commit()
            logger.info(f"Saved setting '{key}' for {lk_field}={self.id}: {value}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving account setting '{key}': {e}", exc_info=True)
            raise
        finally:
            session.close()
            
    def save_settings(self, settings: Dict[str, Any]):
        """
        Save account settings to the database, converting bool to JSON for storage.
        """
        lk_field = type(self).SETTING_LOOKUP_FIELD
        session = get_db()
        
        try:
            for key, (value, setting_type) in settings.items():
                self._save_single_setting(session, key, value, setting_type)
            session.commit()
            logger.info(f"Saved settings for {lk_field}={self.id}: {settings}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving account settings: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def get_all_settings(self) -> Dict[str, Any]:
        """
        Returns all settings from the database for this account instance without applying definitions.
        """
        return self.settings
    
    @property
    def settings(self) -> Dict[str, Any]:
        """
        Loads and returns account settings using the setting_model model
        based on the settings definitions provided by the implementation.
        Handles JSON->bool conversion for bool types.
        Also includes settings from database that don't have definitions.
        """
        setting_model = type(self).SETTING_MODEL
        lk_field = type(self).SETTING_LOOKUP_FIELD
        try:
            definitions = type(self).get_merged_settings_definitions()
            session = get_db()
            statement = select(setting_model).filter_by(**{lk_field: self.id})
            results = session.exec(statement)
            settings_value_from_db = results.all()
            
            # Initialize with definitions (set to None if not found in DB)
            settings = {k : None for k in definitions.keys()}

            for setting in settings_value_from_db:
                definition = definitions.get(setting.key, {})
                value_type = definition.get("type", None)
                
                # If no definition exists, determine type from the stored data
                if value_type is None:
                    if setting.value_json is not None and setting.value_json:  # Non-empty JSON
                        value_type = "json"
                    elif setting.value_float is not None:
                        value_type = "float"
                    else:
                        value_type = "str"
                    #logger.debug(f"Setting '{setting.key}' found in DB but not in definitions, using type: {value_type}")
                
                if value_type == "json":
                    # All JSON values are now stored as Dict objects in the database
                    settings[setting.key] = setting.value_json
                elif value_type == "bool":
                    # Convert JSON string to bool with robust handling of corrupted values
                    try:
                        value = setting.value_json
                        # Handle corrupted/multiply-escaped JSON values
                        while isinstance(value, str) and (value.startswith('"') and value.endswith('"')):
                            try:
                                value = json.loads(value)
                            except json.JSONDecodeError:
                                break
                        
                        # Convert to boolean
                        if isinstance(value, bool):
                            settings[setting.key] = value
                        elif isinstance(value, str):
                            settings[setting.key] = value.lower() == 'true'
                        else:
                            settings[setting.key] = bool(value)
                    except Exception as e:
                        logger.warning(f"Failed to parse boolean setting '{setting.key}': {e}, defaulting to False")
                        settings[setting.key] = False
                elif value_type == "float":
                    settings[setting.key] = setting.value_float
                else:
                    settings[setting.key] = setting.value_str
                    
            #logger.debug(f"Loaded settings for {lk_field}={self.id}: {settings}")
            return settings
        except Exception as e:
            logger.error(f"Error loading account settings: {e}", exc_info=True)
            raise
