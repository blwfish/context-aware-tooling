"""
Model splitting and pin/socket registration utilities for resin MSLA printing.

Provides functions to split a FreeCAD shape along arbitrary or axis-aligned
planes, and add tapered pin/socket registration features for accurate
reassembly of multi-piece prints.

Usage (from FreeCAD MCP execute_python):
    from split_utils import split_and_register, split_and_register_plane

    # Axis-aligned split with registration:
    neg, pos = split_and_register(shape, axis='y', position=45.0)

    # Arbitrary plane split:
    neg, pos = split_and_register_plane(shape, point, normal)

All dimensions are in print-scale mm (not prototype scale).
"""

import Part
import FreeCAD
from FreeCAD import Vector
import math
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — pin/socket registration
# ---------------------------------------------------------------------------

PIN_RADIUS = 0.6               # mm -- pin outer radius at print scale
PIN_HEIGHT = 1.5               # mm -- pin length (extends from split face)
PIN_DRAFT_ANGLE = 2.0          # degrees -- slight taper for press-fit
PIN_CLEARANCE = 0.12           # mm radial clearance for socket
PIN_SPACING = 15.0             # mm default spacing along split edge
PIN_EDGE_MARGIN = 3.0          # mm inset from ends of split edge


# ---------------------------------------------------------------------------
# Model Splitting & Registration
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

    # Build a large planar face on the split plane using explicit corners.
    # This avoids Part.makePlane's internal UV convention issues.
    p = Vector(point)
    c1 = p - u * half_size - v * half_size
    c2 = p + u * half_size - v * half_size
    c3 = p + u * half_size + v * half_size
    c4 = p - u * half_size + v * half_size
    wire = Part.makePolygon([c1, c2, c3, c4, c1])
    plane_face = Part.Face(wire)

    # Create half-spaces using reference points on each side
    ref_pos = p + n * 10.0
    ref_neg = p - n * 10.0
    hs_pos = plane_face.makeHalfSpace(ref_pos)
    hs_neg = plane_face.makeHalfSpace(ref_neg)

    neg_half = shape.common(hs_neg)
    pos_half = shape.common(hs_pos)

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


def _pin_positions_along_edge(shape, axis, split_pos, pin_axis):
    """
    Compute pin center positions along a split face.

    Distributes pins at PIN_SPACING intervals along pin_axis, inset
    from the edges of the split face.

    Parameters
    ----------
    shape : Part.Shape
        One of the split halves (used to get bounding box at the split face).
    axis : str
        The split axis ('x', 'y', or 'z').
    split_pos : float
        Coordinate of the split plane.
    pin_axis : str
        Axis along which to distribute pins. Must differ from split axis.
        Typically the longest axis of the split face.

    Returns
    -------
    list of Vector
        Pin center positions on the split face.
    """
    bb = shape.BoundBox

    # The split face is perpendicular to `axis`. Pins distribute along
    # `pin_axis` and are centered on the remaining axis.
    remaining = [a for a in ['x', 'y', 'z'] if a != axis and a != pin_axis][0]

    def _range(ax):
        if ax == 'x': return bb.XMin, bb.XMax
        if ax == 'y': return bb.YMin, bb.YMax
        if ax == 'z': return bb.ZMin, bb.ZMax

    pa_min, pa_max = _range(pin_axis)
    ra_min, ra_max = _range(remaining)

    # Distribute along pin_axis
    span = pa_max - pa_min - 2 * PIN_EDGE_MARGIN
    if span <= 0:
        # Too short for pins
        return []
    count = max(2, int(span / PIN_SPACING) + 1)
    if count == 1:
        pa_positions = [(pa_min + pa_max) / 2.0]
    else:
        step = span / (count - 1)
        pa_positions = [pa_min + PIN_EDGE_MARGIN + i * step
                        for i in range(count)]

    # Center on remaining axis
    ra_center = (ra_min + ra_max) / 2.0

    positions = []
    for pa_val in pa_positions:
        coords = {axis: split_pos, pin_axis: pa_val, remaining: ra_center}
        positions.append(Vector(coords['x'], coords['y'], coords['z']))

    return positions


