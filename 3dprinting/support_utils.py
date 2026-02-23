"""
Context-aware support generation and model preparation utilities for resin
MSLA printing.

Designed to be executed inside FreeCAD's Python environment via MCP.
Implements the pipeline described in context-aware-supports.md.

Usage (from FreeCAD MCP execute_python):
    exec(open('/Volumes/Files/claude/tooling/3dprinting/support_utils.py').read())

    # Then call pipeline functions:
    # Tilt wall for printing (interior toward plate):
    tilted = tilt_for_printing(shape, tilt_deg=18.0, display_faces_negative_y=True)
    wall_normal = tilted_wall_outward_normal(18.0, display_faces_negative_y=True)

    # Classify faces:
    classified = classify_faces(tilted, wall_normal)

    # Generate supports (interior side only):
    contacts = generate_all_overhang_supports(tilted, classified, wall_normal, 'min')

    # Validate, build, raft:
    validate_tilt_direction(contacts, tilted, wall_normal)
    supports = build_supports(contacts)
    raft = build_raft(tilted, contact_points=contacts)

    # Or use the full pipeline:
    run_support_pipeline(doc, "MyWall", wall_normal, interior_y_side='min')

    # Split models:
    pieces = split_model(shape, axis='y', position=45.0)
    ...

All dimensions are in print-scale mm (not prototype scale).
"""

import Part
import FreeCAD
from FreeCAD import Vector
import math


# ---------------------------------------------------------------------------
# Constants (from context-aware-supports.md)
# ---------------------------------------------------------------------------

FRAGILE_THRESHOLD = 0.6        # mm -- features thinner than this are fragile
COSMETIC_AREA_MAX = 1.0        # mm^2 -- overhang faces smaller than this are cosmetic
COSMETIC_DEPTH_MAX = 1.0       # mm -- overhang depth shallower than this is cosmetic
OVERHANG_DOT_THRESHOLD = -0.3  # normal.z below this = downward-facing overhang
WALL_DOT_THRESHOLD = 0.7       # abs(dot with wall normal) above this = wall surface
                                # 0.5 was too loose: brick step overhangs had dot ~0.59

RAFT_MARGIN = 2.0              # mm beyond footprint
RAFT_THICKNESS = 1.5           # mm
RAFT_CHAMFER = 0.4             # mm

MODEL_RAISE = 3.0              # mm to raise model off raft

TIP_RADIUS = 0.15              # mm (contact point)
TIP_HEIGHT = 1.0               # mm (cone section)
COLUMN_RADIUS = 0.4            # mm
BASE_PAD_RADIUS = 1.0          # mm
BASE_PAD_HEIGHT = 0.5          # mm

BOTTOM_SUPPORT_SPACING = 5.0   # mm along longest axis
BOTTOM_SUPPORT_MIN_DEPTH = 3.0 # mm -- use front+back rows if depth exceeds this

# Pin/socket registration
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
    axes = {'x': 0, 'y': 1, 'z': 2}
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
        print("Warning: could not find split face for pin placement")
        return neg_half, pos_half

    positions = _pin_positions_on_face(split_face, n)
    if not positions:
        print("Warning: no room for registration pins on split face")
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


# ---------------------------------------------------------------------------
# Build Volume Check
# ---------------------------------------------------------------------------

# Known printer build volumes (x, y, z) in mm
PRINTER_VOLUMES = {
    'm7_pro': (218.0, 123.0, 260.0),
    'm7_max': (298.0, 164.0, 300.0),
}


