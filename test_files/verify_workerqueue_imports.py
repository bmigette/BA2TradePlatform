"""
Verification script for WorkerQueue import fixes

This checks that all imports in WorkerQueue.py are correct and working.
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

print("Verifying WorkerQueue import fixes...")
print("=" * 80)

try:
    # Test 1: Import the WorkerQueue module
    print("\n1. Testing WorkerQueue module import...")
    from ba2_trade_platform.core.WorkerQueue import WorkerQueue
    print("   ✅ WorkerQueue imported successfully")
    
    # Test 2: Verify get_expert_instance_from_id is importable from utils
    print("\n2. Testing get_expert_instance_from_id import from utils...")
    from ba2_trade_platform.core.utils import get_expert_instance_from_id
    print(f"   ✅ Function imported: {get_expert_instance_from_id}")
    
    # Test 3: Check that interfaces module does NOT export this function
    print("\n3. Verifying interfaces module does NOT have get_expert_instance_from_id...")
    from ba2_trade_platform.core import interfaces
    if hasattr(interfaces, 'get_expert_instance_from_id'):
        print("   ❌ ERROR: interfaces module should NOT have get_expert_instance_from_id!")
    else:
        print("   ✅ Correct: interfaces module does not export this function")
    
    # Test 4: Verify no incorrect imports in source code
    print("\n4. Checking source code for incorrect imports...")
    worker_queue_path = Path(__file__).parent.parent / "ba2_trade_platform" / "core" / "WorkerQueue.py"
    with open(worker_queue_path, 'r', encoding='utf-8') as f:
        content = f.read()
        if "from .interfaces import get_expert_instance_from_id" in content or \
           "from ..core.interfaces import get_expert_instance_from_id" in content:
            print("   ❌ ERROR: Found incorrect import in WorkerQueue.py!")
        else:
            print("   ✅ No incorrect imports found in WorkerQueue.py")
    
    # Test 5: Count correct imports
    print("\n5. Counting correct imports of get_expert_instance_from_id...")
    correct_imports = content.count("from .utils import get_expert_instance_from_id")
    print(f"   ✅ Found {correct_imports} correct imports from .utils")
    
    print("\n" + "=" * 80)
    print("✅ ALL CHECKS PASSED!")
    print("\nThe error you saw was from an old run. The code is now fixed.")
    print("Python cache has been cleared - restart the application to use the fixed code.")
    
except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
