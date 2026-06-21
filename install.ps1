param(
  [switch]$Editable,    # install the in-repo packages as -e (live source); default = built copy
  [switch]$Ui,          # include the experts [ui] extra (nicegui) in the package chain
  [switch]$Upgrade,     # pass --upgrade so an existing install is re-resolved/updated
  [switch]$TradeOnly,   # only build the trade venv (ba2-trade)
  [switch]$TestOnly,    # only build the test venv (ba2-test)
  [string]$BasePath,    # base folder for the venvs (default: home). Venvs live OUTSIDE the repo.
  [string]$Python,     # force ONE interpreter for both venvs (default: per-app, see below)
  [switch]$NoDb         # skip the DB step (copy old DB -> new location + run migrations)
)
# DB step (after the venvs are built): for each platform, if the app's DB is NOT yet at its
# current (consolidated) location, copy a pre-existing OLD DB there (the OLD file is left in
# place as a backup), then apply that platform's migrations to the target. Paths derive from
# BA2_HOME (default ~/Documents/ba2) and are overridable via env:
#   $env:BA2_OLD_TRADE_DB (default ~/Documents/ba2_trade_platform/db.sqlite)
#   $env:BA2_OLD_TEST_DB  (default ~/Documents/ba2_ml_test_platform/dl_forecasting.db)
# Migrations are idempotent (alembic upgrade head / db_migrate runner) — safe to re-run.
# Self-contained monorepo install: EVERYTHING ships from THIS repo (BA2TradePlatform) — the chain
# packages (packages/common -> providers -> experts), the trade app (repo root) and the test app
# (testplatform/). NO external git / sibling clones are referenced.
$ErrorActionPreference = "Stop"
$Here   = (Resolve-Path $PSScriptRoot).Path                      # the BA2TradePlatform monorepo root
if (-not $BasePath) { $BasePath = $HOME }
$VenvRoot = Join-Path $BasePath "ba2-venvs"                       # <home>/ba2-venvs/{trade,test}

# Interpreter for both venvs: Python 3.12 by default (the backend's pandas-ta requires >=3.12; the
# trade app runs on it too). Override with -Python / $env:PYTHON.
function Resolve-Py {
  if ($Python)     { return $Python }
  if ($env:PYTHON) { return $env:PYTHON }
  try { $p = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null); if ($p) { return $p.Trim() } } catch {}
  return "python"
}
$TradePy = Resolve-Py
$TestPy  = Resolve-Py
Write-Host ">> monorepo root : $Here"
Write-Host ">> venv root     : $VenvRoot"
Write-Host ">> trade python  : $TradePy"
Write-Host ">> test  python  : $TestPy"

# torch wheel selection for the TRADE venv. $env:BA2_TORCH_VARIANT: auto (default) | cpu | cuXXX.
# auto = install the CUDA build when an NVIDIA GPU responds to nvidia-smi, else CPU-only (see
# CLAUDE.md WinError 1114 note for the CPU pin rationale).
$TorchVariant   = if ($env:BA2_TORCH_VARIANT) { $env:BA2_TORCH_VARIANT } else { "auto" }
$CudaWhlDefault = "cu124"

function Get-TorchIndex {
  $variant = $TorchVariant
  if ($variant -eq "auto") {
    $hasGpu = $false
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
      & nvidia-smi *> $null
      if ($LASTEXITCODE -eq 0) { $hasGpu = $true }
    }
    $variant = if ($hasGpu) { $CudaWhlDefault } else { "cpu" }
  }
  return "https://download.pytorch.org/whl/$variant"
}

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
  # Self-contained monorepo: the chain packages ALWAYS ship in-repo under packages/. We never
  # install from an external git (the old sibling repos are gone). -Editable just toggles the
  # -e flag (handled in Install-One); without it the packages install as a built copy.
  $common = Join-Path $Here "packages\common"
  $prov   = Join-Path $Here "packages\providers"
  if ($Ui) { $exp = (Join-Path $Here "packages\experts") + "[ui]" } else { $exp = Join-Path $Here "packages\experts" }
  Install-One -Uv $Uv -Vpy $Vpy -Up $Up -Target $common
  Install-One -Uv $Uv -Vpy $Vpy -Up $Up -Target $prov
  Install-One -Uv $Uv -Vpy $Vpy -Up $Up -Target $exp
}