def check_build_fit(shape, printer='m7_pro', margin=2.0):
    """
    Check if a shape (with supports/raft) fits a printer's build volume.

    Parameters
    ----------
    shape : Part.Shape
        The complete print (model + supports + raft).
    printer : str
        Printer key from PRINTER_VOLUMES.
    margin : float
        Safety margin from build volume edges.

    Returns
    -------
    dict with keys:
        'fits': bool
        'model_size': (x, y, z)
        'build_volume': (x, y, z)
        'overflow': (dx, dy, dz) -- positive values mean doesn't fit
    """
    vol = PRINTER_VOLUMES.get(printer)
    if vol is None:
        raise ValueError(f"Unknown printer '{printer}'. "
                         f"Known: {list(PRINTER_VOLUMES.keys())}")

    bb = shape.BoundBox
    model_size = (bb.XLength, bb.YLength, bb.ZLength)
    available = (vol[0] - 2*margin, vol[1] - 2*margin, vol[2] - 2*margin)
    overflow = (model_size[0] - available[0],
                model_size[1] - available[1],
                model_size[2] - available[2])
    fits = all(o <= 0 for o in overflow)

    status = "FITS" if fits else "DOES NOT FIT"
    print(f"Build volume check ({printer}): {status}")
    print(f"  Model:  {model_size[0]:.1f} x {model_size[1]:.1f} x "
          f"{model_size[2]:.1f} mm")
    print(f"  Volume: {vol[0]:.1f} x {vol[1]:.1f} x {vol[2]:.1f} mm")
    if not fits:
        axes = ['X', 'Y', 'Z']
        for i, o in enumerate(overflow):
            if o > 0:
                print(f"  {axes[i]} overflow: {o:.1f}mm")

    return {
        'fits': fits,
        'model_size': model_size,
        'build_volume': vol,
        'overflow': overflow,
    }


# ---------------------------------------------------------------------------
# Face Classification
# ---------------------------------------------------------------------------

def classify_faces(shape, wall_outward_normal, window_bounds=None,
                   tilt_angle_deg=0):
    """
    Classify every face in a shape for support decisions.

    Parameters
    ----------
    shape : Part.Shape
        The model shape (already oriented/tilted for printing).
    wall_outward_normal : Vector
        Unit vector pointing from interior toward display surface
        (in the *original* untilted frame). Used to distinguish
        display vs interior faces.
    window_bounds : dict or None
        If provided, keys: x_min, x_max, z_min, z_max (in original frame).
        Faces fully inside these bounds and thinner than FRAGILE_THRESHOLD
        are classified as mullion/fragile.
    tilt_angle_deg : float
        Tilt angle applied to the model (for reference; classification
        operates on the already-tilted geometry).

    Returns
    -------
    dict
        Keys are face indices (int), values are dicts with:
        - 'category': str (display, interior, vertical, brick_side,
          cosmetic_overhang, structural_overhang, fragile)
        - 'normal': Vector
        - 'area': float
        - 'bbox': BoundBox
    """
    results = {}

    for i, face in enumerate(shape.Faces):
        try:
            n = face.normalAt(0.5, 0.5)
        except Exception:
            n = Vector(0, 0, 0)
        bb = face.BoundBox
        area = face.Area
        dims = sorted([bb.XLength, bb.YLength, bb.ZLength])

        cat = _classify_single_face(n, bb, area, dims, wall_outward_normal,
                                     window_bounds)
        results[i] = {
            'category': cat,
            'normal': n,
            'area': area,
            'bbox': bb,
        }

    return results


