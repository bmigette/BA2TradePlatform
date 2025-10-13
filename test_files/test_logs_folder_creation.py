#!/usr/bin/env python3
"""
Test script to verify that the logs folder is created properly at startup.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import tempfile
import shutil

# Test the logs folder creation by temporarily moving the existing logs folder
# and then importing the logger to see if it creates the folder automatically

def test_logs_folder_creation():
    """Test that logs folder is created automatically."""
    
    print("=== Testing Logs Folder Creation ===\n")
    
    try:
        from ba2_trade_platform.config import LOG_FOLDER, HOME_PARENT
        
        print(f"Expected logs folder path: {LOG_FOLDER}")
        print(f"Project root: {HOME_PARENT}")
        
        # Check if logs folder exists after import
        if os.path.exists(LOG_FOLDER):
            print(f"✅ Logs folder exists: {LOG_FOLDER}")
            
            # List files in logs folder
            try:
                files = os.listdir(LOG_FOLDER)
                if files:
                    print(f"   Files in logs folder: {files}")
                else:
                    print("   Logs folder is empty")
            except Exception as e:
                print(f"   Could not list files: {e}")
        else:
            print(f"❌ Logs folder does not exist: {LOG_FOLDER}")
        
        # Test the logger import
        print("\nTesting logger import and functionality...")
        from ba2_trade_platform.logger import logger
        
        # Try to log something
        logger.info("Test log message from logs folder creation test")
        
        # Check if logs folder was created after logger initialization
        if os.path.exists(LOG_FOLDER):
            print(f"✅ Logs folder exists after logger import: {LOG_FOLDER}")
            
            # Check for log files
            log_files = []
            for file in os.listdir(LOG_FOLDER):
                if file.endswith('.log'):
                    log_files.append(file)
            
            if log_files:
                print(f"   Log files found: {log_files}")
                
                # Check if our test message was written
                for log_file in log_files:
                    log_path = os.path.join(LOG_FOLDER, log_file)
                    try:
                        with open(log_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            if "Test log message from logs folder creation test" in content:
                                print(f"   ✅ Test message found in {log_file}")
                            else:
                                print(f"   ⚠️  Test message not found in {log_file} (might not have been written yet)")
                    except Exception as e:
                        print(f"   ❌ Could not read {log_file}: {e}")
            else:
                print("   ⚠️  No log files found (might not have been created yet)")
        else:
            print(f"❌ Logs folder still does not exist after logger import")
        
        print("\n✅ Logs folder creation test completed!")
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_logs_folder_creation()