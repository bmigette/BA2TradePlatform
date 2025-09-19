from typing import Any, Dict, Optional
from abc import ABC, abstractmethod
from ..logger import logger
from ..core.db import get_instance, get_db, update_instance, add_instance
from sqlmodel import select
import json

class ExtendableSettingsInterface(ABC):
    
    @classmethod
    @abstractmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        """
        Return a dictionary defining the required configuration/settings for the account implementation.

        Returns:
            Dict[str, Any]: A dictionary where keys are setting names and values are metadata such as:
                - type: The expected type (str, float, json)
                - required: Whether the setting is mandatory
                - description: Human-readable description of the setting
        """
        pass


    def save_settings(self, settings: Dict[str, Any]):
        """
        Save account settings to the database, converting bool to JSON for storage.
        """

        setting_model = type(self).SETTING_MODEL
        lk_field = type(self).SETTING_LOOKUP_FIELD

        session = get_db()
        definitions = type(self).get_settings_definitions()
        try:
            for key, value in settings.items():
                definition = definitions.get(key, {})
                value_type = definition.get("type", "str")
                # Build dynamic where clause
                where_kwargs = {lk_field: self.id, "key": key}
                stmt = select(setting_model).filter_by(**where_kwargs)
                setting = session.exec(stmt).first()
                if value_type == "json" or value_type == "bool":
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
                    if setting:
                        setting.value_str = str(value)
                        update_instance(setting, session)
                    else:
                        setting = setting_model(**{lk_field: self.id, "key": key, "value_str": str(value)})
                        add_instance(setting, session)
            session.commit()
            logger.info(f"Saved settings for {lk_field}={self.id}: {settings}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving account settings: {e}")
            raise
        finally:
            session.close()
   
    @property
    def settings(self) -> Dict[str, Any]:
        """
        Loads and returns account settings using the setting_model model
        based on the settings definitions provided by the implementation.
        Handles JSON->bool conversion for bool types.
        """
        setting_model = type(self).SETTING_MODEL
        lk_field = getattr(type(self), "SETTING_lk_field", "account_id")
        try:
            definitions = type(self).get_settings_definitions()
            session = get_db()
            statement = select(setting_model).filter_by(**{lk_field: self.id})
            results = session.exec(statement)
            settings_value_from_db = results.all()
            settings = {k : None for k in definitions.keys()}

            for setting in settings_value_from_db:
                definition = definitions.get(setting.key, {})
                value_type = definition.get("type", "str")
                if value_type == "json":
                    settings[setting.key] = setting.value_json
                elif value_type == "bool":
                    # Convert JSON string to bool
                    try:
                        settings[setting.key] = json.loads(setting.value_json)
                    except Exception:
                        settings[setting.key] = False
                elif value_type == "float":
                    settings[setting.key] = setting.value_float
                else:
                    settings[setting.key] = setting.value_str
            logger.info(f"Loaded settings for {lk_field}={self.id}: {settings}")
            return settings
        except Exception as e:
            logger.error(f"Error loading account settings: {e}")
            raise
