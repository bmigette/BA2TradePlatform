#!/usr/bin/env python
"""Initialize backend database tables"""

from app.models.database import Base, engine

# Import all models so they are registered with Base
from app.models.dataset import Dataset
from app.models.optimization_job import OptimizationJob
from app.models.model import Model
from app.models.backtest import Backtest
from app.models.optimization_profile import OptimizationProfile
from app.models.api_key import APIKey

print("Creating database tables in backend/dl_forecasting.db...")
try:
    Base.metadata.create_all(bind=engine)
    print("[OK] All tables created successfully")

    # List tables
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"[OK] Tables in database: {', '.join(tables)}")

except Exception as e:
    print(f"[FAIL] Error creating tables: {e}")
    import traceback
    traceback.print_exc()
