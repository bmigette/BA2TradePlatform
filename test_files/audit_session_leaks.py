"""
Audit Script: Database Session Leak Detection
==============================================

This script identifies potential database session leaks in the codebase.
Session leaks occur when `session = get_db()` is called but the session is never closed.

Usage:
    .venv\Scripts\python.exe test_files\audit_session_leaks.py
"""

import os
import re
from pathlib import Path
from typing import List, Tuple

def find_session_leaks(file_path: Path) -> List[Tuple[int, str]]:
    """
    Find potential session leaks in a Python file.
    
    Returns:
        List of (line_number, code_snippet) tuples where sessions may not be closed.
    """
    leaks = []
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        for i, line in enumerate(lines, start=1):
            # Look for: session = get_db()
            if re.search(r'\s*(?:local_)?session\s*=\s*get_db\(\)', line):
                # Check if it's inside a `with` statement (safe usage)
                if 'with' in line:
                    continue
                
                # Check the following lines for try/finally with session.close()
                has_close = False
                has_try_finally = False
                
                # Look ahead up to 50 lines
                for j in range(i, min(i + 50, len(lines))):
                    if 'finally:' in lines[j]:
                        has_try_finally = True
                    if 'session.close()' in lines[j] and has_try_finally:
                        has_close = True
                        break
                
                if not has_close:
                    leaks.append((i, line.strip()))
    
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    
    return leaks


def audit_codebase(base_path: Path = Path("ba2_trade_platform")):
    """Audit the entire codebase for session leaks."""
    print("=" * 80)
    print("DATABASE SESSION LEAK AUDIT")
    print("=" * 80)
    print()
    
    total_files_checked = 0
    total_leaks_found = 0
    files_with_leaks = []
    
    # Walk through all Python files
    for py_file in base_path.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        
        total_files_checked += 1
        leaks = find_session_leaks(py_file)
        
        if leaks:
            total_leaks_found += len(leaks)
            files_with_leaks.append((py_file, leaks))
    
    # Print results
    if files_with_leaks:
        print(f"⚠️  FOUND {total_leaks_found} POTENTIAL SESSION LEAKS IN {len(files_with_leaks)} FILES:\n")
        
        for file_path, leaks in files_with_leaks:
            print(f"\n📄 {file_path}")
            print("-" * 80)
            for line_num, code in leaks:
                print(f"   Line {line_num:4d}: {code}")
                print(f"   {'':>12}⚠️  Session may not be closed!")
    else:
        print("✅ No obvious session leaks detected!")
    
    print("\n" + "=" * 80)
    print(f"SUMMARY: Checked {total_files_checked} files, found {total_leaks_found} potential leaks")
    print("=" * 80)
    print()
    print("RECOMMENDATIONS:")
    print("1. Use 'with get_db() as session:' for automatic cleanup")
    print("2. If manual session = get_db() is required, wrap in try/finally with session.close()")
    print("3. Reduce pool_size and max_overflow to fail faster on leaks")
    print()


def show_good_pattern():
    """Show the correct pattern for using database sessions."""
    print("\n" + "=" * 80)
    print("CORRECT PATTERNS FOR DATABASE SESSION USAGE")
    print("=" * 80)
    
    print("\n✅ RECOMMENDED: Context Manager (automatically closes)")
    print("-" * 80)
    print("""
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import Transaction
    from sqlmodel import select
    
    with get_db() as session:
        transactions = session.exec(select(Transaction)).all()
        # Use transactions here
    # Session automatically closed when exiting 'with' block
    """)
    
    print("\n✅ ACCEPTABLE: Manual close with try/finally")
    print("-" * 80)
    print("""
    from ba2_trade_platform.core.db import get_db
    
    session = get_db()
    try:
        # Do database operations
        results = session.exec(select(SomeModel)).all()
        for result in results:
            process(result)
    finally:
        session.close()  # MUST close in finally block
    """)
    
    print("\n❌ WRONG: No cleanup (causes connection pool exhaustion)")
    print("-" * 80)
    print("""
    from ba2_trade_platform.core.db import get_db
    
    session = get_db()
    results = session.exec(select(SomeModel)).all()
    # Session never closed - LEAKS CONNECTION!
    """)
    
    print()


if __name__ == "__main__":
    # Change to project root
    script_dir = Path(__file__).parent.parent
    os.chdir(script_dir)
    
    show_good_pattern()
    audit_codebase()
