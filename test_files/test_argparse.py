"""Test script to verify command-line argument parsing works correctly."""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock the arguments
sys.argv = [
    'main.py',
    '--db-file', '/custom/path/test.db',
    '--cache-folder', '/custom/cache',
    '--log-folder', '/custom/logs',
    '--port', '9090'
]

# Import and test argument parsing
from main import parse_arguments

args = parse_arguments()

print("Parsed arguments:")
print(f"  DB File: {args.db_file}")
print(f"  Cache Folder: {args.cache_folder}")
print(f"  Log Folder: {args.log_folder}")
print(f"  Port: {args.port}")

# Verify values
assert args.db_file == '/custom/path/test.db', "DB file mismatch"
assert args.cache_folder == '/custom/cache', "Cache folder mismatch"
assert args.log_folder == '/custom/logs', "Log folder mismatch"
assert args.port == 9090, "Port mismatch"

print("\n✅ All argument parsing tests passed!")

# Test with default values
sys.argv = ['main.py']
args_default = parse_arguments()

print("\nDefault values:")
print(f"  DB File: {args_default.db_file}")
print(f"  Cache Folder: {args_default.cache_folder}")
print(f"  Log Folder: {args_default.log_folder}")
print(f"  Port: {args_default.port}")

print("\n✅ Default values test passed!")
