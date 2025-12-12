"""Test script for AI Social Media Sentiment with kimi_k2 model."""

import sys
sys.path.insert(0, ".")

from datetime import datetime
from ba2_trade_platform.modules.dataproviders.socialmedia.AISocialMediaSentiment import AISocialMediaSentiment

def main():
    symbol = "AAPL"
    model = "kimi_k2"
    lookback_days = 3
    
    print(f"Testing AISocialMediaSentiment for {symbol} with {model} model")
    print(f"Lookback: {lookback_days} days")
    print("=" * 60)
    
    provider = AISocialMediaSentiment(model=model)
    
    result = provider.get_social_media_sentiment(
        symbol=symbol,
        end_date=datetime.now(),
        lookback_days=lookback_days,
        format_type="markdown"
    )
    
    print(result)
    print("=" * 60)
    print("Test completed!")

if __name__ == "__main__":
    main()
