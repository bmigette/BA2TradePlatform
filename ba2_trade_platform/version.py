"""
Application version derived from the git history.

Format: YYYY.MM.NNNNN
  YYYY  - current year
  MM    - current month (zero-padded)
  NNNNN - total git commit count on the current branch (auto-increments on every push)
"""

import os
import subprocess
from datetime import datetime

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_commit_count() -> int:
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            cwd=_REPO_DIR,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return 0


def get_version() -> str:
    now = datetime.now()
    count = _git_commit_count()
    return f"{now.year}.{now.month:02d}.{count}"


APP_VERSION = get_version()
