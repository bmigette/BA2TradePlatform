param(
  [switch]$Editable,    # install the sibling clones editable (default: git install over SSH @branch)
  [switch]$Ui,          # include the experts [ui] extra (nicegui) in the package chain
  [switch]$Upgrade,     # pass --upgrade so an existing install is re-resolved/updated
  [switch]$TradeOnly,   # only build the trade venv (ba2-trade)
  [switch]$TestOnly,    # only build the test venv (ba2-test)
  [string]$Branch = "dev",
  [string]$BasePath,    # base folder for the venvs (default: home). Venvs live OUTSIDE the git repos.
  [string]$Python      # force ONE interpreter for both venvs (default: per-app, see below)
)
$ErrorActionPreference = "Stop"
$Owner  = "bmigette"
$Here   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path     # dev dir holding the sibling clones
if (-not $BasePath) { $BasePath = $HOME }
$VenvRoot = Join-Path $BasePath "ba2-venvs"                       # <home>/ba2-venvs/{trade,test}

# Interpreter for both venvs: Python 3.12 by default (the backend's pandas-ta requires
# >=3.12; the trade app runs on it too). Override with -Python / $env:PYTHON.
function Resolve-Py {
  if ($Python)     { return $Python }
  if ($env:PYTHON) { return $env:PYTHON }
  try { $p = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null); if ($p) { return $p.Trim() } } catch {}
  return "python"
}
$TradePy = Resolve-Py
$TestPy  = Resolve-Py
Write-Host ">> sibling-clone dir: $Here"
Write-Host ">> venv root        : $VenvRoot"
Write-Host ">> trade python     : $TradePy"
Write-Host ">> test  python     : $TestPy"

# One uv install with explicit args (no array splatting / multiline-if — those mangled args).
function Invoke-Uv {
  param([string]$Uv, [string]$Vpy, [string[]]$Rest)
  $a = @("pip", "install", "--python", $Vpy) + $Rest
  & $Uv @a
  if ($LASTEXITCODE -ne 0) { throw "uv pip install failed (exit $LASTEXITCODE): $($Rest -join ' ')" }
}

# Install one chain package. --no-sources: ignore the [tool.uv.sources] git pins in
# providers/experts so OUR explicit (editable or @branch) install of each package wins.
function Install-One {
  param([string]$Uv, [string]$Vpy, [bool]$Up, [string]$Target)
  $rest = @("--no-sources")
  if ($Up) { $rest += "--upgrade" }
  if ($Editable) { $rest += "-e" }
  $rest += $Target
  Invoke-Uv -Uv $Uv -Vpy $Vpy -Rest $rest
}

function Install-Chain {
  param([string]$Uv, [string]$Vpy, [bool]$Up)
  if ($Editable) {
    $common = Join-Path $Here "BA2TradeCommon"
    $prov   = Join-Path $Here "BA2TradeProviders"
    if ($Ui) { $exp = (Join-Path $Here "BA2TradeExperts") + "[ui]" } else { $exp = Join-Path $Here "BA2TradeExperts" }
  } else {
    $common = "git+ssh://git@github.com/$Owner/BA2TradeCommon.git@$Branch"
    $prov   = "git+ssh://git@github.com/$Owner/BA2TradeProviders.git@$Branch"
    if ($Ui) { $exp = "ba2trade-experts[ui] @ git+ssh://git@github.com/$Owner/BA2TradeExperts.git@$Branch" }
    else     { $exp = "git+ssh://git@github.com/$Owner/BA2TradeExperts.git@$Branch" }
  }
  Install-One -Uv $Uv -Vpy $Vpy -Up $Up -Target $common
  Install-One -Uv $Uv -Vpy $Vpy -Up $Up -Target $prov
  Install-One -Uv $Uv -Vpy $Vpy -Up $Up -Target $exp
}

