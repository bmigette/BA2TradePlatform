#!/bin/bash

# BA2ML Platform Start Script (Linux/macOS)
# Usage: ./start.sh [backend|frontend|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check if .env exists
check_env() {
    if [ ! -f ".env" ]; then
        log_warn ".env file not found. Creating from .env.example..."
        if [ -f ".env.example" ]; then
            cp .env.example .env
            log_success ".env created. Please edit it with your API keys."
        else
            log_error ".env.example not found!"
            exit 1
        fi
    fi
}

# Start backend
start_backend() {
    log_info "Starting backend server..."
    cd backend

    # Check for virtual environment
    if [ ! -d "venv" ]; then
        log_info "Creating virtual environment..."
        python3 -m venv venv
        source venv/bin/activate
        log_info "Installing dependencies..."
        pip install -r requirements.txt
    else
        source venv/bin/activate
    fi

    log_success "Backend starting on http://localhost:8000"
    log_info "API docs: http://localhost:8000/docs"
    uvicorn app.main:app --host 0.0.0.0 --port 8000
}

# Start frontend
start_frontend() {
    log_info "Starting frontend server..."
    cd frontend

    # Check for node_modules
    if [ ! -d "node_modules" ]; then
        log_info "Installing npm dependencies..."
        npm install
    fi

    log_success "Frontend starting on http://localhost:5173"
    npm run dev
}

# Start both in background
start_all() {
    log_info "Starting BA2ML Platform..."
    check_env

    # Start backend in background
    log_info "Starting backend in background..."
    cd "$SCRIPT_DIR/backend"
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        source venv/bin/activate
        pip install -r requirements.txt
    else
        source venv/bin/activate
    fi
    uvicorn app.main:app --host 0.0.0.0 --port 8000 &
    BACKEND_PID=$!
    log_success "Backend started (PID: $BACKEND_PID)"

    # Start frontend in background
    log_info "Starting frontend in background..."
    cd "$SCRIPT_DIR/frontend"
    if [ ! -d "node_modules" ]; then
        npm install
    fi
    npm run dev &
    FRONTEND_PID=$!
    log_success "Frontend started (PID: $FRONTEND_PID)"

    echo ""
    log_success "BA2ML Platform is running!"
    echo -e "  Backend:  ${GREEN}http://localhost:8000${NC}"
    echo -e "  Frontend: ${GREEN}http://localhost:5173${NC}"
    echo -e "  API Docs: ${GREEN}http://localhost:8000/docs${NC}"
    echo ""
    log_info "Press Ctrl+C to stop all services"

    # Wait for Ctrl+C
    trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" SIGINT SIGTERM
    wait
}

# Main
case "${1:-all}" in
    backend)
        check_env
        start_backend
        ;;
    frontend)
        start_frontend
        ;;
    all)
        start_all
        ;;
    *)
        echo "Usage: $0 [backend|frontend|all]"
        echo "  backend  - Start only the backend server"
        echo "  frontend - Start only the frontend server"
        echo "  all      - Start both servers (default)"
        exit 1
        ;;
esac
