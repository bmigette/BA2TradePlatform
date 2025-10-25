#!/usr/bin/env python3
"""
Generate test traffic from different countries to populate the geolocation map.
This script simulates HTTP requests from various international IP addresses.
"""

import requests
import time
import threading
from typing import List
import random

# Sample IPs from different countries (real public IPs or common ranges)
INTERNATIONAL_IPS = {
    # North America
    'Canada': '24.48.0.0',
    'USA - New York': '1.1.1.1',
    'USA - California': '8.8.8.8',
    
    # Europe
    'United Kingdom': '2.56.0.0',
    'France': '90.0.0.0',
    'Germany': '3.0.0.0',
    'Spain': '84.0.0.0',
    'Italy': '79.0.0.0',
    'Netherlands': '5.145.0.0',
    'Switzerland': '62.0.0.0',
    'Sweden': '81.0.0.0',
    'Norway': '44.0.0.0',
    
    # Asia
    'China': '110.0.0.0',
    'Japan': '61.0.0.0',
    'India': '103.0.0.0',
    'Singapore': '159.0.0.0',
    'South Korea': '219.0.0.0',
    
    # South America
    'Brazil': '177.0.0.0',
    
    # Oceania
    'Australia': '101.0.0.0',
    
    # Other
    'Russia': '89.0.0.0',
    'Mexico': '187.0.0.0',
}

def generate_traffic(url: str = 'http://localhost:8080/serverperf', num_requests: int = 50, delay_between_requests: float = 0.5):
    """
    Generate HTTP requests from different country IPs to populate the map.
    
    Args:
        url: Target URL
        num_requests: Total number of requests to make
        delay_between_requests: Delay between requests in seconds
    """
    print(f"🌍 Starting to generate traffic from {len(INTERNATIONAL_IPS)} countries...")
    print(f"📊 Total requests: {num_requests}")
    print(f"⏱️  Delay between requests: {delay_between_requests}s\n")
    
    countries = list(INTERNATIONAL_IPS.items())
    successful = 0
    failed = 0
    
    for i in range(num_requests):
        country_name, country_ip = random.choice(countries)
        
        try:
            # Make request with X-Forwarded-For header to simulate the IP
            headers = {
                'X-Forwarded-For': country_ip,
                'User-Agent': f'TrafficGenerator/{country_name}'
            }
            
            response = requests.get(url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                successful += 1
                status = '✅'
            else:
                failed += 1
                status = '⚠️'
            
            print(f"{status} Request {i+1}/{num_requests}: {country_name:20s} ({country_ip:15s}) -> {response.status_code}")
            
        except requests.exceptions.Timeout:
            failed += 1
            print(f"❌ Request {i+1}/{num_requests}: {country_name:20s} ({country_ip:15s}) -> TIMEOUT")
        except requests.exceptions.ConnectionError:
            failed += 1
            print(f"❌ Request {i+1}/{num_requests}: {country_name:20s} ({country_ip:15s}) -> CONNECTION ERROR")
        except Exception as e:
            failed += 1
            print(f"❌ Request {i+1}/{num_requests}: {country_name:20s} ({country_ip:15s}) -> ERROR: {e}")
        
        time.sleep(delay_between_requests)
    
    print(f"\n📈 Traffic generation complete!")
    print(f"✅ Successful: {successful}")
    print(f"❌ Failed: {failed}")
    print(f"📊 Total: {successful + failed}")
    print(f"\n🗺️  Check the map at http://localhost:8080 -> Server Performance -> 🌍 Clients by Country")

def generate_continuous_traffic(url: str = 'http://localhost:8080/serverperf', duration_seconds: int = 300, requests_per_minute: int = 10):
    """
    Generate continuous traffic for a specified duration.
    
    Args:
        url: Target URL
        duration_seconds: How long to generate traffic (in seconds)
        requests_per_minute: How many requests per minute
    """
    delay = 60.0 / requests_per_minute  # Convert requests/minute to delay between requests
    
    print(f"🌍 Starting continuous traffic generation...")
    print(f"⏱️  Duration: {duration_seconds} seconds")
    print(f"📊 Rate: {requests_per_minute} requests/minute (delay: {delay:.1f}s)\n")
    
    start_time = time.time()
    request_count = 0
    
    while time.time() - start_time < duration_seconds:
        elapsed = time.time() - start_time
        remaining = duration_seconds - elapsed
        
        country_name, country_ip = random.choice(list(INTERNATIONAL_IPS.items()))
        
        try:
            headers = {
                'X-Forwarded-For': country_ip,
                'User-Agent': f'TrafficGenerator/{country_name}'
            }
            
            response = requests.get(url, headers=headers, timeout=5)
            request_count += 1
            
            status = '✅' if response.status_code == 200 else '⚠️'
            print(f"{status} [{elapsed:6.1f}s] {country_name:20s} ({country_ip:15s}) -> {response.status_code} | Remaining: {remaining:6.1f}s")
            
        except Exception as e:
            print(f"❌ [{elapsed:6.1f}s] {country_name:20s} ({country_ip:15s}) -> ERROR: {str(e)[:40]}")
        
        time.sleep(delay)
    
    print(f"\n✅ Continuous traffic generation complete!")
    print(f"📊 Total requests sent: {request_count}")
    print(f"🗺️  Check the map at http://localhost:8080 -> Server Performance -> 🌍 Clients by Country")

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        
        if mode == 'continuous':
            # Continuous mode: run for 5 minutes at 20 requests/minute
            duration = int(sys.argv[2]) if len(sys.argv) > 2 else 300
            rate = int(sys.argv[3]) if len(sys.argv) > 3 else 20
            generate_continuous_traffic(duration_seconds=duration, requests_per_minute=rate)
        else:
            # One-time mode with specified number of requests
            num_requests = int(mode) if mode.isdigit() else 50
            generate_traffic(num_requests=num_requests, delay_between_requests=0.5)
    else:
        # Default: 50 requests with 0.5s delay
        generate_traffic(num_requests=50, delay_between_requests=0.5)
    
    print("\n💡 To run again:")
    print("   - One-time: python3 test_files/generate_traffic.py 50")
    print("   - Continuous (5 min at 20/min): python3 test_files/generate_traffic.py continuous")
    print("   - Continuous custom (10 min at 30/min): python3 test_files/generate_traffic.py continuous 600 30")
