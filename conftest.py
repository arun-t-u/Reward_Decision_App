"""
Root conftest.py — ensures the project root is on sys.path so that
`import app.*` works without needing `pip install -e .`
"""
import sys
from pathlib import Path

# Add project root to PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))