def _classify_single_face(normal, bbox, area, dims_sorted,
                           wall_outward_normal, window_bounds):
    """Classify a single face. Returns category string."""

    nz = normal.z

    # --- Wall-aligned faces first (display/interior) ---
    # Check alignment with wall outward normal BEFORE overhang detection.
    # A tilted wall's interior surface can have nz = -0.3 which would
    # otherwise trip the overhang threshold, but dot with wall_outward_normal
    # reveals it's clearly a wall surface, not an overhang.
    dot_wall = (normal.x * wall_outward_normal.x +
                normal.y * wall_outward_normal.y +
                normal.z * wall_outward_normal.z)

    if abs(dot_wall) > WALL_DOT_THRESHOLD:
        # Strongly aligned with wall normal -- this is a wall surface.
        # Check for brick_side (thin face) first.
        if dims_sorted[0] < FRAGILE_THRESHOLD:
            if window_bounds and _is_inside_window(bbox, window_bounds):
                return 'fragile'
            else:
                return 'brick_side'
        if dot_wall > 0.5:
            return 'display'
        else:
            return 'interior'

    # --- Overhang detection (downward-facing) ---
    if nz < OVERHANG_DOT_THRESHOLD:
        # Is it inside a window opening? Could be mullion.
        if window_bounds and _is_inside_window(bbox, window_bounds):
            if dims_sorted[0] < FRAGILE_THRESHOLD:
                return 'fragile'

        # Cosmetic vs structural overhang -- check BOTH area and depth.
        # Overhang depth = second-smallest bbox dimension (the projection
        # depth of the overhang). Brick course steps are ~0.4mm deep but
        # can have area > 1mm² on wide bays; the depth test catches these.
        overhang_depth = dims_sorted[1] if len(dims_sorted) > 1 else dims_sorted[0]
        if area < COSMETIC_AREA_MAX or overhang_depth < COSMETIC_DEPTH_MAX:
            return 'cosmetic_overhang'
        else:
            return 'structural_overhang'

    # --- Vertical / near-vertical faces ---
    if abs(nz) < 0.3:
        if dims_sorted[0] < FRAGILE_THRESHOLD:
            if window_bounds and _is_inside_window(bbox, window_bounds):
                return 'fragile'
            else:
                return 'brick_side'
        return 'vertical'

    # --- Upward-facing ---
    return 'vertical'  # top surfaces, etc.


def _is_inside_window(bbox, wb):
    """Check if a face bbox is fully inside window opening bounds."""
    return (bbox.XMin >= wb['x_min'] and bbox.XMax <= wb['x_max'] and
            bbox.ZMin >= wb['z_min'] and bbox.ZMax <= wb['z_max'])


def summarize_classification(classified):
    """Print a summary of face classification results."""
    counts = {}
    for info in classified.values():
        cat = info['category']
        counts[cat] = counts.get(cat, 0) + 1

    print(f"Face classification ({len(classified)} faces):")
    for cat in sorted(counts.keys()):
        print(f"  {cat}: {counts[cat]}")
    return counts


# ---------------------------------------------------------------------------
# Support Point Generation
# ---------------------------------------------------------------------------

def generate_lintel_supports(shape, classified, window_bounds,
                              mullion_x=None):
    """
    Generate support contact points for structural overhangs (lintels).

    Finds structural_overhang faces and places contacts at structural
    junctions (jamb corners, near mullion cross-points).

    Parameters
    ----------
    shape : Part.Shape
    classified : dict from classify_faces()
    window_bounds : dict with x_min, x_max, z_min, z_max
    mullion_x : float or None
        X position of vertical mullion center (if present).

    Returns
    -------
    list of (x, y, z) tuples -- contact points on the lintel underside.
    """
    # Find the lintel face(s) -- structural overhangs near window top
    lintel_faces = []
    for idx, info in classified.items():
        if info['category'] != 'structural_overhang':
            continue
        bb = info['bbox']
        # Must overlap with window X range and be near window top Z
        if (bb.XMin >= window_bounds['x_min'] - 1 and
            bb.XMax <= window_bounds['x_max'] + 1 and
            bb.ZMin > window_bounds['z_min']):
            lintel_faces.append((idx, info))

    if not lintel_faces:
        print("No lintel faces found")
        return []

    # Use the largest lintel face
    lintel_faces.sort(key=lambda x: x[1]['area'], reverse=True)
    lintel_idx, lintel_info = lintel_faces[0]
    lintel_face = shape.Faces[lintel_idx]
    bb = lintel_info['bbox']

    print(f"Lintel face {lintel_idx}: area={lintel_info['area']:.1f} "
          f"X=[{bb.XMin:.1f},{bb.XMax:.1f}] "
          f"Y=[{bb.YMin:.1f},{bb.YMax:.1f}] "
          f"Z=[{bb.ZMin:.1f},{bb.ZMax:.1f}]")

    # Build a Z-from-Y interpolation for the tilted lintel
    # (assumes planar face, linear Z variation across Y)
    def lintel_z(y):
        if bb.YMax == bb.YMin:
            return bb.ZMax
        t = (y - bb.YMin) / (bb.YMax - bb.YMin)
        return bb.ZMax - t * (bb.ZMax - bb.ZMin)

    # X positions: jamb edges + near mullion
    x_positions = [bb.XMin + 0.5, bb.XMax - 0.5]  # jamb corners
    if mullion_x is not None:
        # Flank the mullion (avoid the mullion hole itself)
        x_positions.extend([mullion_x - 1.5, mullion_x + 1.5])
    x_positions.sort()

    # Y positions: front and back rows
    y_margin = 0.5
    y_front = bb.YMin + y_margin
    y_back = bb.YMax - y_margin
    y_positions = [y_front, y_back]

    contacts = []
    for x in x_positions:
        for y in y_positions:
            z = lintel_z(y)
            contacts.append((x, y, z))

    print(f"Generated {len(contacts)} lintel support points")
    return contacts


