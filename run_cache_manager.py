#!/usr/bin/env python3
"""
Entry point script for Cache Manager

Run this script to start the modern PySide6-based cache manager.
"""

import sys
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

if __name__ == "__main__":
    from cache_manager.main import cli_main
    sys.exit(cli_main())