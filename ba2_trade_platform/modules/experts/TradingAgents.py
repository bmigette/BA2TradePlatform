from typing import Any, Dict, List
from sqlmodel import select
from ...core.MarketExpertInterface import MarketExpertInterface
from ...core.models import Instrument, ExpertInstance
from ...core.db import get_db, get_instance
from ...logger import logger


class TradingAgents(MarketExpertInterface):
    """
    Implementation of MarketExpertInterface for AI-powered trading agents.
    
    This class provides market predictions and instrument management
    for AI-based trading strategies.
    """
    
    @classmethod
    def description(cls) -> str:
        return """Expert that provides AI-driven market predictions """
    
    def __init__(self, id: int):
        """
        Initialize the TradingAgent with an expert instance ID.
        
        Args:
            id (int): The ExpertInstance ID from the database
        """
        super().__init__(id)
        logger.debug(f'Initializing TradingAgent with instance id: {id}')
        
        # Load the expert instance from database
        self.instance = get_instance(ExpertInstance, id)
        if not self.instance:
            raise ValueError(f"ExpertInstance with id {id} not found")
        
        logger.info(f'TradingAgent initialized for expert: {self.instance.expert}')
    
    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            
        }
    def get_prediction_for_instrument(self, instrument: str) -> Any:
        """
        Get predictions for a single instrument (symbol).
        
        Args:
            instrument (str): The instrument symbol or identifier.
            
        Returns:
            Dict[str, Any]: Prediction result containing:
                - signal: 'BUY', 'SELL', or 'HOLD'
                - confidence: float between 0 and 1
                - price_target: optional target price
                - reasoning: explanation of the prediction
        """
        logger.debug(f'Getting prediction for instrument: {instrument}')
        
        try:
            # Check if instrument is supported and enabled
            if instrument not in self.get_supported_instruments():
                logger.warning(f'Instrument {instrument} not supported by this expert')
                return self._create_no_prediction_result(instrument, 'Not supported')
            
            if instrument not in self.get_enabled_instruments():
                logger.debug(f'Instrument {instrument} not enabled for this expert instance')
                return self._create_no_prediction_result(instrument, 'Not enabled')
            
            # Get instrument configuration
            enabled_instruments = self._get_enabled_instruments_config()
            instrument_config = enabled_instruments.get(instrument, {})
            
            # Generate prediction based on the expert type and configuration
            prediction = self._generate_prediction(instrument, instrument_config)
            
            logger.debug(f'Generated prediction for {instrument}: {prediction["signal"]} (confidence: {prediction["confidence"]})')
            return prediction
            
        except Exception as e:
            logger.error(f'Error generating prediction for {instrument}: {e}')
            return self._create_error_result(instrument, str(e))
    
    def get_predictions_for_all_enabled_instruments(self) -> Dict[str, Any]:
        """
        Get predictions for all enabled instruments.
        
        Returns:
            Dict[str, Any]: Mapping of instrument symbol to prediction result.
        """
        logger.debug('Getting predictions for all enabled instruments')
        
        predictions = {}
        enabled_instruments = self.get_enabled_instruments()
        
        logger.info(f'Generating predictions for {len(enabled_instruments)} enabled instruments')
        
        for instrument in enabled_instruments:
            try:
                prediction = self.get_prediction_for_instrument(instrument)
                predictions[instrument] = prediction
            except Exception as e:
                logger.error(f'Error getting prediction for {instrument}: {e}')
                predictions[instrument] = self._create_error_result(instrument, str(e))
        
        logger.info(f'Generated {len(predictions)} predictions')
        return predictions
    
    def get_supported_instruments(self) -> List[str]:
        """
        Get a list of all supported instruments for this expert.
        
        Returns:
            List[str]: List of supported instrument symbols/identifiers.
        """
        logger.debug('Getting supported instruments')
        
        try:
            session = get_db()
            statement = select(Instrument)
            results = session.exec(statement)
            instruments = results.all()
            session.close()
            
            # Filter instruments based on expert capabilities
            supported = []
            for instrument in instruments:
                if self._is_instrument_supported(instrument):
                    supported.append(instrument.name)
            
            logger.debug(f'Found {len(supported)} supported instruments')
            return supported
            
        except Exception as e:
            logger.error(f'Error getting supported instruments: {e}')
            return []
    
    def get_enabled_instruments(self) -> List[str]:
        """
        Get a list of all enabled instruments for this expert instance.
        
        Returns:
            List[str]: List of enabled instrument symbols/identifiers.
        """
        logger.debug('Getting enabled instruments')
        
        try:
            enabled_config = self._get_enabled_instruments_config()
            enabled_instruments = list(enabled_config.keys())
            
            logger.debug(f'Found {len(enabled_instruments)} enabled instruments')
            return enabled_instruments
            
        except Exception as e:
            logger.error(f'Error getting enabled instruments: {e}')
            return []
    
    def _get_enabled_instruments_config(self) -> Dict[str, Dict]:
        """
        Get the configuration of enabled instruments from settings.
        
        Returns:
            Dict[str, Dict]: Mapping of instrument symbol to configuration
        """
        # Get enabled instruments from expert settings
        enabled_instruments_setting = self.settings.get('enabled_instruments')
        
        if enabled_instruments_setting and enabled_instruments_setting.value_json:
            return enabled_instruments_setting.value_json
        
        # Return empty dict if no enabled instruments configured
        return {}
    
    def _is_instrument_supported(self, instrument: Instrument) -> bool:
        """
        Check if an instrument is supported by this expert type.
        
        Args:
            instrument (Instrument): The instrument to check
            
        Returns:
            bool: True if supported, False otherwise
        """
        # Basic support check - can be extended based on expert type
        if not instrument.name:
            return False
        
        # Check if instrument type is supported
        expert_type = self.instance.expert if self.instance else 'generic'
        
        # Different expert types might support different instrument types
        if expert_type == 'stock_expert':
            return instrument.instrument_type in ['stock', 'etf']
        elif expert_type == 'crypto_expert':
            return instrument.instrument_type == 'crypto'
        elif expert_type == 'forex_expert':
            return instrument.instrument_type == 'forex'
        else:
            # Generic expert supports all types
            return True
    
    def _generate_prediction(self, instrument: str, config: Dict) -> Dict[str, Any]:
        """
        Generate a prediction for an instrument based on expert configuration.
        
        Args:
            instrument (str): Instrument symbol
            config (Dict): Instrument configuration including weight
            
        Returns:
            Dict[str, Any]: Prediction result
        """
        # This is a placeholder implementation
        # In a real implementation, this would:
        # 1. Fetch market data for the instrument
        # 2. Apply the expert's AI/ML model
        # 3. Consider the instrument's weight in the portfolio
        # 4. Generate signal, confidence, and reasoning
        
        weight = config.get('weight', 100.0)
        
        # Placeholder logic - replace with actual AI prediction
        import random
        signals = ['BUY', 'SELL', 'HOLD']
        signal = random.choice(signals)
        confidence = random.uniform(0.3, 0.9)
        
        # Adjust confidence based on weight (higher weight = more careful analysis)
        if weight > 150:
            confidence *= 1.1  # Higher confidence for high-weight instruments
        elif weight < 50:
            confidence *= 0.8  # Lower confidence for low-weight instruments
        
        confidence = min(confidence, 1.0)  # Cap at 1.0
        
        return {
            'instrument': instrument,
            'signal': signal,
            'confidence': round(confidence, 3),
            'weight': weight,
            'price_target': None,  # Could be calculated based on signal
            'reasoning': f'AI prediction based on market analysis (weight: {weight})',
            'timestamp': self._get_current_timestamp(),
            'expert_id': self.id,
            'expert_type': self.instance.expert if self.instance else 'unknown'
        }
    
    def _create_no_prediction_result(self, instrument: str, reason: str) -> Dict[str, Any]:
        """Create a result for when no prediction can be made."""
        return {
            'instrument': instrument,
            'signal': 'HOLD',
            'confidence': 0.0,
            'weight': 0.0,
            'price_target': None,
            'reasoning': f'No prediction: {reason}',
            'timestamp': self._get_current_timestamp(),
            'expert_id': self.id,
            'error': reason
        }
    
    def _create_error_result(self, instrument: str, error: str) -> Dict[str, Any]:
        """Create a result for when an error occurs."""
        return {
            'instrument': instrument,
            'signal': 'HOLD',
            'confidence': 0.0,
            'weight': 0.0,
            'price_target': None,
            'reasoning': f'Error generating prediction: {error}',
            'timestamp': self._get_current_timestamp(),
            'expert_id': self.id,
            'error': error
        }
    
    def _get_current_timestamp(self) -> str:
        """Get current timestamp for predictions."""
        from datetime import datetime
        return datetime.utcnow().isoformat()
    
    def set_enabled_instruments(self, instrument_configs: Dict[str, Dict]):
        """
        Set the enabled instruments and their configuration.
        
        Args:
            instrument_configs: Dict mapping instrument symbol to config dict
                                containing 'enabled' and 'weight' keys
        """
        logger.debug(f'Setting enabled instruments: {list(instrument_configs.keys())}')
        
        # Filter to only enabled instruments
        enabled_configs = {
            symbol: config for symbol, config in instrument_configs.items()
            if config.get('enabled', False)
        }
        
        # Save to expert settings
        self.save_setting('enabled_instruments', enabled_configs)
        
        logger.info(f'Updated enabled instruments: {len(enabled_configs)} instruments enabled')