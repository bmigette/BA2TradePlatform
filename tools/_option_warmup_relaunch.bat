@echo off
cd /d "C:\Users\basti\Documents\dev\BA2TradePlatform"
C:\Users\basti\Documents\dev\BA2TradePlatform\.venv\Scripts\python.exe C:\Users\basti\Documents\dev\BA2TradePlatform\tools\run_option_warmup_parallel.py --workers 8 --batch-size 100 >> "C:\Users\basti\Documents\dev\BA2TradePlatform\logs_option_warmup\relaunch.log" 2>&1