def make_pin(center, direction, radius=PIN_RADIUS, height=PIN_HEIGHT,
             draft_deg=PIN_DRAFT_ANGLE):
    """
    Make a single tapered registration pin (truncated cone).

    Parameters
    ----------
    center : Vector
        Center of pin base (on the split face).
    direction : Vector
        Unit vector pointing away from the body (into the mating piece).
    radius : float
        Base radius.
    height : float
        Pin length.
    draft_deg : float
        Taper angle in degrees. Tip radius = radius - height * tan(draft).

    Returns
    -------
    Part.Shape
    """
    tip_radius = max(0.1, radius - height * math.tan(math.radians(draft_deg)))
    pin = Part.makeCone(radius, tip_radius, height,
                        center, direction)
    return pin


def make_socket(center, direction, radius=PIN_RADIUS, height=PIN_HEIGHT,
                draft_deg=PIN_DRAFT_ANGLE, clearance=PIN_CLEARANCE):
    """
    Make a single registration socket (hole matching a pin, with clearance).

    The socket is a truncated cone slightly larger than the pin.

    Parameters
    ----------
    center : Vector
        Center of socket opening (on the split face).
    direction : Vector
        Unit vector pointing into the body (opposite of pin direction).
    radius : float
        Pin base radius (socket adds clearance).
    height : float
        Socket depth (slightly deeper than pin for bottoming clearance).
    draft_deg : float
        Taper angle matching pin.
    clearance : float
        Radial clearance added to socket.

    Returns
    -------
    Part.Shape
        Solid to be subtracted (boolean cut) from the body.
    """
    sock_radius = radius + clearance
    tip_radius = max(0.1, radius - height * math.tan(math.radians(draft_deg)))
    sock_tip = tip_radius + clearance
    sock_height = height + clearance  # slightly deeper for bottoming room
    socket = Part.makeCone(sock_radius, sock_tip, sock_height,
                           center, direction)
    return socket


def _find_split_face(shape, plane_point, plane_normal, tol=0.1):
    """
    Find the face on a split half that lies on the split plane.

    Looks for a planar face whose center is close to the split plane
    and whose normal is parallel to the plane normal.

    Returns the face, or None if not found.
    """
    n = Vector(plane_normal)
    n.normalize()
    for face in shape.Faces:
        if face.Surface.TypeId != 'Part::GeomPlane':
            continue
        try:
            fn = face.normalAt(0.5, 0.5)
        except Exception:
            continue
        # Check normal is parallel (or anti-parallel) to split plane normal
        dot = abs(fn.x * n.x + fn.y * n.y + fn.z * n.z)
        if dot < 0.95:
            continue
        # Check face center is on the split plane
        cog = face.CenterOfGravity
        dist = abs((cog.x - plane_point.x) * n.x +
                    (cog.y - plane_point.y) * n.y +
                    (cog.z - plane_point.z) * n.z)
        if dist < tol:
            return face
    return None


def _pin_positions_on_face(face, plane_normal, spacing=PIN_SPACING,
                            margin=PIN_EDGE_MARGIN):
    """
    Distribute pin positions across a split face.

    Works with arbitrary planar faces, not just axis-aligned ones.
    Uses the face's own in-plane basis vectors computed from the plane
    normal, so diagonal and angled splits work correctly.

    Distributes pins along the longer in-plane direction, centered
    on the shorter direction.

    Parameters
    ----------
    face : Part.Face
        The planar split face.
    plane_normal : Vector
        Normal of the split plane (pin direction).
    spacing : float
        Target spacing between pins.
    margin : float
        Inset from face edges.

    Returns
    -------
    list of Vector
        Pin center positions on the split face.
    """
    n = Vector(plane_normal)
    n.normalize()
    u, v = _plane_basis(n)

    # Get the face center of gravity as the origin for UV projection
    cog = face.CenterOfGravity

    # Project all face vertices onto the UV plane to find extent
    verts = face.Vertexes
    if len(verts) < 3:
        return []

    u_vals = []
    v_vals = []
    for vert in verts:
        p = vert.Point
        d = p - cog
        u_vals.append(d.dot(u))
        v_vals.append(d.dot(v))

    u_min, u_max = min(u_vals), max(u_vals)
    v_min, v_max = min(v_vals), max(v_vals)
    u_span = u_max - u_min
    v_span = v_max - v_min

    # Distribute along the longer direction, center on shorter
    if u_span >= v_span:
        long_dir, long_min, long_max = u, u_min, u_max
        short_dir, short_min, short_max = v, v_min, v_max
    else:
        long_dir, long_min, long_max = v, v_min, v_max
        short_dir, short_min, short_max = u, u_min, u_max

    long_span = long_max - long_min
    span = long_span - 2 * margin
    if span <= 0:
        return []

    count = max(2, int(span / spacing) + 1)
    step = span / (count - 1) if count > 1 else 0
    long_positions = [long_min + margin + i * step for i in range(count)]

    short_center = (short_min + short_max) / 2.0

    positions = []
    for lv in long_positions:
        pos = cog + long_dir * lv + short_dir * short_center
        positions.append(pos)

    return positions


