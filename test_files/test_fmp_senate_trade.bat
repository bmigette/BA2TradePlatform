@echo off
REM Test FMPSenateTrade Expert
REM Usage: test_fmp_senate_trade.bat [SYMBOL]
REM Example: test_fmp_senate_trade.bat AAPL

cd /d "%~dp0.."

echo.
echo ================================================
echo  Testing FMPSenateTrade Expert
echo ================================================
echo.

if "%1"=="" (
    echo Testing default symbol: AAPL
    .venv\Scripts\python.exe test_files\test_fmp_senate_trade.py
) else (
    echo Testing symbol: %1
    .venv\Scripts\python.exe test_files\test_fmp_senate_trade.py %1
)

echo.
pause