# requirements.txt for both apps pins ba2trade-* to the package chain. We install the chain
# explicitly (above), so strip those lines to avoid a conflicting re-resolve.
function Install-Reqs {
  param([string]$Uv, [string]$Vpy, [string]$ReqPath, [bool]$Up)
  if (-not (Test-Path $ReqPath)) { return }
  $tmp = New-TemporaryFile
  Get-Content $ReqPath |
    Where-Object { $_ -notmatch '(?i)ba2trade-|BA2TradeCommon|BA2TradeProviders|BA2TradeExperts|packages[\\/](common|providers|experts)' } |
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
    # Pre-install torch so the bare `torch` line in requirements.txt is already satisfied with the
    # right build. Variant auto-detected (CUDA cu124 if an NVIDIA GPU is present, else +cpu).
    $idx = Get-TorchIndex
    Write-Host ">> installing torch from $idx"
    Invoke-Uv -Uv $Uv -Vpy $Vpy -Rest @("torch", "--index-url", $idx)
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
              -AppDir $Here `
              -ReqPath (Join-Path $Here "requirements.txt") `
              -TorchCpu $true -VerifyImport "ba2_common" -BasePy $TradePy
}
if ($doTest) {
  Write-Host "==== TEST venv ===="
  New-AppVenv -Venv (Join-Path $VenvRoot "test") `
              -AppDir (Join-Path $Here "testplatform") `
              -ReqPath (Join-Path $Here "testplatform\backend\requirements.txt") `
              -TorchCpu $false -VerifyImport "ba2_common" -BasePy $TestPy
  # Test-platform frontend (Vite/React UI) deps — so `ba2-test serve` can start the UI.
  $fe = Join-Path $Here "testplatform\frontend"
  if (Test-Path (Join-Path $fe "package.json")) {
    $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if (-not $npm -and (Test-Path "C:\Program Files\nodejs\npm.cmd")) { $npm = "C:\Program Files\nodejs\npm.cmd" }
    if ($npm) {
      Write-Host ">> installing test frontend deps (npm install in testplatform/frontend)"
      Push-Location $fe; & $npm install; $rc = $LASTEXITCODE; Pop-Location
      if ($rc -ne 0) { Write-Host ">> npm install failed (rc=$rc) — run it manually in $fe" }
    } else {
      Write-Host ">> npm not found — skipping frontend deps (install Node.js, then 'npm install' in $fe)"
    }
  }
}

# ---- DB step: copy a pre-existing OLD db to the app's current location (keep source as a
#      backup) + apply that platform's migrations to the target. Idempotent / safe to re-run.
$Ba2Home    = if ($env:BA2_HOME) { $env:BA2_HOME } else { Join-Path $HOME "Documents\ba2" }
$OldTradeDb = if ($env:BA2_OLD_TRADE_DB) { $env:BA2_OLD_TRADE_DB } else { Join-Path $HOME "Documents\ba2_trade_platform\db.sqlite" }
$OldTestDb  = if ($env:BA2_OLD_TEST_DB)  { $env:BA2_OLD_TEST_DB }  else { Join-Path $HOME "Documents\ba2_ml_test_platform\dl_forecasting.db" }
$NewTradeDb = Join-Path $Ba2Home "trade\db.sqlite"
$NewTestDb  = Join-Path $Ba2Home "test\dl_forecasting.db"

function Copy-DbIfNeeded {
  param([string]$Label, [string]$Old, [string]$New)
  $dir = Split-Path -Parent $New
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  if (Test-Path $New) {
    Write-Host ">> $Label`: target DB already at $New — keeping it (no copy)"
  } elseif ((Test-Path $Old) -and ($Old -ne $New)) {
    Write-Host ">> $Label`: copying $Old -> $New (source left in place as backup)"
    Copy-Item -Path $Old -Destination $New
  } else {
    Write-Host ">> $Label`: no old DB at $Old — target created on first app run"
  }
}

if (-not $NoDb -and $doTrade) {
  Write-Host "==== TRADE DB ===="
  Copy-DbIfNeeded -Label "TRADE" -Old $OldTradeDb -New $NewTradeDb
  if (Test-Path $NewTradeDb) {
    $al = Join-Path $VenvRoot "trade\Scripts\alembic.exe"; if (-not (Test-Path $al)) { $al = Join-Path $VenvRoot "trade\bin\alembic" }
    Write-Host ">> migrating TRADE db -> head ($NewTradeDb)"
    $env:BA2_DB_FILE = $NewTradeDb
    Push-Location $Here; & $al upgrade head; $rc = $LASTEXITCODE; Pop-Location
    Remove-Item Env:\BA2_DB_FILE -ErrorAction SilentlyContinue
    if ($rc -eq 0) { Write-Host ">> TRADE migrations applied" } else { Write-Host ">> TRADE migration FAILED (rc=$rc) — continuing" }
  }
}
if (-not $NoDb -and $doTest) {
  Write-Host "==== TEST DB ===="
  Copy-DbIfNeeded -Label "TEST" -Old $OldTestDb -New $NewTestDb
  if (Test-Path $NewTestDb) {
    $pyv = Join-Path $VenvRoot "test\Scripts\python.exe"; if (-not (Test-Path $pyv)) { $pyv = Join-Path $VenvRoot "test\bin\python" }
    Write-Host ">> migrating TEST db ($NewTestDb)"
    $env:DATABASE_PATH = $NewTestDb
    Push-Location (Join-Path $Here "testplatform\backend"); & $pyv "scripts\migrate_db.py"; $rc = $LASTEXITCODE; Pop-Location
    Remove-Item Env:\DATABASE_PATH -ErrorAction SilentlyContinue
    if ($rc -eq 0) { Write-Host ">> TEST migrations applied" } else { Write-Host ">> TEST migration FAILED (rc=$rc) — continuing" }
  }
}

Write-Host ">> done."
if ($doTrade) { Write-Host "   trade venv: $(Join-Path $VenvRoot 'trade')   (ba2-trade)" }
if ($doTest)  { Write-Host "   test  venv: $(Join-Path $VenvRoot 'test')    (ba2-test)" }
