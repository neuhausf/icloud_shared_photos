"""Load the integration's HA-independent modules without importing Home Assistant."""

from __future__ import annotations

from pathlib import Path
import sys
import types

PKG_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "icloud_shared_photos"
)

# Register a stub package so relative imports (``from .const import ...``)
# work without executing the integration's __init__.py (which needs HA).
_pkg = types.ModuleType("isp")
_pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("isp", _pkg)
