# PowerShell script to update all expert files to use new logger system

$experts = @(
    @{Name="FMPSenateTraderCopy"; Path="ba2_trade_platform\modules\experts\FMPSenateTraderCopy.py"},
    @{Name="FMPSenateTraderWeight"; Path="ba2_trade_platform\modules\experts\FMPSenateTraderWeight.py"}
)

foreach ($expert in $experts) {
    Write-Host "Processing $($expert.Name)..." -ForegroundColor Cyan
    
    $file = $expert.Path
    $content = Get-Content $file -Raw
    
    # Replace import statement
    $content = $content -replace 'from \.\.\.logger import logger', 'from ...logger import get_expert_logger'
    
    # Add logger initialization in __init__ method
    # Find the position after self._load_expert_instance(id) or similar init code
    $initPattern = '(def __init__\(self, id: int\):.*?)(super\(\).__init__\(id\).*?)(\n\s+self\._)'
    if ($content -match $initPattern) {
        $content = $content -replace $initPattern, "`$1`$2`n`n        # Initialize expert-specific logger`n        self.logger = get_expert_logger(`"$($expert.Name)`", id)`$3"
    }
    
    # Replace all logger. with self.logger.
    $content = $content -replace '(\s+)logger\.', '$1self.logger.'
    
    # Save the file
    Set-Content -Path $file -Value $content -NoNewline
    
    Write-Host "✓ Updated $($expert.Name)" -ForegroundColor Green
}

Write-Host "`nAll experts updated successfully!" -ForegroundColor Green