def generate_bottom_supports(shape, classified, raise_amount=MODEL_RAISE,
                             interior_y_side=None):
    """
    Generate support contact points for the model's bottom face.

    Called after the model has been raised off the raft.

    Parameters
    ----------
    shape : Part.Shape
        The raised model shape.
    classified : dict from classify_faces()
    raise_amount : float
        How far the model was raised.
    interior_y_side : str or None
        'min' or 'max' -- which Y side of overhang faces is interior.
        Determined by tilt direction. If None, uses both rows.

    Returns
    -------
    list of (x, y, z) tuples -- contact points on the bottom face.
    """
    # Find the bottom face -- the downward-facing face whose ZMin is closest
    # to raise_amount (i.e., the lowest overhang, which is the model's base).
    # We filter to structural_overhang category and pick by lowest ZMin,
    # with a minimum area to avoid brick course fragments.
    bottom_candidates = []
    for idx, info in classified.items():
        if info['category'] != 'structural_overhang':
            continue
        bb = info['bbox']
        if bb.ZMin < raise_amount + 2.0 and info['area'] > 10.0:
            bottom_candidates.append((idx, info))

    if not bottom_candidates:
        print("No bottom face found")
        return []

    # Pick the face with the lowest ZMin (closest to the raft)
    bottom_candidates.sort(key=lambda x: x[1]['bbox'].ZMin)
    bottom_idx, bottom_info = bottom_candidates[0]
    bb = bottom_info['bbox']

    print(f"Bottom face {bottom_idx}: area={bottom_info['area']:.1f} "
          f"X=[{bb.XMin:.1f},{bb.XMax:.1f}] "
          f"Y=[{bb.YMin:.1f},{bb.YMax:.1f}] "
          f"Z=[{bb.ZMin:.1f},{bb.ZMax:.1f}]")

    # Z interpolation for tilted bottom
    def bottom_z(y):
        if bb.YMax == bb.YMin:
            return bb.ZMax
        t = (y - bb.YMin) / (bb.YMax - bb.YMin)
        return bb.ZMax - t * (bb.ZMax - bb.ZMin)

    # X positions: distribute at ~BOTTOM_SUPPORT_SPACING intervals
    x_count = max(2, int(bb.XLength / BOTTOM_SUPPORT_SPACING) + 1)
    x_margin = 1.5
    x_positions = []
    for i in range(x_count):
        t = (i + 0.5) / x_count
        x_positions.append(bb.XMin + x_margin + t * (bb.XLength - 2 * x_margin))

    # Y positions -- INTERIOR SIDE ONLY
    # The interior side depends on tilt direction. After correct tilt
    # (interior toward plate), the interior is at the lower or higher Y
    # of the overhang face. We default to the side further from the
    # display surface.
    y_margin = 0.5
    if interior_y_side == 'max':
        y_positions = [bb.YMax - y_margin]
    elif interior_y_side == 'min':
        y_positions = [bb.YMin + y_margin]
    else:
        # Fallback: use both rows if depth allows
        if bb.YLength > BOTTOM_SUPPORT_MIN_DEPTH:
            y_positions = [bb.YMin + y_margin, bb.YMax - y_margin]
        else:
            y_positions = [(bb.YMin + bb.YMax) / 2.0]

    contacts = []
    for x in x_positions:
        for y in y_positions:
            z = bottom_z(y)
            contacts.append((x, y, z))

    print(f"Generated {len(contacts)} bottom support points")
    return contacts


