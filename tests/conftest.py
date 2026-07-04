"""Pytest configuration for desktop-clock tests.

clock.py is a single-file GTK app. The __main__ guard (added v1.3.6) lets
it be imported without starting the app. Tests that import clock need a
display (DISPLAY env var) — skip them if none is available.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

requires_display = pytest.mark.skipif(
    not os.environ.get('DISPLAY'),
    reason="requires a display (DISPLAY not set)"
)
