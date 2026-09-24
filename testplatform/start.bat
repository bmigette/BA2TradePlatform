@echo off
REM BA2 Test Platform start script (Windows) -- a thin wrapper around `ba2-test serve`.
REM Usage: start.bat [backend|frontend|all] [extra `ba2-test serve` flags, e.g. --port 8001 --reload]
REM
REM Needs the test venv built by the installer at the monorepo root (.\install.ps1 -TestOnly).
REM The `ba2-test` command is looked up in this order: %BA2_TEST_BIN%, `ba2-test` on PATH (an
REM activated venv), then the installer's default location %USERPROFILE%\ba2-venvs\test (or
REM %BA2_VENV_BASE%\ba2-venvs\test if the venvs were built with -BasePath). Provider API keys are
REM set in the UI (Settings -> API Keys); no .env file is required.

setlocal

set "MODE="
set "ARG=%~1"
if "%ARG%"=="" set "ARG=all"
if /i "%ARG%"=="backend" set "MODE=back"
if /i "%ARG%"=="frontend" set "MODE=front"
if /i "%ARG%"=="all" set "MODE=both"
if not defined MODE goto :usage

REM Collect any flags after the mode word and pass them through to `ba2-test serve`.
set "EXTRA="
if not "%~1"=="" shift
:collect
if "%~1"=="" goto :resolve
set "EXTRA=%EXTRA% %1"
shift
goto :collect

:resolve
set "BIN=%BA2_TEST_BIN%"
if not defined BIN (
    for /f "delims=" %%i in ('where ba2-test 2^>nul') do if not defined BIN set "BIN=%%i"
)
set "VBASE=%BA2_VENV_BASE%"
if not defined VBASE set "VBASE=%USERPROFILE%"
if not defined BIN if exist "%VBASE%\ba2-venvs\test\Scripts\ba2-test.exe" set "BIN=%VBASE%\ba2-venvs\test\Scripts\ba2-test.exe"
if not defined BIN goto :notfound
if not exist "%BIN%" goto :notfound

echo [INFO] Starting BA2 Test Platform (%MODE%) with %BIN%
"%BIN%" serve --mode %MODE%%EXTRA%
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:notfound
echo [ERROR] ba2-test not found. Build the test venv first, from the monorepo root:
echo         .\install.ps1 -TestOnly -Editable
echo         (or set BA2_TEST_BIN to the full path of ba2-test.exe)
endlocal & exit /b 1

:usage
echo Usage: start.bat [backend^|frontend^|all] [ba2-test serve flags]
echo   backend  - start only the API (http://localhost:8000, docs at /docs)
echo   frontend - start only the Vite UI (http://localhost:5173)
echo   all      - start both (default)
echo Equivalent to: ba2-test serve --mode back^|front^|both [flags]
endlocal & exit /b 1