def generate_all_overhang_supports(shape, classified, wall_outward_normal,
                                   interior_y_side='min'):
    """
    Generate support contact points for all structural overhangs.

    Handles multi-bay walls: finds all structural overhang faces, separates
    them into bottom faces (grid supports) and lintels/features (jamb-corner
    supports), and places contacts on the interior side only.

    Parameters
    ----------
    shape : Part.Shape
        The tilted, raised model.
    classified : dict from classify_faces()
    wall_outward_normal : Vector
        Points from interior toward display (in tilted frame).
    interior_y_side : str
        'min' or 'max' -- which Y side of overhang faces is interior.
        After correct tilt (interior toward plate), this is 'min' if
        display was originally at -Y, 'max' if display was at +Y.

    Returns
    -------
    list of (x, y, z) tuples -- contact points.
    """
    contacts = []
    y_margin = 0.5
    x_margin = 1.5

    # Collect structural overhangs
    overhangs = []
    for idx, info in classified.items():
        if info['category'] != 'structural_overhang':
            continue
        overhangs.append((idx, info))

    if not overhangs:
        print("No structural overhangs found")
        return contacts

    # Separate by area: large faces are bottom/floor (grid supports),
    # smaller faces are lintels (jamb-corner supports)
    areas = [info['area'] for _, info in overhangs]
    area_threshold = max(areas) * 0.5  # rough split

    for idx, info in overhangs:
        bb = info['bbox']

        # Interior Y position
        if interior_y_side == 'min':
            y_int = bb.YMin + y_margin
        else:
            y_int = bb.YMax - y_margin

        # Z interpolation across the face (for tilted geometry)
        def z_at_y(y, b=bb):
            if b.YMax == b.YMin:
                return b.ZMin
            t = (y - b.YMin) / (b.YMax - b.YMin)
            return b.ZMin + t * (b.ZMax - b.ZMin)

        if info['area'] >= area_threshold:
            # Large face (bottom/floor) -- grid of supports
            x_count = max(2, int(bb.XLength / BOTTOM_SUPPORT_SPACING) + 1)
            for i in range(x_count):
                t = (i + 0.5) / x_count
                x = bb.XMin + x_margin + t * (bb.XLength - 2 * x_margin)
                z = z_at_y(y_int)
                contacts.append((x, y_int, z))
        else:
            # Lintel/feature -- supports at jamb corners only
            x_left = bb.XMin + 0.5
            x_right = bb.XMax - 0.5
            for x in [x_left, x_right]:
                z = z_at_y(y_int)
                contacts.append((x, y_int, z))

    print(f"Generated {len(contacts)} overhang support points "
          f"(all on {'YMin' if interior_y_side == 'min' else 'YMax'} / interior side)")
    return contacts


# ---------------------------------------------------------------------------
# Geometry Builders
# ---------------------------------------------------------------------------

