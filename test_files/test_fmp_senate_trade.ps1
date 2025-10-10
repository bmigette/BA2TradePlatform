# Test FMPSenateTrade Expert
# Usage: .\test_fmp_senate_trade.ps1 [SYMBOL]
# Example: .\test_fmp_senate_trade.ps1 AAPL

param(
    [string]$Symbol = "AAPL"
)

# Navigate to project root
$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptPath
Set-Location $projectRoot

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host " Testing FMPSenateTrade Expert" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Symbol: $Symbol" -ForegroundColor Yellow
Write-Host ""

# Run the test
& .\.venv\Scripts\python.exe .\test_files\test_fmp_senate_trade.py $Symbol

Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
