from abc import abstractmethod
from typing import Dict, Optional
from ...logger import logger


class SmartRiskExpertInterface:
    """
    Abstract interface for experts that support Smart Risk Manager integration.
    Experts implementing this interface can provide detailed analysis data to the Smart Risk Manager.
    """
    
    @abstractmethod
    def get_analysis_summary(self, market_analysis_id: int) -> str:
        """
        Get a concise summary of a market analysis.
        
        Args:
            market_analysis_id: ID of the MarketAnalysis record
            
        Returns:
            str: Human-readable summary (2-3 sentences) covering:
                - Symbol analyzed
                - Overall recommendation (buy/sell/hold)
                - Confidence level
                - Key insights
                
        Example:
            "Analysis of AAPL shows a STRONG BUY recommendation with 85% confidence.
             Technical indicators are bullish with strong momentum. Fundamental analysis
             shows solid earnings growth and reasonable valuation."
        """
        pass
    
    @abstractmethod
    def get_available_outputs(self, market_analysis_id: int) -> Dict[str, str]:
        """
        List all available analysis outputs with descriptions.
        
        Args:
            market_analysis_id: ID of the MarketAnalysis record
            
        Returns:
            Dict[str, str]: Map of output_key -> description
            
        Example for TradingAgents:
            {
                "analyst_fundamentals_output": "Fundamental analysis including P/E, revenue, earnings",
                "analyst_technical_output": "Technical indicators and chart patterns",
                "analyst_sentiment_output": "Market sentiment and news analysis",
                "analyst_risk_output": "Risk assessment and volatility analysis",
                "final_recommendation": "Synthesized recommendation from all analysts"
            }
            
        Note:
            Output keys should be stable identifiers that can be used with get_output_detail().
            Descriptions should be brief (1 sentence) explaining what the output contains.
        """
        pass
    
    @abstractmethod
    def get_output_detail(self, market_analysis_id: int, output_key: str) -> str:
        """
        Get the full content of a specific analysis output.
        
        Args:
            market_analysis_id: ID of the MarketAnalysis record
            output_key: Key of the output to retrieve (from get_available_outputs)
            
        Returns:
            str: Complete output content (can be multi-paragraph, includes all details)
            
        Raises:
            KeyError: If output_key is not valid for this analysis
            ValueError: If market_analysis_id is not found or not accessible
            
        Example:
            For output_key="analyst_fundamentals_output", might return:
            "Fundamental Analysis for AAPL:
             
             Valuation Metrics:
             - P/E Ratio: 28.5 (Industry avg: 25.2)
             - PEG Ratio: 1.8 (Fair value range)
             - Price to Book: 35.2
             
             Financial Health:
             - Revenue Growth: 12% YoY
             - Profit Margin: 25.3%
             - Debt to Equity: 1.2
             
             Recommendation: The company shows strong fundamentals with consistent
             growth and healthy margins. Valuation is slightly elevated but justified
             by growth prospects."
        """
        pass
    
    def supports_smart_risk_manager(self) -> bool:
        """
        Check if this expert supports Smart Risk Manager integration.
        
        Returns:
            bool: True if expert implements all required methods
            
        Note:
            Override this method to return True after implementing all abstract methods.
            Default implementation returns False for backward compatibility.
        """
        return False