def build_tapered_support(cx, cy, cz, raft_top_z=0.0):
    """
    Build a single tapered support column.

    Returns a list of Part.Shape objects (pad, column, cone).
    Caller is responsible for combining into a compound.

    Parameters
    ----------
    cx, cy, cz : float
        Contact point (tip of support touches model here).
    raft_top_z : float
        Z coordinate of raft top surface.

    Returns
    -------
    list of Part.Shape
    """
    shapes = []

    # Base pad on raft
    pad = Part.makeCylinder(BASE_PAD_RADIUS, BASE_PAD_HEIGHT,
                            Vector(cx, cy, raft_top_z), Vector(0, 0, 1))
    shapes.append(pad)

    # Column
    col_bot = raft_top_z + BASE_PAD_HEIGHT
    col_top = cz - TIP_HEIGHT
    if col_top > col_bot:
        col = Part.makeCylinder(COLUMN_RADIUS, col_top - col_bot,
                                Vector(cx, cy, col_bot), Vector(0, 0, 1))
        shapes.append(col)
    else:
        col_top = col_bot  # degenerate case: no column section

    # Cone tip
    cone = Part.makeCone(COLUMN_RADIUS, TIP_RADIUS, TIP_HEIGHT,
                         Vector(cx, cy, col_top), Vector(0, 0, 1))
    shapes.append(cone)

    return shapes


def build_supports(contact_points, raft_top_z=0.0):
    """
    Build all supports as a single compound.

    Parameters
    ----------
    contact_points : list of (x, y, z) tuples
    raft_top_z : float

    Returns
    -------
    Part.Compound
    """
    all_shapes = []
    for (cx, cy, cz) in contact_points:
        all_shapes.extend(build_tapered_support(cx, cy, cz, raft_top_z))

    compound = Part.Compound(all_shapes)
    print(f"Built {len(contact_points)} supports ({len(all_shapes)} shapes)")
    return compound


def build_raft(shape, contact_points=None, margin=RAFT_MARGIN,
               thickness=RAFT_THICKNESS, chamfer=RAFT_CHAMFER):
    """
    Build a raft under the model with chamfered bottom edges.

    The raft is sized to cover the model footprint AND all support
    base pad positions (whichever extent is larger).

    Parameters
    ----------
    shape : Part.Shape
        The model shape (used to compute footprint).
    contact_points : list of (x, y, z) or None
        Support contact points. Raft extends to cover all base pads.
    margin : float
        Extension beyond footprint.
    thickness : float
        Raft thickness.
    chamfer : float
        Chamfer size on bottom edges.

    Returns
    -------
    Part.Shape
    """
    bb = shape.BoundBox

    x0 = bb.XMin
    x1 = bb.XMax
    y0 = bb.YMin
    y1 = bb.YMax

    # Expand to cover all support base pad positions
    if contact_points:
        for (cx, cy, cz) in contact_points:
            x0 = min(x0, cx - BASE_PAD_RADIUS)
            x1 = max(x1, cx + BASE_PAD_RADIUS)
            y0 = min(y0, cy - BASE_PAD_RADIUS)
            y1 = max(y1, cy + BASE_PAD_RADIUS)

    # Apply margin
    x0 -= margin
    x1 += margin
    y0 -= margin
    y1 += margin

    raft = Part.makeBox(x1 - x0, y1 - y0, thickness,
                        Vector(x0, y0, -thickness))

    # Chamfer bottom edges
    if chamfer > 0:
        bottom_edges = [e for e in raft.Edges
                        if (abs(e.BoundBox.ZMin + thickness) < 0.01 and
                            abs(e.BoundBox.ZMax + thickness) < 0.01)]
        if bottom_edges:
            try:
                raft = raft.makeChamfer(chamfer, chamfer, bottom_edges)
            except Exception:
                pass  # chamfer can fail on degenerate geometry

    print(f"Raft: {x1-x0:.1f} x {y1-y0:.1f} x {thickness} "
          f"at Z=[{-thickness:.1f}, 0]")
    return raft


def raise_model(shape, amount=MODEL_RAISE):
    """
    Raise a shape by translating it upward.

    Parameters
    ----------
    shape : Part.Shape
    amount : float
        Distance to raise in Z.

    Returns
    -------
    Part.Shape
    """
    return shape.translated(Vector(0, 0, amount))


