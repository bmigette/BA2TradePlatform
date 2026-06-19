# Quick Start Guide - Session 2 and Beyond

## 🚀 For the Next Agent

### Step 1: Understand the Project (5 minutes)
```bash
# Read these files in order:
1. app_spec.txt          # What are we building?
2. feature_list.json     # What needs to be done? (200+ tests)
3. claude-progress.txt   # What's already done?
4. SESSION_1_SUMMARY.md  # Session 1 overview
```

### Step 2: Priority Tasks (Start Here!)

#### Task 1: Copy Data Providers Package ⭐ CRITICAL
```bash
# Copy from BA2TradePlatform
cp -r "C:\Users\basti\OneDrive\Documents\dev\BA2TradePlatform\ba2_trade_platform\modules\dataproviders" backend/

# Verify files copied
ls backend/dataproviders/

# Remove AI providers (keep only API-based providers)
# Mark features 1-2 as passing in feature_list.json
```

#### Task 2: Install Dependencies
```bash
cd backend

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# This will take 10-15 minutes on first install
# Mark feature 4 as passing when complete
```

#### Task 3: Initialize Database
```bash
# Create a script to initialize the database
# See backend/app/models/database.py - call init_db()

# Example:
python -c "from app.models.database import init_db; init_db()"

# Verify tables exist
python -c "from app.models.database import engine; print(engine.table_names())"

# Mark feature 6 as passing
```

#### Task 4: Test Server Startup
```bash
# From backend directory with venv activated
uvicorn app.main:app --reload

# Open browser to:
# http://localhost:8000       - API info
# http://localhost:8000/docs  - Swagger docs
# http://localhost:8000/health - Health check

# Mark feature 4 as passing
```

### Step 3: First Implementation (Dataset API)

Create these files:

**backend/app/schemas/dataset.py** - Pydantic schemas
```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Dict, Any

class DatasetCreate(BaseModel):
    name: str
    ticker: str
    timeframe: str
    # ... etc
```

**backend/app/api/datasets.py** - API endpoints
```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
# ... implement CRUD endpoints
```

**Test each endpoint as you create it:**
- POST /api/datasets - Create
- GET /api/datasets - List all
- GET /api/datasets/:id - Get one
- DELETE /api/datasets/:id - Delete

Mark features 13-16 as passing when all work.

---

## 📋 How to Mark Features as Passing

1. Open feature_list.json
2. Find the feature you completed
3. Change `"passes": false` to `"passes": true`
4. Commit with message describing what you implemented

**NEVER** remove features or edit descriptions!

---

## 🔄 Session End Checklist

Before context fills up:

1. ✅ Update feature_list.json (mark completed features as passing)
2. ✅ Update claude-progress.txt (add new accomplishments)
3. ✅ Commit all code with descriptive messages
4. ✅ Create SESSION_N_SUMMARY.md (your session number)
5. ✅ Leave project in working state (no broken code)

---

## 💡 Pro Tips

### Testing Features
- Always run the actual test steps from feature_list.json
- Don't mark as passing unless all steps succeed
- Test with real data when possible

### Git Commits
```bash
# Good commit message format:
git commit -m "Implement dataset creation API endpoint

- Created DatasetCreate Pydantic schema
- Implemented POST /api/datasets endpoint
- Added validation for ticker and timeframe
- Tested with AAPL 1Y data
- Features 13, 17 now passing

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

### Common Issues

**Import errors?**
- Make sure venv is activated
- Check all dependencies installed
- Verify directory structure

**Database errors?**
- Run init_db() to create tables
- Check .env has correct DATABASE_URL
- Verify SQLite file permissions

**API not responding?**
- Check server is running (uvicorn)
- Verify port 8000 is not in use
- Check logs in backend/logs/

---

## 🎯 Current Status

**Completed**: 0 features passing (foundation only)
**Next Priority**: Features 1-16 (Data providers and Dataset API)
**Total Features**: 200+
**Estimated Sessions**: 20-30

---

## 📁 File Locations

```
project_root/
├── app_spec.txt              # ← Project specification
├── feature_list.json         # ← Test cases (IMPORTANT!)
├── claude-progress.txt       # ← Progress tracking
├── README.md                 # ← Full documentation
├── SESSION_1_SUMMARY.md      # ← Session 1 overview
├── QUICK_START.md           # ← This file
├── init.sh                   # ← Setup script
├── .env                      # ← Configuration
└── backend/
    ├── app/
    │   ├── main.py          # ← FastAPI app
    │   ├── models/          # ← Database models (COMPLETE)
    │   ├── api/             # ← API endpoints (EMPTY)
    │   ├── schemas/         # ← Pydantic schemas (EMPTY)
    │   ├── services/        # ← Business logic (EMPTY)
    │   └── tasks/           # ← Celery tasks (EMPTY)
    ├── dataproviders/       # ← Data providers (TO COPY)
    └── requirements.txt     # ← Dependencies
```

---

## 🆘 If You Get Stuck

1. Read app_spec.txt for context
2. Check feature_list.json for test steps
3. Review claude-progress.txt for what's done
4. Look at existing code for patterns
5. Remember: You have unlimited time across sessions!

---

**Ready? Start with Task 1 above! 🚀**
