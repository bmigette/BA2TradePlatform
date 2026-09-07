from typing import Any, Dict, Optional
from abc import ABC, abstractmethod
from ba2_common.logger import logger
from ba2_common.core.db import get_instance, get_db, update_instance, add_instance
from sqlmodel import select
import json


#: Every spelling a bool-declared setting can arrive or be stored as, mapped to its truth.
#: Keys are lowercased strings; real bools and 0/1 ints are handled before the lookup.
_BOOL_WORDS = {
    "true": True, "false": False,
    "1": True, "0": False,
    "yes": True, "no": False,
    "on": True, "off": False,
}


def coerce_bool(value: Any) -> bool:
    """A bool-declared setting's value, whatever spelling it arrived in.

    THE DEFECT THIS EXISTS FOR (parity review 2026-09-07). ``save_settings`` stored a bool as
    ``json.dumps(value)`` with no coercion, so the GA's integer ``1`` was written as the JSON
    string ``"1"``. The reader tested ``value.lower() == 'true'``, which ``"1"`` is not -- so a
    gene the optimizer had turned ON came back OFF. Live instances 6-12 held thirteen such rows:
    ``use_atr_stop``, ``regime_overlay_enabled`` and ``screener_weinstein_stage2_only``
    silently disabled on strategies selected with them enabled.

    Both ends now go through here, so a value round-trips: 1 is written as ``true`` and reads
    back True, and a legacy ``"1"`` already in the database reads back True too.

    Raises ValueError on a spelling nothing can mean -- a bool setting holding "maybe" is a bug
    to surface, not a value to guess at. Silence is what made the original defect survive.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        # Unwrap multiply-escaped JSON ('"\\"true\\""'), which corrupted rows carry.
        unwrapped = value
        while (isinstance(unwrapped, str) and len(unwrapped) > 1
               and unwrapped.startswith('"') and unwrapped.endswith('"')):
            try:
                unwrapped = json.loads(unwrapped)
            except json.JSONDecodeError:
                break
        if isinstance(unwrapped, bool):
            return unwrapped
        if isinstance(unwrapped, str) and unwrapped.strip().lower() in _BOOL_WORDS:
            return _BOOL_WORDS[unwrapped.strip().lower()]
    raise ValueError(f"cannot read {value!r} as a boolean setting value")


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

    def get_setting_with_interface_default(self, setting_key: str, log_warning: bool = True) -> Any:
        """
        Get setting value with automatic fallback to interface-defined default.
        
        This method checks the current settings first, then falls back to the default
        value defined in the interface's get_merged_settings_definitions().
        
        Args:
            setting_key: The setting key to retrieve
            log_warning: Whether to log a warning when using interface default
            
        Returns:
            The setting value or interface default
            
        Raises:
            ValueError: If setting key not found in interface definitions
        """
        # Check if setting exists in current settings
        # Also treat the string "None" as None (bug: str(None) was stored in DB)
        setting_value = self.settings.get(setting_key)
        if setting_value is not None and setting_value != "None":
            return setting_value
        
        # Fall back to interface default
        try:
            merged_defs = type(self).get_merged_settings_definitions()
            if setting_key in merged_defs:
                default_value = merged_defs[setting_key].get('default')
                if log_warning:
                    logger.warning(
                        f"Setting '{setting_key}' not configured for {type(self).__name__} "
                        f"(ID: {getattr(self, 'id', 'unknown')}), using interface default: {default_value}"
                    )
                return default_value
            else:
                raise ValueError(
                    f"Setting '{setting_key}' not found in {type(self).__name__} interface definitions. "
                    f"Available settings: {list(merged_defs.keys())}"
                )
        except Exception as e:
            logger.error(f"Error getting interface default for setting '{setting_key}': {e}")
            raise

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
        
        elif value_type == "list":
            # List values are stored as JSON
            if not isinstance(value, list):
                raise ValueError(f"List setting '{key}' must be a list, got {type(value).__name__}: {repr(value)}")
            
            if setting:
                setting.value_json = value
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_json": value})
                add_instance(setting, session)
                
        elif value_type == "bool":
            # COERCE FIRST. json.dumps(1) is '"1"', which the reader could not recognise as
            # true -- so an optimizer gene arriving as an int silently disabled itself. Store
            # the canonical `true`/`false` and the value survives the round trip.
            json_value = json.dumps(coerce_bool(value))
            if setting:
                setting.value_json = json_value
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_json": json_value})
                add_instance(setting, session)
                
        elif value_type == "int":
            # No dedicated value_int column: store in value_float and clear any
            # legacy value_str so future reads don't fall back to the string.
            if setting:
                setting.value_float = float(int(value))
                setting.value_str = None
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_float": float(int(value))})
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
            # Handle None values - don't store as string "None"
            if value is None:
                str_value = None
            else:
                str_value = str(value)
            
            if setting:
                setting.value_str = str_value
                update_instance(setting, session)
            else:
                setting = setting_model(**{lk_field: self.id, "key": key, "value_str": str_value})
                add_instance(setting, session)

    def save_setting(self, key: str, value: Any, setting_type: Optional[str] = None):
        """
        Save a single account setting to the database, converting bool to JSON for storage.
        Invalidates the settings cache to ensure fresh data on next access.
        
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
            
            # Invalidate cache for account settings
            self._invalidate_settings_cache()
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving account setting '{key}': {e}", exc_info=True)
            raise
        finally:
            session.close()
            
    def save_settings(self, settings: Dict[str, Any]):
        """
        Save account settings to the database, converting bool to JSON for storage.
        Invalidates the settings cache to ensure fresh data on next access.
        """
        lk_field = type(self).SETTING_LOOKUP_FIELD
        session = get_db()
        
        try:
            for key, (value, setting_type) in settings.items():
                self._save_single_setting(session, key, value, setting_type)
            session.commit()
            logger.info(f"Saved settings for {lk_field}={self.id}: {settings}")

            # Invalidate cache for account settings
            self._invalidate_settings_cache()

            # Post-save sanity hook: the point where a DEPLOY lands, so a config that is
            # internally consistent but strategically degenerate gets flagged once, loudly,
            # instead of silently running for weeks. Must come AFTER the cache invalidation
            # so the check reads the values just written, and must never fail the save.
            try:
                self.validate_deployed_settings()
            except Exception as e:  # noqa: BLE001 — a bad check must not block a good save
                logger.warning(f"validate_deployed_settings() failed for {lk_field}={self.id}: {e}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error saving account settings: {e}", exc_info=True)
            raise
        finally:
            session.close()
    
    def validate_deployed_settings(self) -> None:
        """Hook: sanity-check a freshly-saved settings set. Default no-op.

        For configurations that are VALID (every key known, every value in range) but
        strategically degenerate — the kind a GA can converge on and a deploy will happily
        accept. Subclasses should LOG rather than raise: these instances are already live, and
        refusing the save would make an existing bad config unfixable through the normal path.

        Added 2026-08-06 after three live FactorRankers were found running with
        ``top_n >= screener_max_stocks``, which silently makes the GA-optimised factor weights
        do nothing (the ranker keeps its entire candidate pool). Nothing in the deploy path
        looked at the relationship between those two settings.
        """
        return None

    def reset_settings(self):
        """
        Delete ALL existing settings rows for this instance, so a subsequent
        ``save_setting``/``save_settings`` call starts from a clean slate instead of merging
        onto whatever was previously configured. Needed before applying an imported settings
        payload: an import only writes the keys it explicitly contains, so any key absent from
        the payload (e.g. a setting that was never a GA-optimized gene) would otherwise silently
        keep its stale prior value instead of reverting to the class default.
        """
        setting_model = type(self).SETTING_MODEL
        lk_field = type(self).SETTING_LOOKUP_FIELD
        session = get_db()

        try:
            statement = select(setting_model).filter_by(**{lk_field: self.id})
            existing = session.exec(statement).all()
            for setting in existing:
                session.delete(setting)
            session.commit()
            logger.info(f"Reset {len(existing)} setting(s) for {lk_field}={self.id}")

            self._invalidate_settings_cache()
        except Exception as e:
            session.rollback()
            logger.error(f"Error resetting settings: {e}", exc_info=True)
            raise
        finally:
            session.close()

    def _invalidate_settings_cache(self):
        """
        Invalidate the cached settings for this instance and the singleton cache.
        Call this after updating settings in the database.
        """
        # Clear instance-level settings cache
        self._settings_cache = None
        logger.debug(f"Cleared settings cache for {type(self).__name__} id={self.id}")
        
        # Also invalidate the host's singleton instance cache (if any) so fresh data
        # is loaded on next access. The concrete account/expert instance caches are
        # live-platform runtime and are wrapped by the injected InstanceResolver;
        # ba2_common never imports them. The resolver MAY expose an optional
        # ``invalidate_instance(id)`` hook (duck-typed) — when it doesn't (e.g. the
        # unconfigured default, or a backtest with no live cache) this is a no-op.
        try:
            from ba2_common.core.instance_resolver import get_instance_resolver

            resolver = get_instance_resolver()
            invalidate = getattr(resolver, "invalidate_instance", None)
            if callable(invalidate):
                invalidate(self.id)
                logger.debug(f"Invalidated host instance cache for id={self.id}")
        except Exception as e:
            logger.warning(f"Could not invalidate singleton cache: {e}")
    
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
        
        Settings are cached at the instance level to avoid repeated database queries.
        """
        # Check if settings are already cached on this instance
        if hasattr(self, '_settings_cache') and self._settings_cache is not None:
            #logger.debug(f"Returning cached settings for {type(self).__name__} id={self.id}")
            return self._settings_cache
        
        setting_model = type(self).SETTING_MODEL
        lk_field = type(self).SETTING_LOOKUP_FIELD
        try:
            logger.debug(f"Loading settings from database for {type(self).__name__} id={self.id}")
            definitions = type(self).get_merged_settings_definitions()
            with get_db() as session:
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
                
                if value_type == "json" or value_type == "list":
                    # JSON and list values are stored as JSON in the database
                    settings[setting.key] = setting.value_json
                elif value_type == "bool":
                    # Legacy rows written before coerce_bool existed hold '"1"' / '"0"', which
                    # the old `value.lower() == 'true'` test read as False regardless. Reading
                    # through the shared coercion recognises them (see coerce_bool's docstring).
                    try:
                        settings[setting.key] = coerce_bool(setting.value_json)
                    except ValueError as e:
                        # A stored spelling nothing can mean. Still defaults to False so one bad
                        # row cannot take an expert down, but it is now LOUD -- the old handler
                        # swallowed every '"1"' in the database this way, without a word.
                        logger.warning(f"Boolean setting '{setting.key}' holds an unreadable "
                                       f"value ({e}); defaulting to False")
                        settings[setting.key] = False
                elif value_type == "int":
                    if setting.value_float is not None:
                        settings[setting.key] = int(setting.value_float)
                    elif setting.value_str is not None and setting.value_str != "None":
                        # Legacy rows saved before "int" was stored in value_float
                        try:
                            settings[setting.key] = int(setting.value_str)
                        except ValueError:
                            logger.warning(f"Could not parse int setting '{setting.key}' from value_str={setting.value_str!r}")
                            settings[setting.key] = None
                    else:
                        settings[setting.key] = None
                elif value_type == "float":
                    settings[setting.key] = setting.value_float
                else:
                    # Convert string "None" (from str(None) bug) back to Python None
                    value_str = setting.value_str
                    if value_str == "None":
                        settings[setting.key] = None
                    else:
                        settings[setting.key] = value_str
                    
            #logger.debug(f"Loaded settings for {lk_field}={self.id}: {settings}")
            
            # Cache the settings for future access
            self._settings_cache = settings
            return settings
        except Exception as e:
            logger.error(f"Error loading account settings: {e}", exc_info=True)
            raise
