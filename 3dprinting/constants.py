"""
Shared constants for the 3D printing pipeline.

Pure Python — no FreeCAD dependency.  All modules import from here
to avoid duplicating printer specs, pipeline metadata, etc.
"""

# ---------------------------------------------------------------------------
# Printer build volumes (x, y, z) in mm
# ---------------------------------------------------------------------------

PRINTER_VOLUMES = {
    'm7_pro': (218.0, 123.0, 260.0),
    'm7_max': (298.0, 164.0, 300.0),
}

# ---------------------------------------------------------------------------
# Pipeline metadata (provenance tracking)
# ---------------------------------------------------------------------------

PIPELINE_NAME = "print_pipeline"
PIPELINE_VERSION = "1.0.0"
_METADATA_GROUP = "Metadata"