def add_registration_plane(neg_half, pos_half, plane_point, plane_normal):
    """
    Add pin/socket registration features to two split halves.

    General-purpose version that works with any split plane orientation.
    Pins protrude from the negative half into sockets cut into the
    positive half.

    Parameters
    ----------
    neg_half : Part.Shape
        The side opposite to the plane normal.
    pos_half : Part.Shape
        The side the plane normal points toward.
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal vector of the split plane (points from neg toward pos).

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins, pos_with_sockets)
    """
    n = Vector(plane_normal)
    n.normalize()

    # Find the split face on the negative half to place pins on
    split_face = _find_split_face(neg_half, plane_point, n)
    if split_face is None:
        # Fallback: try finding it on pos_half
        split_face = _find_split_face(pos_half, plane_point, n)
    if split_face is None:
        logger.warning("Could not find split face for pin placement")
        return neg_half, pos_half

    positions = _pin_positions_on_face(split_face, n)
    if not positions:
        logger.warning("No room for registration pins on split face")
        return neg_half, pos_half

    pin_dir = n  # pins grow from neg toward pos

    pin_shapes = []
    socket_shapes = []
    for pos in positions:
        pin_shapes.append(make_pin(pos, pin_dir))
        socket_shapes.append(make_socket(pos, pin_dir))

    # Fuse pins onto negative half
    pin_compound = Part.Compound(pin_shapes)
    neg_result = neg_half.fuse(pin_compound)

    # Cut sockets from positive half
    sock_compound = Part.Compound(socket_shapes)
    pos_result = pos_half.cut(sock_compound)

    print(f"Added {len(positions)} registration pin/socket pairs")
    return neg_result, pos_result


def add_registration(neg_half, pos_half, axis, split_pos, pin_axis=None):
    """
    Add pin/socket registration features to two axis-aligned split halves.

    Convenience wrapper around add_registration_plane.

    Parameters
    ----------
    neg_half : Part.Shape
        The side with coords < split_pos.
    pos_half : Part.Shape
        The side with coords >= split_pos.
    axis : str
        Split axis ('x', 'y', or 'z').
    split_pos : float
        Coordinate of split plane.
    pin_axis : str or None
        Ignored (kept for backward compatibility). Pin distribution
        axis is auto-detected from the split face geometry.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins, pos_with_sockets)
    """
    axis_map = {
        'x': (Vector(split_pos, 0, 0), Vector(1, 0, 0)),
        'y': (Vector(0, split_pos, 0), Vector(0, 1, 0)),
        'z': (Vector(0, 0, split_pos), Vector(0, 0, 1)),
    }
    point, normal = axis_map[axis]
    return add_registration_plane(neg_half, pos_half, point, normal)


def split_and_register_plane(shape, point, normal):
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

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins, pos_with_sockets)
    """
    neg, pos = split_model_plane(shape, point, normal)
    return add_registration_plane(neg, pos, point, normal)


def split_and_register(shape, axis, position, pin_axis=None):
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

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_pins, pos_with_sockets)
    """
    neg, pos = split_model(shape, axis, position)
    return add_registration(neg, pos, axis, position)