# requirements.txt for both apps pins ba2trade-* to git@dev. We install the chain
# explicitly (above), so strip those lines to avoid a conflicting re-resolve.
function Install-Reqs {
  param([string]$Uv, [string]$Vpy, [string]$ReqPath, [bool]$Up)
  if (-not (Test-Path $ReqPath)) { return }
  $tmp = New-TemporaryFile
  # Drop any chain reference (PyPI name `ba2trade-*`, git/path to the repos, `-e ../..`),
  # whichever form a requirements.txt uses — the chain is installed explicitly above.
  Get-Content $ReqPath |
    Where-Object { $_ -notmatch '(?i)ba2trade-|BA2TradeCommon|BA2TradeProviders|BA2TradeExperts' } |
    Set-Content $tmp.FullName
  $rest = @(); if ($Up) { $rest += "--upgrade" }
  $rest += @("-r", $tmp.FullName)
  Invoke-Uv -Uv $Uv -Vpy $Vpy -Rest $rest
  Remove-Item $tmp.FullName -Force
}

function New-AppVenv {
  param([string]$Venv, [string]$AppDir, [string]$ReqPath, [bool]$TorchCpu, [string]$VerifyImport, [string]$BasePy)
  Write-Host ">> creating venv at $Venv (base: $BasePy)"
  & $BasePy -m venv $Venv
  $Vpy = Join-Path $Venv "Scripts/python.exe"; if (-not (Test-Path $Vpy)) { $Vpy = Join-Path $Venv "bin/python" }
  Write-Host ">> bootstrapping pip + uv"
  & $Vpy -m pip install --upgrade pip uv | Out-Null
  $Uv = Join-Path $Venv "Scripts/uv.exe"; if (-not (Test-Path $Uv)) { $Uv = Join-Path $Venv "bin/uv" }
  $Up = [bool]$Upgrade

  if ($TorchCpu) {
    # CPU-only torch wheel (Windows) — see CLAUDE.md WinError 1114 note. Pre-install so the
    # bare `torch` line in requirements.txt is already satisfied with a +cpu build.
    Write-Host ">> installing CPU-only torch"
    Invoke-Uv -Uv $Uv -Vpy $Vpy -Rest @("torch", "--index-url", "https://download.pytorch.org/whl/cpu")
  }

  Install-Chain -Uv $Uv -Vpy $Vpy -Up $Up
  Install-Reqs  -Uv $Uv -Vpy $Vpy -ReqPath $ReqPath -Up $Up
  # Register the app's console command (ba2-trade / ba2-test) — deps already in place.
  Invoke-Uv -Uv $Uv -Vpy $Vpy -Rest @("--no-sources", "--no-deps", "-e", $AppDir)

  Write-Host ">> verifying $Venv"
  & $Vpy -c "import $VerifyImport; print('ok', $VerifyImport.__version__)"
  if ($LASTEXITCODE -ne 0) { throw "verify import failed in $Venv" }
}

$doTrade = -not $TestOnly
$doTest  = -not $TradeOnly

if ($doTrade) {
  Write-Host "==== TRADE venv ===="
  New-AppVenv -Venv (Join-Path $VenvRoot "trade") `
              -AppDir (Join-Path $Here "BA2TradePlatform") `
              -ReqPath (Join-Path $Here "BA2TradePlatform\requirements.txt") `
              -TorchCpu $true -VerifyImport "ba2_common" -BasePy $TradePy
}
if ($doTest) {
  Write-Host "==== TEST venv ===="
  New-AppVenv -Venv (Join-Path $VenvRoot "test") `
              -AppDir (Join-Path $Here "BA2TestPlatform") `
              -ReqPath (Join-Path $Here "BA2TestPlatform\backend\requirements.txt") `
              -TorchCpu $false -VerifyImport "ba2_common" -BasePy $TestPy
}

Write-Host ">> done."
if ($doTrade) { Write-Host "   trade venv: $(Join-Path $VenvRoot 'trade')   (ba2-trade)" }
if ($doTest)  { Write-Host "   test  venv: $(Join-Path $VenvRoot 'test')    (ba2-test)" }
