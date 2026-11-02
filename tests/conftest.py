"""Gemeinsame pytest-Einstellungen: Projektordner in sys.path (auch fuer pytest < 7)."""
import sys
from pathlib import Path

PROJECT_DIR = str(Path(__file__).resolve().parents[1])
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
