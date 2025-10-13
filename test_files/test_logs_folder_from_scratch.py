#!/usr/bin/env python3
"""
Comprehensive test to verify logs folder creation from scratch.
This test temporarily renames the logs folder and then tests if it gets recreated.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import tempfile
import shutil

def test_logs_folder_creation_from_scratch():
    """Test that logs folder is created when it doesn't exist."""
    
    print("=== Testing Logs Folder Creation From Scratch ===\n")
    
    try:
        # Import config to get the LOG_FOLDER path
        from ba2_trade_platform.config import LOG_FOLDER
        
        print(f"Target logs folder: {LOG_FOLDER}")
        
        # Create a backup of the logs folder if it exists
        backup_folder = None
        if os.path.exists(LOG_FOLDER):
            backup_folder = LOG_FOLDER + "_backup_test"
            if os.path.exists(backup_folder):
                shutil.rmtree(backup_folder)
            shutil.move(LOG_FOLDER, backup_folder)
            print(f"✅ Moved existing logs folder to: {backup_folder}")
        
        # Verify logs folder doesn't exist
        if not os.path.exists(LOG_FOLDER):
            print(f"✅ Confirmed logs folder doesn't exist: {LOG_FOLDER}")
        else:
            print(f"❌ Logs folder still exists, test invalid")
            return
        
        # Now import the logger - this should create the logs folder
        print("\nImporting logger module (should create logs folder)...")
        from ba2_trade_platform.logger import logger
        
        # Check if folder was created
        if os.path.exists(LOG_FOLDER):
            print(f"✅ Logs folder was created by logger import: {LOG_FOLDER}")
        else:
            print(f"❌ Logs folder was NOT created by logger import")
        
        # Test logging functionality
        print("\nTesting logging functionality...")
        logger.info("Test message - logs folder creation from scratch")
        logger.debug("Debug test message - logs folder creation from scratch")
        logger.warning("Warning test message - logs folder creation from scratch")
        
        # Give a moment for file writes
        import time
        time.sleep(0.5)
        
        # Check for log files
        if os.path.exists(LOG_FOLDER):
            files = os.listdir(LOG_FOLDER)
            log_files = [f for f in files if f.endswith('.log')]
            
            if log_files:
                print(f"   ✅ Log files created: {log_files}")
                
                # Verify content in one of the log files
                for log_file in log_files:
                    if 'app' in log_file:
                        log_path = os.path.join(LOG_FOLDER, log_file)
                        try:
                            with open(log_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                                if "logs folder creation from scratch" in content:
                                    print(f"   ✅ Test messages found in {log_file}")
                                    break
                        except Exception as e:
                            print(f"   ⚠️  Could not read {log_file}: {e}")
            else:
                print("   ❌ No log files created")
        
        print("\n✅ Logs folder creation from scratch test completed!")
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # Restore the original logs folder if we backed it up
        if backup_folder and os.path.exists(backup_folder):
            try:
                if os.path.exists(LOG_FOLDER):
                    # Merge the new logs with the backup
                    if os.path.exists(backup_folder):
                        # Copy new log files to backup folder
                        new_files = os.listdir(LOG_FOLDER)
                        for file in new_files:
                            src = os.path.join(LOG_FOLDER, file)
                            dst = os.path.join(backup_folder, file)
                            if not os.path.exists(dst):  # Don't overwrite existing files
                                shutil.copy2(src, dst)
                        
                        # Remove new logs folder and restore backup
                        shutil.rmtree(LOG_FOLDER)
                    
                shutil.move(backup_folder, LOG_FOLDER)
                print(f"\n✅ Restored original logs folder from: {backup_folder}")
            except Exception as e:
                print(f"\n⚠️  Could not restore backup logs folder: {e}")

if __name__ == "__main__":
    test_logs_folder_creation_from_scratch()