def tilt_for_printing(shape, tilt_deg=18.0, display_faces_negative_y=True):
    """
    Tilt a wall for resin printing: interior toward plate, display away.

    The wall is rotated around the X axis (bottom edge) so the interior
    surface faces downward toward the build plate and the display surface
    faces upward/away.

    After tilting, the shape is shifted so its lowest point is at Z=0.

    Parameters
    ----------
    shape : Part.Shape
        The wall shape (oriented with wall plane roughly in XZ, thin in Y).
    tilt_deg : float
        Tilt angle in degrees (15-30 typical).
    display_faces_negative_y : bool
        If True, the display surface is at the -Y side of the wall
        (standard for front walls). If False, display is at +Y side.

    Returns
    -------
    Part.Shape
        Tilted shape with bottom at Z=0.
    """
    # Interior toward plate means:
    # - If display is at -Y: rotate so top tilts toward +Y (negative angle)
    # - If display is at +Y: rotate so top tilts toward -Y (positive angle)
    angle = -tilt_deg if display_faces_negative_y else tilt_deg

    import FreeCAD
    rot = FreeCAD.Rotation(Vector(1, 0, 0), angle)
    tilted = shape.copy()
    tilted.Placement = FreeCAD.Placement(Vector(0, 0, 0), rot)

    # Shift so bottom at Z=0
    z_shift = -tilted.BoundBox.ZMin
    tilted = tilted.translated(Vector(0, 0, z_shift))

    return tilted


def tilted_wall_outward_normal(tilt_deg=18.0, display_faces_negative_y=True):
    """
    Compute the wall outward (display) normal after tilting.

    Parameters
    ----------
    tilt_deg : float
    display_faces_negative_y : bool

    Returns
    -------
    Vector
        Unit normal pointing from interior toward display surface,
        in the tilted frame.
    """
    tilt_rad = math.radians(tilt_deg)
    if display_faces_negative_y:
        # Original normal (0, -1, 0), rotated -tilt_deg around X:
        # Y' = -cos(tilt), Z' = sin(tilt)
        return Vector(0, -math.cos(tilt_rad), math.sin(tilt_rad))
    else:
        # Original normal (0, 1, 0), rotated +tilt_deg around X:
        # Y' = cos(tilt), Z' = -sin(tilt)
        return Vector(0, math.cos(tilt_rad), -math.sin(tilt_rad))


def validate_tilt_direction(contact_points, shape, wall_outward_normal):
    """
    Verify that all support contacts are on the interior (non-display) side.

    Projects each contact onto the wall_outward_normal axis and checks
    that it falls on the interior (negative-projection) side relative to
    the display surface. Works correctly with tilted multi-bay geometry
    where a simple center_y threshold fails.

    Parameters
    ----------
    contact_points : list of (x, y, z)
    shape : Part.Shape
        The tilted model.
    wall_outward_normal : Vector
        Points from interior toward display.

    Returns
    -------
    bool
        True if all contacts are safe (interior side).
    """
    n = Vector(wall_outward_normal)
    n.normalize()

    # Find the display-side extreme: the point on the shape with the
    # largest projection onto wall_outward_normal = the outermost display
    # surface coordinate.
    bb = shape.BoundBox
    # Project all 8 bbox corners onto n and find max (display side)
    corners = [
        Vector(bb.XMin, bb.YMin, bb.ZMin), Vector(bb.XMax, bb.YMin, bb.ZMin),
        Vector(bb.XMin, bb.YMax, bb.ZMin), Vector(bb.XMax, bb.YMax, bb.ZMin),
        Vector(bb.XMin, bb.YMin, bb.ZMax), Vector(bb.XMax, bb.YMin, bb.ZMax),
        Vector(bb.XMin, bb.YMax, bb.ZMax), Vector(bb.XMax, bb.YMax, bb.ZMax),
    ]
    projections = [c.dot(n) for c in corners]
    display_proj = max(projections)   # display surface = max projection
    interior_proj = min(projections)  # interior extremity = min projection
    midplane_proj = (display_proj + interior_proj) / 2.0

    bad = 0
    for cx, cy, cz in contact_points:
        p = Vector(cx, cy, cz)
        proj = p.dot(n)
        # Contact should be on interior side (proj < midplane)
        if proj > midplane_proj:
            bad += 1

    if bad > 0:
        print(f"WARNING: {bad}/{len(contact_points)} contacts on display side "
              f"(proj range [{interior_proj:.1f}, {display_proj:.1f}], "
              f"mid={midplane_proj:.1f})")
        return False
    print(f"Tilt validation: all {len(contact_points)} contacts on interior side "
          f"(proj range [{interior_proj:.1f}, {display_proj:.1f}])")
    return True


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------

