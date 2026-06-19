@echo off
REM BA2ML Platform Start Script (Windows)
REM Usage: start.bat [backend|frontend|all]

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

set ARG=%1
if "%ARG%"=="" set ARG=all

REM Check if .env exists
if not exist ".env" (
    echo [WARN] .env file not found. Creating from .env.example...
    if exist ".env.example" (
        copy .env.example .env
        echo [OK] .env created. Please edit it with your API keys.
    ) else (
        echo [ERROR] .env.example not found!
        exit /b 1
    )
)

if "%ARG%"=="backend" goto :backend
if "%ARG%"=="frontend" goto :frontend
if "%ARG%"=="all" goto :all
goto :usage

:backend
echo [INFO] Starting backend server...
cd backend

REM Check for virtual environment
if not exist "venv" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo [INFO] Installing dependencies...
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

echo [OK] Backend starting on http://localhost:8000
echo [INFO] API docs: http://localhost:8000/docs
uvicorn app.main:app --host 0.0.0.0 --port 8000
goto :end

:frontend
echo [INFO] Starting frontend server...
cd frontend

REM Check for node_modules
if not exist "node_modules" (
    echo [INFO] Installing npm dependencies...
    call npm install
)

echo [OK] Frontend starting on http://localhost:5173
call npm run dev
goto :end

:all
echo [INFO] Starting BA2ML Platform...
echo.
echo This will open two command windows for backend and frontend.
echo.

REM Start backend in new window
start "BA2ML Backend" cmd /k "cd /d %SCRIPT_DIR%backend && (if not exist venv (python -m venv venv && call venv\Scripts\activate.bat && pip install -r requirements.txt) else (call venv\Scripts\activate.bat)) && uvicorn app.main:app --host 0.0.0.0 --port 8000"

REM Wait a bit for backend to start
timeout /t 3 /nobreak > nul

REM Start frontend in new window
start "BA2ML Frontend" cmd /k "cd /d %SCRIPT_DIR%frontend && (if not exist node_modules (npm install)) && npm run dev"

echo.
echo [OK] BA2ML Platform is starting!
echo.
echo   Backend:  http://localhost:8000
echo   Frontend: http://localhost:5173
echo   API Docs: http://localhost:8000/docs
echo.
echo Close the command windows to stop the servers.
goto :end

:usage
echo Usage: start.bat [backend^|frontend^|all]
echo   backend  - Start only the backend server
echo   frontend - Start only the frontend server
echo   all      - Start both servers (default)
exit /b 1

:end
endlocal
