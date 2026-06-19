#!/usr/bin/env python
"""Initialize database tables"""

import sys
sys.path.insert(0, 'backend')

from app.models.database import Base, engine

print("Creating database tables...")
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
