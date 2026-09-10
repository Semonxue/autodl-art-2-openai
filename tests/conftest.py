"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so ``import app.*`` works
# regardless of where pytest is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
