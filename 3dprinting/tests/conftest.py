"""Test configuration for 3dprinting tests.

Two tiers:
  - Pure-math tests: run anywhere (no FreeCAD needed)
  - FreeCAD tests: marked with @freecad, skipped when FreeCAD unavailable

To run FreeCAD tests, either:
  - Run pytest inside FreeCAD's Python environment
  - Add FreeCAD's lib path to PYTHONPATH
"""

import pytest

try:
    import FreeCAD
    HAS_FREECAD = True
except ImportError:
    HAS_FREECAD = False

freecad = pytest.mark.skipif(
    not HAS_FREECAD, reason="FreeCAD not available")


@pytest.fixture
def freecad_doc():
    """Create and yield a temporary FreeCAD document, cleaned up after test."""
    import FreeCAD
    doc = FreeCAD.newDocument("TestDoc")
    yield doc
    FreeCAD.closeDocument("TestDoc")
