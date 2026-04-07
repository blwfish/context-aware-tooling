"""
Model splitting utilities for resin MSLA printing.

Splits FreeCAD shapes along arbitrary or axis-aligned planes using
box-based boolean intersection (which correctly creates cap faces on
compound shapes, unlike half-space intersection).

Usage (from FreeCAD MCP execute_python):
    from split import split_model, split_model_plane

All dimensions are in print-scale mm (not prototype scale).
"""

import Part
import FreeCAD
from FreeCAD import Vector
import math
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plane basis utility
# ---------------------------------------------------------------------------

def _plane_basis(normal):
    """
    Return two orthonormal vectors (u, v) perpendicular to normal.
    """
    n = Vector(normal)
    n.normalize()
    if abs(n.x) < 0.9:
        ref = Vector(1, 0, 0)
    else:
        ref = Vector(0, 1, 0)
    u = n.cross(ref)
    u.normalize()
    v = n.cross(u)
    v.normalize()
    return u, v


# ---------------------------------------------------------------------------
# Model Splitting
# ---------------------------------------------------------------------------

def split_model_plane(shape, point, normal):
    """
    Split a shape into two halves along an arbitrary plane.

    The plane is defined by a point on the plane and its normal vector.
    The "positive" half is on the side the normal points toward.

    Parameters
    ----------
    shape : Part.Shape
        The shape to split.
    point : Vector
        A point on the split plane.
    normal : Vector
        Normal vector of the split plane. The positive half is on the
        side this vector points toward.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (negative_side, positive_side)
    """
    bb = shape.BoundBox
    diag = math.sqrt(bb.XLength**2 + bb.YLength**2 + bb.ZLength**2)
    half_size = diag + 10.0  # oversized to ensure full coverage

    n = Vector(normal)
    n.normalize()
    u, v = _plane_basis(n)
    p = Vector(point)

    # Build two large boxes, one on each side of the split plane.
    # Using boolean cut (shape.common with a solid box) reliably creates
    # cap faces at the cut boundary -- unlike half-space common which
    # clips faces but doesn't cap compound shapes.
    #
    # Each box extends from the split plane outward by half_size.
    # Box thickness along the normal = half_size (one-sided).

    def _make_half_box(offset_dir):
        """Make a large box on one side of the split plane."""
        center = p + offset_dir * (half_size / 2)
        c1 = center - u * half_size - v * half_size - offset_dir * (half_size / 2)
        c2 = center + u * half_size - v * half_size - offset_dir * (half_size / 2)
        c3 = center + u * half_size + v * half_size - offset_dir * (half_size / 2)
        c4 = center - u * half_size + v * half_size - offset_dir * (half_size / 2)
        # Extrude along offset_dir
        wire = Part.makePolygon([c1, c2, c3, c4, c1])
        face = Part.Face(wire)
        return face.extrude(offset_dir * half_size)

    box_neg = _make_half_box(n * -1)
    box_pos = _make_half_box(n)

    neg_half = shape.common(box_neg)
    pos_half = shape.common(box_pos)

    print(f"Split at plane (point={point}, normal={n}): "
          f"neg solids={len(neg_half.Solids)}, "
          f"pos solids={len(pos_half.Solids)}")
    return neg_half, pos_half


def split_model(shape, axis, position):
    """
    Split a shape into two halves at a plane perpendicular to the given axis.

    Convenience wrapper around split_model_plane for axis-aligned splits.

    Parameters
    ----------
    shape : Part.Shape
        The shape to split.
    axis : str
        'x', 'y', or 'z' -- the axis perpendicular to the split plane.
    position : float
        Coordinate along that axis where the cut is made.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (negative_side, positive_side) -- the two halves.
        negative_side has coords < position on the given axis;
        positive_side has coords >= position.
    """
    axis_map = {
        'x': (Vector(position, 0, 0), Vector(1, 0, 0)),
        'y': (Vector(0, position, 0), Vector(0, 1, 0)),
        'z': (Vector(0, 0, position), Vector(0, 0, 1)),
    }
    if axis not in axis_map:
        raise ValueError(f"axis must be 'x', 'y', or 'z', got '{axis}'")

    point, normal = axis_map[axis]
    return split_model_plane(shape, point, normal)