def run_support_pipeline(doc, object_name, wall_outward_normal,
                          window_bounds=None, mullion_x=None,
                          raise_amount=MODEL_RAISE,
                          interior_y_side=None):
    """
    Run the full context-aware support pipeline on a model.

    1. Raise model off raft
    2. Classify all faces
    3. Generate support contact points (lintel + bottom)
    4. Build supports
    5. Build raft

    Parameters
    ----------
    doc : FreeCAD.Document
    object_name : str
        Name of the model object in the document.
    wall_outward_normal : Vector
        Points from interior to display surface (original frame).
    window_bounds : dict or None
        {x_min, x_max, z_min, z_max} in original frame.
    mullion_x : float or None
        X center of vertical mullion.
    raise_amount : float
        How far to raise model off raft.
    interior_y_side : str or None
        'min' or 'max' -- which Y side is interior after tilting.
        If None, uses both sides (legacy behavior for single-bay).

    Returns
    -------
    dict with keys: 'classified', 'lintel_contacts', 'bottom_contacts'
    """
    obj = doc.getObject(object_name)
    if obj is None:
        raise ValueError(f"Object '{object_name}' not found")

    # 1. Raise
    raised_shape = raise_model(obj.Shape, raise_amount)
    obj.Shape = raised_shape
    print(f"Raised {object_name} by {raise_amount}mm")

    # 2. Classify
    classified = classify_faces(raised_shape, wall_outward_normal,
                                 window_bounds)
    summarize_classification(classified)

    # 3. Generate contact points
    all_contacts = []

    if window_bounds:
        lintel_contacts = generate_lintel_supports(
            raised_shape, classified, window_bounds, mullion_x)
        all_contacts.extend(lintel_contacts)
    else:
        lintel_contacts = []

    bottom_contacts = generate_bottom_supports(raised_shape, classified,
                                                raise_amount,
                                                interior_y_side)
    all_contacts.extend(bottom_contacts)

    # 3b. For multi-bay panels, also generate all overhang supports
    overhang_contacts = generate_all_overhang_supports(
        raised_shape, classified, wall_outward_normal,
        interior_y_side or 'min')
    # Deduplicate with any lintel/bottom contacts already generated
    existing = set(all_contacts)
    new_overhang = [c for c in overhang_contacts if c not in existing]
    all_contacts.extend(new_overhang)

    # 4. Validate tilt direction
    if all_contacts:
        validate_tilt_direction(all_contacts, raised_shape,
                                wall_outward_normal)

    # 5. Build supports
    if all_contacts:
        support_compound = build_supports(all_contacts)
        sup_obj = doc.addObject("Part::Feature", "Supports")
        sup_obj.Shape = support_compound

    # 6. Build raft (sized to cover all support base pads)
    raft_shape = build_raft(raised_shape, contact_points=all_contacts)
    raft_obj = doc.addObject("Part::Feature", "Raft")
    raft_obj.Shape = raft_shape

    doc.recompute()
    print("Pipeline complete!")

    return {
        'classified': classified,
        'lintel_contacts': lintel_contacts,
        'bottom_contacts': bottom_contacts,
        'overhang_contacts': new_overhang,
        'all_contacts': all_contacts,
    }
