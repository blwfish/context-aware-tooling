"""
Model splitting, registration, and bracing utilities for resin MSLA printing.

This module is a backward-compatible re-export shim.  The implementation
has been split into focused modules:

- **split.py** -- model splitting (split_model, split_model_plane)
- **registration.py** -- pin/socket, tab/slot, blister registration
- **bracing.py** -- temporary sprue runner bracing

All public names are re-exported here so existing code that does
``from split_utils import ...`` continues to work unchanged.

Usage (from FreeCAD MCP execute_python):
    from split_utils import split_and_register, split_and_register_plane

    # Axis-aligned split with registration (auto pin count from spacing):
    neg, pos = split_and_register(shape, axis='y', position=45.0)

    # Specify exact number of pins:
    neg, pos = split_and_register(shape, axis='y', position=45.0, pin_count=3)

    # Arbitrary plane split:
    neg, pos = split_and_register_plane(shape, point, normal, pin_count=4)

    # Split + register + brace in one step:
    neg, pos = split_register_and_brace(shape, axis='y', position=45.0)

All dimensions are in print-scale mm (not prototype scale).
"""

from FreeCAD import Vector

# ---------------------------------------------------------------------------
# Re-exports from split.py
# ---------------------------------------------------------------------------
from split import (
    _plane_basis,
    split_model_plane,
    split_model,
)

# ---------------------------------------------------------------------------
# Re-exports from registration.py
# ---------------------------------------------------------------------------
from registration import (
    # Constants
    PIN_RADIUS,
    PIN_HEIGHT,
    PIN_DRAFT_ANGLE,
    PIN_CLEARANCE,
    PIN_SPACING,
    PIN_EDGE_MARGIN,
    TAB_WIDTH,
    TAB_DEPTH,
    TAB_HEIGHT,
    TAB_CLEARANCE,
    TAB_SPACING,
    TAB_EDGE_MARGIN,
    TAB_MIN_WALL,
    TAB_BASE,
    BLISTER_RADIUS,
    BLISTER_DEPTH,
    BLISTER_OVERLAP,
    BLISTER_SPACING,
    BLISTER_EDGE_MARGIN,
    # Internal helpers (used by tests)
    _find_split_face,
    _classify_split_face_edges,
    _pin_positions_on_face,
    _pin_positions_along_edge,
    _tab_positions_along_edge,
    _make_tab_box,
    _measure_wall_thickness,
    _blister_positions_along_edge,
    # Public functions
    make_pin,
    make_socket,
    make_tab,
    make_tab_slot,
    make_blister,
    add_registration_plane,
    add_registration,
    add_tab_registration_plane,
    add_blister_registration_plane,
    # Analysis
    CutFaceAnalysis,
    analyze_cut_face,
)

# ---------------------------------------------------------------------------
# Re-exports from bracing.py
# ---------------------------------------------------------------------------
from bracing import (
    # Constants
    BRACE_WIDTH,
    BRACE_DEPTH,
    BRACE_NECK_WIDTH,
    BRACE_NECK_LENGTH,
    BRACE_OFFSET,
    # Internal helpers
    _project_point_to_edge,
    _find_pins_near_edge,
    _interior_direction_at,
    _make_runner_segment,
    _make_neck_notch,
    _is_floor_edge,
    # Public functions
    add_bracing,
    add_bracing_both,
)


# ---------------------------------------------------------------------------
# Convenience wrappers (defined here since they compose split + registration
# + bracing and don't belong to any single submodule)
# ---------------------------------------------------------------------------

def split_and_register_plane(shape, point, normal, pin_count=None):
    """
    Split a shape along an arbitrary plane and add registration features.

    Parameters
    ----------
    shape : Part.Shape
        Model to split.
    point : Vector
        A point on the split plane.
    normal : Vector
        Normal vector of the split plane.
    pin_count : int or None
        Exact number of pins. When None, count is derived from spacing.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins, pos_with_sockets)
    """
    neg, pos = split_model_plane(shape, point, normal)
    return add_registration_plane(neg, pos, point, normal,
                                  pin_count=pin_count)


def split_and_register(shape, axis, position, pin_axis=None, pin_count=None):
    """
    Split a shape along an axis-aligned plane and add registration features.

    Convenience wrapper for axis-aligned splits.

    Parameters
    ----------
    shape : Part.Shape
        Model to split.
    axis : str
        Split axis ('x', 'y', or 'z').
    position : float
        Split coordinate.
    pin_axis : str or None
        Ignored (kept for backward compatibility).
    pin_count : int or None
        Exact number of pins. When None, count is derived from spacing.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins, pos_with_sockets)
    """
    neg, pos = split_model(shape, axis, position)
    return add_registration(neg, pos, axis, position, pin_count=pin_count)


def split_register_and_brace_plane(shape, point, normal, pin_count=None):
    """
    Split, register, and brace in one step.

    Splits the shape, adds pin/socket registration, then adds temporary
    sprue bracing connecting pin bases along interior walls.

    Parameters
    ----------
    shape : Part.Shape
        Model to split.
    point : Vector
        A point on the split plane.
    normal : Vector
        Normal vector of the split plane.
    pin_count : int or None
        Exact number of pins.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins_and_braces, pos_with_sockets_and_braces)
    """
    neg, pos = split_model_plane(shape, point, normal)
    neg, pos, pin_positions = add_registration_plane(
        neg, pos, point, normal, pin_count=pin_count, return_positions=True)
    neg, pos = add_bracing_both(neg, pos, point, normal, pin_positions)
    return neg, pos


def split_register_and_brace(shape, axis, position, pin_count=None):
    """
    Split, register, and brace along an axis-aligned plane.

    Convenience wrapper for axis-aligned splits with bracing.

    Parameters
    ----------
    shape : Part.Shape
        Model to split.
    axis : str
        Split axis ('x', 'y', or 'z').
    position : float
        Split coordinate.
    pin_count : int or None
        Exact number of pins.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins_and_braces, pos_with_sockets_and_braces)
    """
    axis_map = {
        'x': (Vector(position, 0, 0), Vector(1, 0, 0)),
        'y': (Vector(0, position, 0), Vector(0, 1, 0)),
        'z': (Vector(0, 0, position), Vector(0, 0, 1)),
    }
    if axis not in axis_map:
        raise ValueError(f"axis must be 'x', 'y', or 'z', got '{axis}'")
    point, normal = axis_map[axis]
    return split_register_and_brace_plane(shape, point, normal,
                                           pin_count=pin_count)
