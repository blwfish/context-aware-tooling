"""
Model splitting, registration, and bracing utilities for resin MSLA printing.

Provides functions to split a FreeCAD shape along arbitrary or axis-aligned
planes, add tapered pin/socket registration features for accurate reassembly,
and add temporary sprue-like bracing for structural support during printing.

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

    # Add bracing separately (after registration):
    neg, pos, pins = add_registration_plane(neg, pos, pt, n, return_positions=True)
    neg = add_bracing(neg, pt, n, pins)

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
# Constants — tab/slot registration
# ---------------------------------------------------------------------------

TAB_WIDTH = 2.0                # mm -- tab extent along the split edge
TAB_DEPTH = 1.5                # mm -- tab protrusion from split face
TAB_HEIGHT = 1.0               # mm -- tab extent inward from wall interior edge
TAB_CLEARANCE = 0.12           # mm -- slot oversize on each side
TAB_SPACING = 10.0             # mm -- default spacing along interior edges
TAB_EDGE_MARGIN = 2.0          # mm -- inset from ends of interior edges
TAB_MIN_WALL = 0.8             # mm -- minimum wall thickness for tabs

# ---------------------------------------------------------------------------
# Constants — blister registration
# ---------------------------------------------------------------------------

BLISTER_RADIUS = 1.5           # mm -- boss radius (must be > PIN_RADIUS)
BLISTER_DEPTH = 1.5            # mm -- boss extent from split face into each half
BLISTER_OVERLAP = 0.3          # mm -- embed into wall for solid bond
BLISTER_SPACING = 15.0         # mm -- default spacing along interior edges
BLISTER_EDGE_MARGIN = 3.0     # mm -- inset from ends of interior edges

# ---------------------------------------------------------------------------
# Constants — temporary bracing (sprue runners)
# ---------------------------------------------------------------------------

BRACE_WIDTH = 1.5              # mm -- runner width (perpendicular to run direction)
BRACE_DEPTH = 1.0              # mm -- runner depth (along plane normal, straddles split)
BRACE_NECK_WIDTH = 0.4         # mm -- thin neck at pin connections for snap-off
BRACE_NECK_LENGTH = 1.5        # mm -- length of neck-down zone at each end
BRACE_OFFSET = 0.0             # mm -- offset from wall into hollow (0 = flush with wall)


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
    p = Vector(point)

    # Build two large boxes, one on each side of the split plane.
    # Using boolean cut (shape.common with a solid box) reliably creates
    # cap faces at the cut boundary — unlike half-space common which
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
                            margin=PIN_EDGE_MARGIN, count=None,
                            _grid_resolution=0.5):
    """
    Distribute pin positions across a split face.

    Works with arbitrary planar faces including hollow cross-sections
    (U-shaped, L-shaped, etc.).  Samples a grid of candidate points
    across the face bounding box, keeps only those that actually lie
    on the face material, then selects well-spaced positions using
    greedy farthest-point sampling.

    Parameters
    ----------
    face : Part.Face
        The planar split face.
    plane_normal : Vector
        Normal of the split plane (pin direction).
    spacing : float
        Target spacing between pins. Ignored when count is provided.
    margin : float
        Inset from face edges.
    count : int or None
        Exact number of pins to place. When provided, pins are evenly
        distributed from the valid candidates, overriding spacing.
    _grid_resolution : float
        Candidate grid spacing in mm (smaller = more candidates, slower).

    Returns
    -------
    list of Vector
        Pin center positions on the split face.
    """
    n = Vector(plane_normal)
    n.normalize()
    u, v = _plane_basis(n)

    cog = face.CenterOfGravity

    # Project all face vertices onto the UV plane to find extent
    verts = face.Vertexes
    if len(verts) < 3:
        return []

    u_vals = []
    v_vals = []
    for vert in verts:
        d = vert.Point - cog
        u_vals.append(d.dot(u))
        v_vals.append(d.dot(v))

    u_min, u_max = min(u_vals), max(u_vals)
    v_min, v_max = min(v_vals), max(v_vals)

    # Sample a grid of candidates across the full bounding box, then filter
    # to points that lie on the face AND are at least `margin` from the
    # face boundary (wire edges).  For thin-walled cross-sections the margin
    # is automatically clamped so pins still fit.
    on_face_tol = 0.01  # mm — distToShape threshold for "on the face"
    boundary = face.Wires[0] if face.Wires else None
    candidates = []

    u_steps = max(1, int((u_max - u_min) / _grid_resolution) + 1)
    v_steps = max(1, int((v_max - v_min) / _grid_resolution) + 1)

    for iu in range(u_steps):
        uu = u_min + iu * (u_max - u_min) / max(1, u_steps - 1) if u_steps > 1 else (u_min + u_max) / 2
        for iv in range(v_steps):
            vv = v_min + iv * (v_max - v_min) / max(1, v_steps - 1) if v_steps > 1 else (v_min + v_max) / 2
            pt = cog + u * uu + v * vv
            # Must be on the face
            dist_face = face.distToShape(Part.Vertex(pt))[0]
            if dist_face > on_face_tol:
                continue
            # Prefer points inset from the face boundary.  Store the
            # edge distance so the farthest-point selector favours
            # well-inset positions, but don't discard points on thin
            # walls — the user controls pin count and may accept pins
            # wider than the wall (slicer handles the overhang).
            edge_dist = 0.0
            if boundary:
                edge_dist = boundary.distToShape(Part.Vertex(pt))[0]
            if edge_dist < _grid_resolution * 0.1:
                continue  # on or too near the boundary edge
            candidates.append((pt, edge_dist))

    if not candidates:
        return []

    # Determine how many pins to place
    if count is not None:
        if count < 1:
            return []
        n_pins = min(count, len(candidates))
    else:
        # Estimate from spacing: use the face perimeter as a rough guide
        perim = sum(e.Length for e in face.Wires[0].Edges) if face.Wires else 0
        n_pins = max(2, int(perim / (2 * spacing)) + 1)
        n_pins = min(n_pins, len(candidates))

    # Warn if pins are wider than the wall
    max_edge_dist = max(ed for _, ed in candidates)
    if max_edge_dist < PIN_RADIUS:
        logger.warning(
            f"Wall thickness ({max_edge_dist * 2:.1f}mm) is less than "
            f"pin diameter ({PIN_RADIUS * 2:.1f}mm) — pins will overhang")

    # Greedy farthest-point sampling for well-spaced selection.
    # Start with the candidate that has the greatest edge inset
    # (most centered in the wall material).
    pts = [pt for pt, _ in candidates]

    selected = []
    remaining = list(range(len(candidates)))

    # First point: best edge inset (most centered in material)
    best_idx = max(remaining, key=lambda i: candidates[i][1])
    selected.append(best_idx)
    remaining.remove(best_idx)

    # Subsequent points: farthest from nearest already-selected point
    for _ in range(n_pins - 1):
        if not remaining:
            break
        best_idx = None
        best_min_dist = -1
        for i in remaining:
            min_dist = min((pts[i] - pts[s]).Length for s in selected)
            if min_dist > best_min_dist:
                best_min_dist = min_dist
                best_idx = i
        selected.append(best_idx)
        remaining.remove(best_idx)

    return [pts[i] for i in selected]


# ---------------------------------------------------------------------------
# Interior edge detection
# ---------------------------------------------------------------------------

def _classify_split_face_edges(shape, split_face):
    """
    Classify each edge of the split face as INTERIOR or EXTERIOR.

    Uses the adjacent face normal direction relative to the shape's
    bounding box center.  Interior wall faces have outward normals
    pointing toward the enclosed volume (toward BB center); exterior
    wall faces have normals pointing away.

    Uses OCC's ancestor map for O(1) edge→face lookup instead of
    brute-force iteration over all faces.

    Parameters
    ----------
    shape : Part.Shape
        The full split-half shape (provides adjacent faces and BB).
    split_face : Part.Face
        The planar face lying on the split plane.

    Returns
    -------
    list of (Part.Edge, str)
        Each entry is (edge, 'interior' or 'exterior').
    """
    bb = shape.BoundBox
    bb_center = Vector(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )

    # Build edge midpoint → adjacent face lookup.
    # For each edge of the split face, find any non-split-face face of the
    # shape that shares the edge.  We match edges by midpoint proximity
    # (cheaper than isSame on every combination).
    #
    # Step 1: Index all shape faces by the midpoints of their edges.
    edge_mid_to_faces = {}  # (rounded_x, rounded_y, rounded_z) → list of face
    rnd = 2  # decimal places for rounding — 0.01mm precision
    split_face_id = id(split_face)

    for face in shape.Faces:
        if face.isSame(split_face):
            continue
        for fe in face.Edges:
            fem = fe.valueAt(
                fe.FirstParameter + (fe.LastParameter - fe.FirstParameter) / 2
            )
            key = (round(fem.x, rnd), round(fem.y, rnd), round(fem.z, rnd))
            if key not in edge_mid_to_faces:
                edge_mid_to_faces[key] = []
            edge_mid_to_faces[key].append(face)

    # Step 2: For each split face edge, look up adjacent face via midpoint.
    results = []
    for edge in split_face.Edges:
        mid = edge.valueAt(
            edge.FirstParameter + (edge.LastParameter - edge.FirstParameter) / 2
        )
        key = (round(mid.x, rnd), round(mid.y, rnd), round(mid.z, rnd))

        classified = False
        for face in edge_mid_to_faces.get(key, []):
            try:
                uv = face.Surface.parameter(mid)
                normal = face.normalAt(uv[0], uv[1])
            except Exception:
                continue

            # Outward normal pointing toward BB center → interior face
            to_center = bb_center - mid
            dot = normal.dot(to_center)
            results.append((edge, 'interior' if dot > 0 else 'exterior'))
            classified = True
            break

        if not classified:
            results.append((edge, 'exterior'))

    return results


def _tab_positions_along_edge(edge, plane_normal, wall_dir,
                               spacing=TAB_SPACING, margin=TAB_EDGE_MARGIN,
                               count=None):
    """
    Compute tab center positions along an interior edge of the split face.

    Parameters
    ----------
    edge : Part.Edge
        The interior edge to distribute tabs along.
    plane_normal : Vector
        Normal of the split plane (tab protrusion direction).
    wall_dir : Vector
        Direction from the interior edge into the wall (toward exterior).
    spacing : float
        Target spacing between tabs.
    margin : float
        Inset from edge endpoints.
    count : int or None
        Exact number of tabs on this edge. If None, derived from spacing.

    Returns
    -------
    list of (Vector, Vector, Vector)
        Each entry is (center, plane_normal, wall_dir) — the tab
        center on the split face, the protrusion direction, and the
        inward direction for tab height.
    """
    length = edge.Length
    span = length - 2 * margin
    if span <= 0:
        return []

    if count is not None:
        n_tabs = max(1, count)
    else:
        n_tabs = max(1, int(span / spacing) + 1)

    if n_tabs == 1:
        params = [edge.FirstParameter +
                  (edge.LastParameter - edge.FirstParameter) / 2]
    else:
        step = span / (n_tabs - 1)
        p0 = edge.FirstParameter
        p1 = edge.LastParameter
        param_margin = margin / length * (p1 - p0) if length > 0 else 0
        params = [p0 + param_margin + i * step / length * (p1 - p0)
                  for i in range(n_tabs)]

    positions = []
    for param in params:
        pt = edge.valueAt(param)
        positions.append((pt, Vector(plane_normal), Vector(wall_dir)))

    return positions


def _make_tab_box(center, plane_normal, wall_dir,
                  width, height, d_back, d_front):
    """
    Build a solid box for a tab or slot, straddling the split plane.

    The box extends from -d_back to +d_front along plane_normal,
    ±width/2 along the edge, and height inward from the interior edge.

    Parameters
    ----------
    center : Vector
        Center of the tab on the split face, at the interior wall edge.
    plane_normal : Vector
        Direction from negative half toward positive half.
    wall_dir : Vector
        Direction toward the model interior.
    width : float
        Extent along the split edge.
    height : float
        Extent inward from the wall edge.
    d_back : float
        Extent behind the split face (into the source piece).
    d_front : float
        Extent in front of the split face (into the mating piece).

    Returns
    -------
    Part.Shape
    """
    n = Vector(plane_normal)
    n.normalize()
    into_wall = Vector(wall_dir)
    into_wall.normalize()
    along_edge = n.cross(into_wall)
    along_edge.normalize()

    hw = width / 2
    corners = [
        center + along_edge * s * hw + into_wall * h + n * d
        for s in (-1, 1)
        for h in (0, height)
        for d in (-d_back, d_front)
    ]

    def _quad(a, b, c, d):
        wire = Part.makePolygon([a, b, c, d, a])
        return Part.Face(wire)

    # Corner indices: (s, h, d) -> index
    # (-1,0,-B)=0, (-1,0,+F)=1, (-1,H,-B)=2, (-1,H,+F)=3
    # (+1,0,-B)=4, (+1,0,+F)=5, (+1,H,-B)=6, (+1,H,+F)=7
    c = corners
    faces = [
        _quad(c[0], c[1], c[3], c[2]),  # -edge face
        _quad(c[4], c[6], c[7], c[5]),  # +edge face
        _quad(c[0], c[4], c[5], c[1]),  # bottom (h=0)
        _quad(c[2], c[3], c[7], c[6]),  # top (h=H)
        _quad(c[0], c[2], c[6], c[4]),  # back (d=-B)
        _quad(c[1], c[5], c[7], c[3]),  # front (d=+F)
    ]
    shell = Part.makeShell(faces)
    return Part.makeSolid(shell)


TAB_BASE = 0.3  # mm — shallow base anchoring tongue to source wall


def make_tab(center, plane_normal, wall_dir,
             width=TAB_WIDTH, depth=TAB_DEPTH, height=TAB_HEIGHT):
    """
    Make a registration tab tongue with a shallow base.

    The tongue protrudes from the source piece's split face into
    the mating piece.  A shallow base (TAB_BASE) anchors it to the
    source wall behind the split face.

    Parameters
    ----------
    center : Vector
        Center of the tab on the split face, at the interior wall edge.
    plane_normal : Vector
        Direction from negative half toward positive half.
    wall_dir : Vector
        Direction from the interior edge INTO the wall (toward exterior).
    width : float
        Tab extent along the split edge.
    depth : float
        Tongue protrusion past the split face into the mating piece.
    height : float
        Tab extent into the wall from the interior edge.

    Returns
    -------
    Part.Shape
    """
    return _make_tab_box(center, plane_normal, wall_dir,
                         width, height, d_back=TAB_BASE, d_front=depth)


def make_tab_slot(center, plane_normal, wall_dir,
                  width=TAB_WIDTH, depth=TAB_DEPTH, height=TAB_HEIGHT,
                  clearance=TAB_CLEARANCE):
    """
    Make a slot matching a registration tab, with clearance.

    The slot extends from the split face into the mating piece,
    cutting into the wall material so the tab tongue can seat.

    Parameters
    ----------
    center, plane_normal, wall_dir, width, depth, height :
        Same as make_tab.
    clearance : float
        Oversize on each side for fit.

    Returns
    -------
    Part.Shape
        Solid to be subtracted (boolean cut) from the mating piece.
    """
    return _make_tab_box(center, plane_normal, wall_dir,
                         width=width + 2 * clearance,
                         height=height + clearance,
                         d_back=clearance,  # slight cut past split face
                         d_front=depth + clearance)


def _measure_wall_thickness(shape, point, direction, max_probe=10.0):
    """
    Measure wall thickness at a point by probing along a direction.

    Starting from a point on the interior surface, probes along
    direction (into the wall) to find where the solid ends.

    Parameters
    ----------
    shape : Part.Shape
        The solid shape.
    point : Vector
        Start point on the interior surface.
    direction : Vector
        Unit vector pointing into the wall.
    max_probe : float
        Maximum probe distance.

    Returns
    -------
    float
        Wall thickness in mm.
    """
    d = Vector(direction)
    d.normalize()
    lo, hi = 0.0, max_probe
    for _ in range(20):  # binary search
        test = (lo + hi) / 2
        probe = point + d * test
        if shape.isInside(probe, 0.001, True):
            lo = test
        else:
            hi = test
    return (lo + hi) / 2


def add_tab_registration_plane(neg_half, pos_half, plane_point, plane_normal,
                                tab_count=None):
    """
    Add tab/slot registration features on interior edges of two split halves.

    Tabs protrude from the negative half into slots cut into the
    positive half.  All registration geometry is on the interior side
    of the wall, keeping the exterior surface clean.

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
    tab_count : int or None
        Total number of tabs. Distributed proportionally across
        interior edges by length.  When None, derived from spacing.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_tabs, pos_with_slots)
    """
    n = Vector(plane_normal)
    n.normalize()

    # Find the split face
    split_face = _find_split_face(neg_half, plane_point, n)
    if split_face is None:
        split_face = _find_split_face(pos_half, plane_point, n)
    if split_face is None:
        logger.warning("Could not find split face for tab placement")
        return neg_half, pos_half

    # Classify edges as interior/exterior
    edge_classes = _classify_split_face_edges(neg_half, split_face)
    interior_edges = [(e, c) for e, c in edge_classes if c == 'interior']

    if not interior_edges:
        logger.warning("No interior edges found on split face — "
                       "falling back to pin registration")
        return add_registration_plane(neg_half, pos_half, plane_point, n)

    # Skip edges too short to hold a tab
    interior_edges = [(e, c) for e, c in interior_edges
                      if e.Length >= TAB_WIDTH]
    total_interior_length = sum(e.Length for e, _ in interior_edges)
    logger.info(f"Found {len(interior_edges)} interior edges (>={TAB_WIDTH}mm), "
                f"total length {total_interior_length:.1f}mm")

    # Compute interior direction for each edge (toward BB center)
    bb = neg_half.BoundBox
    bb_center = Vector(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )

    all_tab_params = []  # list of (center, plane_normal, wall_dir, tab_height, wall_thickness)
    for edge, _ in interior_edges:
        # Distribute tab positions along this edge
        if tab_count is not None:
            edge_count = max(1, round(tab_count * edge.Length / total_interior_length))
        else:
            edge_count = None

        # Get positions only (wall_dir placeholder — will recompute per-position)
        tabs = _tab_positions_along_edge(edge, n, Vector(0, 0, 0), count=edge_count)

        for center, pn, _ in tabs:
            # Compute wall_dir at THIS tab position, not the edge midpoint.
            # Critical for curved edges (circles, arcs) where the radial
            # direction varies along the edge.
            to_center = bb_center - center
            # Remove component along plane normal
            to_center = to_center - n * to_center.dot(n)
            # Remove component along edge tangent at this position
            param = edge.Curve.parameter(center)
            edge_tangent = edge.tangentAt(param)
            to_center = to_center - edge_tangent * to_center.dot(edge_tangent)
            if to_center.Length < 1e-6:
                continue
            to_center.normalize()
            wall_dir = to_center * -1  # flip: into wall, not into hollow

            # Measure wall thickness at this position
            wall_thickness = _measure_wall_thickness(neg_half, center, wall_dir)
            if wall_thickness < TAB_MIN_WALL:
                logger.warning(f"Wall too thin ({wall_thickness:.2f}mm) for tabs, "
                               f"need >={TAB_MIN_WALL}mm")
                continue
            tab_height = min(TAB_HEIGHT, wall_thickness / 2.0)

            all_tab_params.append((center, pn, wall_dir, tab_height))

    if not all_tab_params:
        logger.warning("No tab positions found on interior edges")
        return neg_half, pos_half

    tab_shapes = []
    slot_shapes = []
    for center, pn, wdir, th in all_tab_params:
        tab_shapes.append(make_tab(center, pn, wdir, height=th))
        # Slot uses tab height — make_tab_slot adds clearance internally
        slot_shapes.append(make_tab_slot(center, pn, wdir, height=th))

    # Fuse tabs onto negative half
    tab_compound = Part.Compound(tab_shapes)
    neg_result = neg_half.fuse(tab_compound)

    # Cut slots from positive half
    slot_compound = Part.Compound(slot_shapes)
    pos_result = pos_half.cut(slot_compound)

    print(f"Added {len(all_tab_params)} registration tab/slot pairs "
          f"on {len(interior_edges)} interior edges")
    return neg_result, pos_result


# ---------------------------------------------------------------------------
# Blister registration — for thin-walled hollow structures
# ---------------------------------------------------------------------------

def make_blister(center, plane_normal, blister_dir,
                 radius=BLISTER_RADIUS, depth=BLISTER_DEPTH,
                 overlap=BLISTER_OVERLAP):
    """
    Make a blister (cylindrical boss) on the interior wall surface.

    The blister is a cylinder with axis along plane_normal, offset
    from the interior edge into the hollow.  It provides a mounting
    platform for pin/socket registration where the native wall is
    too thin.

    Returns a pair of half-blisters (neg_side, pos_side) split at
    the center point.

    Parameters
    ----------
    center : Vector
        Point on the interior edge at the split plane.
    plane_normal : Vector
        Direction from negative half toward positive half.
    blister_dir : Vector
        Direction from interior edge into the hollow (away from wall).
    radius : float
        Boss radius.
    depth : float
        How far the boss extends from the split plane into each half.
    overlap : float
        How much the boss overlaps into the wall for bonding.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_blister, pos_blister) — half-cylinders for each side.
    """
    n = Vector(plane_normal)
    n.normalize()
    bd = Vector(blister_dir)
    bd.normalize()

    # Offset center into hollow, minus overlap so blister embeds into wall
    blister_center = center + bd * (radius - overlap)

    # Neg-side half: extends backward from split plane
    neg_base = blister_center - n * depth
    neg_blister = Part.makeCylinder(radius, depth, neg_base, n)

    # Pos-side half: extends forward from split plane
    pos_blister = Part.makeCylinder(radius, depth, blister_center, n)

    return neg_blister, pos_blister


def _blister_positions_along_edge(edge, plane_normal,
                                  spacing=BLISTER_SPACING,
                                  margin=BLISTER_EDGE_MARGIN,
                                  count=None):
    """
    Distribute blister positions along an interior edge.

    Parameters
    ----------
    edge : Part.Edge
        The interior edge to place blisters along.
    plane_normal : Vector
        Split plane normal.
    spacing : float
        Nominal distance between blisters.
    margin : float
        Inset from edge ends.
    count : int or None
        Exact count. When None, derived from spacing.

    Returns
    -------
    list of Vector
        Blister center positions on the edge.
    """
    length = edge.Length
    usable = length - 2 * margin
    if usable < 0:
        return []

    if count is None:
        count = max(1, round(usable / spacing))

    if count == 1:
        mid_param = (edge.FirstParameter + edge.LastParameter) / 2
        return [edge.valueAt(mid_param)]

    positions = []
    for i in range(count):
        t = margin + usable * i / (count - 1)
        param = edge.getParameterByLength(t)
        positions.append(edge.valueAt(param))
    return positions


def add_blister_registration_plane(neg_half, pos_half, plane_point,
                                   plane_normal, blister_count=None):
    """
    Add blister + pin/socket registration on thin-walled interior edges.

    For hollow models where the wall is too thin for tabs, this adds
    cylindrical bosses (blisters) on the interior wall surface at the
    split boundary, then places pin/socket pairs on the blister faces.

    Parameters
    ----------
    neg_half, pos_half : Part.Shape
        The two halves to register.
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal vector (points from neg toward pos).
    blister_count : int or None
        Total number of blisters. When None, derived from spacing.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_blisters_and_pins, pos_with_blisters_and_sockets)
    """
    n = Vector(plane_normal)
    n.normalize()

    # Find split face and interior edges
    split_face = _find_split_face(neg_half, plane_point, n)
    if split_face is None:
        split_face = _find_split_face(pos_half, plane_point, n)
    if split_face is None:
        logger.warning("Could not find split face for blister placement")
        return neg_half, pos_half

    classes = _classify_split_face_edges(neg_half, split_face)
    interior_edges = [(e, c) for e, c in classes if c == 'interior']
    if not interior_edges:
        logger.warning("No interior edges found — cannot place blisters")
        return neg_half, pos_half

    # Only use edges long enough for blisters
    interior_edges = [(e, c) for e, c in interior_edges
                      if e.Length >= 2 * BLISTER_EDGE_MARGIN]
    total_interior_length = sum(e.Length for e, _ in interior_edges)

    if total_interior_length < 1.0:
        logger.warning("Interior edges too short for blisters")
        return neg_half, pos_half

    logger.info(f"Found {len(interior_edges)} interior edges for blisters, "
                f"total length {total_interior_length:.1f}mm")

    bb = neg_half.BoundBox
    bb_center = Vector(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )

    neg_blisters = []
    pos_blisters = []
    pin_shapes = []
    socket_shapes = []

    for edge, _ in interior_edges:
        # Distribute blister count proportionally by edge length
        if blister_count is not None:
            edge_count = max(1, round(
                blister_count * edge.Length / total_interior_length))
        else:
            edge_count = None

        positions = _blister_positions_along_edge(
            edge, n, count=edge_count)

        for center in positions:
            # Compute blister_dir (into hollow) at this position
            to_center = bb_center - center
            to_center = to_center - n * to_center.dot(n)
            param = edge.Curve.parameter(center)
            edge_tangent = edge.tangentAt(param)
            to_center = to_center - edge_tangent * to_center.dot(edge_tangent)
            if to_center.Length < 1e-6:
                continue
            to_center.normalize()
            # blister_dir = toward BB center = into hollow
            blister_dir = to_center

            neg_b, pos_b = make_blister(center, n, blister_dir)
            neg_blisters.append(neg_b)
            pos_blisters.append(pos_b)

            # Pin/socket at the blister face (at the split plane)
            # Offset into hollow so pin sits on blister surface, not on wall
            pin_center = center + blister_dir * (BLISTER_RADIUS - BLISTER_OVERLAP)
            pin_shapes.append(make_pin(pin_center, n))
            socket_shapes.append(make_socket(pin_center, n))

    if not neg_blisters:
        logger.warning("No blister positions found")
        return neg_half, pos_half

    # Fuse blisters + pins onto negative half
    neg_compound = Part.Compound(neg_blisters + pin_shapes)
    neg_result = neg_half.fuse(neg_compound)

    # Fuse blisters onto positive half, then cut sockets
    pos_compound = Part.Compound(pos_blisters)
    pos_result = pos_half.fuse(pos_compound)
    sock_compound = Part.Compound(socket_shapes)
    pos_result = pos_result.cut(sock_compound)

    print(f"Added {len(pin_shapes)} blister registration pairs "
          f"on {len(interior_edges)} interior edges")
    return neg_result, pos_result


def add_registration_plane(neg_half, pos_half, plane_point, plane_normal,
                           pin_count=None, return_positions=False):
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
    pin_count : int or None
        Exact number of pins. When None, count is derived from spacing.
    return_positions : bool
        If True, also return the list of pin positions (for downstream
        use by bracing, etc.).

    Returns
    -------
    tuple of (Part.Shape, Part.Shape) or (Part.Shape, Part.Shape, list)
        (neg_with_pins, pos_with_sockets) or
        (neg_with_pins, pos_with_sockets, pin_positions) when return_positions=True.
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
        if return_positions:
            return neg_half, pos_half, []
        return neg_half, pos_half

    positions = _pin_positions_on_face(split_face, n, count=pin_count)
    if not positions:
        logger.warning("No room for registration pins on split face")
        if return_positions:
            return neg_half, pos_half, []
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

    if return_positions:
        return neg_result, pos_result, positions
    return neg_result, pos_result


def add_registration(neg_half, pos_half, axis, split_pos, pin_axis=None,
                     pin_count=None):
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
    pin_count : int or None
        Exact number of pins. When None, count is derived from spacing.

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
    return add_registration_plane(neg_half, pos_half, point, normal,
                                  pin_count=pin_count)


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


# ---------------------------------------------------------------------------
# Cut face analysis — reusable result from split face detection
# ---------------------------------------------------------------------------

class CutFaceAnalysis:
    """
    Reusable analysis of a split face: the face itself, edge classifications,
    and the bounding box center for interior direction computation.
    """

    def __init__(self, split_face, edge_classes, bb_center):
        self.split_face = split_face
        self.edge_classes = edge_classes  # list of (edge, 'interior'|'exterior')
        self.bb_center = bb_center

    @property
    def interior_edges(self):
        return [e for e, c in self.edge_classes if c == 'interior']

    @property
    def exterior_edges(self):
        return [e for e, c in self.edge_classes if c == 'exterior']

    @property
    def wall_edges(self):
        """Interior edges that are roughly horizontal (not floor/ceiling).

        Filters to edges whose direction has a significant component
        perpendicular to the vertical (Z) axis.  Floor edges run
        horizontally but are interior edges at the bottom of the model;
        wall edges are the vertical interior edges we want braces on.

        For a horizontal split plane (normal along Y, say), the
        "wall edges" are the ones that run along X or Z on interior walls.
        We keep all interior edges here — the caller filters floor vs wall
        based on the specific model orientation.
        """
        return self.interior_edges


def analyze_cut_face(shape, plane_point, plane_normal):
    """
    Analyze the cut face on a split half: find the face, classify edges.

    Parameters
    ----------
    shape : Part.Shape
        A split half.
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal of the split plane.

    Returns
    -------
    CutFaceAnalysis or None
        Analysis result, or None if the split face wasn't found.
    """
    n = Vector(plane_normal)
    n.normalize()

    split_face = _find_split_face(shape, plane_point, n)
    if split_face is None:
        return None

    edge_classes = _classify_split_face_edges(shape, split_face)

    bb = shape.BoundBox
    bb_center = Vector(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )

    return CutFaceAnalysis(split_face, edge_classes, bb_center)


# ---------------------------------------------------------------------------
# Temporary bracing — sprue runners connecting pin bases
# ---------------------------------------------------------------------------

def _project_point_to_edge(point, edge):
    """
    Project a point onto an edge and return (parameter, projected_point, distance).
    """
    try:
        dist_info = edge.distToShape(Part.Vertex(point))
        closest_pt = dist_info[1][0][1]  # closest point on edge
        param = edge.Curve.parameter(closest_pt)
        return param, closest_pt, dist_info[0]
    except Exception:
        return None, None, float('inf')


def _find_pins_near_edge(pin_positions, edge, max_dist=3.0):
    """
    Find pin positions that are close to a given edge.

    Returns list of (pin_index, parameter_on_edge, projected_point)
    sorted by parameter along the edge.
    """
    results = []
    for i, pos in enumerate(pin_positions):
        param, proj_pt, dist = _project_point_to_edge(pos, edge)
        if dist <= max_dist and param is not None:
            results.append((i, param, proj_pt))

    # Sort by parameter along edge
    results.sort(key=lambda x: x[1])
    return results


def _interior_direction_at(point, edge, plane_normal, bb_center):
    """
    Compute the direction from an interior edge point into the hollow.

    This is the same computation used for blister_dir and tab wall_dir,
    factored out for reuse.

    Returns a unit vector pointing into the hollow (toward BB center),
    or None if degenerate.
    """
    n = Vector(plane_normal)
    n.normalize()

    to_center = bb_center - point
    # Remove plane-normal component
    to_center = to_center - n * to_center.dot(n)
    # Remove edge-tangent component
    try:
        param = edge.Curve.parameter(point)
        tangent = edge.tangentAt(param)
        to_center = to_center - tangent * to_center.dot(tangent)
    except Exception:
        pass
    if to_center.Length < 1e-6:
        return None
    to_center.normalize()
    return to_center


def _make_runner_segment(start, end, plane_normal, interior_dir,
                         width=BRACE_WIDTH, depth=BRACE_DEPTH):
    """
    Make a single runner segment (rectangular bar) between two points.

    The bar runs from start to end, with cross-section width x depth.
    Width is measured perpendicular to both the run direction and
    plane_normal (i.e., into the hollow / along the wall).
    Depth straddles the split plane along plane_normal.

    Parameters
    ----------
    start, end : Vector
        Endpoints of the runner segment.
    plane_normal : Vector
        Split plane normal.
    interior_dir : Vector
        Direction into the hollow from the wall edge.
    width : float
        Cross-section width (into hollow).
    depth : float
        Cross-section depth (along plane normal).

    Returns
    -------
    Part.Shape
        Solid bar.
    """
    n = Vector(plane_normal)
    n.normalize()
    d = Vector(interior_dir)
    d.normalize()

    run_dir = end - start
    length = run_dir.Length
    if length < 0.01:
        return None
    run_dir.normalize()

    # Cross-section axes:
    # - along plane_normal: ±depth/2
    # - into hollow (interior_dir): 0 to width (offset from wall edge)
    hd = depth / 2

    # Build the 4 corners of the cross-section at start
    c1 = start - n * hd + d * BRACE_OFFSET
    c2 = start + n * hd + d * BRACE_OFFSET
    c3 = start + n * hd + d * (BRACE_OFFSET + width)
    c4 = start - n * hd + d * (BRACE_OFFSET + width)

    # Extrude along run direction
    wire = Part.makePolygon([c1, c2, c3, c4, c1])
    face = Part.Face(wire)
    bar = face.extrude(run_dir * length)
    return bar


def _make_neck_notch(point, run_dir, plane_normal, interior_dir,
                     width=BRACE_WIDTH, depth=BRACE_DEPTH,
                     neck_width=BRACE_NECK_WIDTH,
                     neck_length=BRACE_NECK_LENGTH):
    """
    Make a pair of notch solids that, when subtracted from a runner,
    create a thin neck at a connection point.

    The notches cut from both sides of the runner (along interior_dir),
    leaving only neck_width of material in the center.

    Returns a list of Part.Shape (notch solids to subtract).
    """
    n = Vector(plane_normal)
    n.normalize()
    d = Vector(interior_dir)
    d.normalize()
    r = Vector(run_dir)
    r.normalize()

    hd = depth / 2
    hl = neck_length / 2
    notch_depth = (width - neck_width) / 2

    if notch_depth < 0.05:
        return []  # runner is already thin enough

    notches = []

    # Notch on the wall side (near edge, cutting from offset toward center)
    wall_base = point - r * hl - n * (hd + 0.1) + d * BRACE_OFFSET
    wall_notch = Part.makeBox(
        neck_length, depth + 0.2, notch_depth,
        wall_base, r
    )
    # Need to orient the box properly — use explicit corners instead
    wb1 = point - r * hl - n * (hd + 0.1) + d * BRACE_OFFSET
    wb2 = point + r * hl - n * (hd + 0.1) + d * BRACE_OFFSET
    wb3 = point + r * hl + n * (hd + 0.1) + d * BRACE_OFFSET
    wb4 = point - r * hl + n * (hd + 0.1) + d * BRACE_OFFSET
    wt1 = wb1 + d * notch_depth
    wt2 = wb2 + d * notch_depth
    wt3 = wb3 + d * notch_depth
    wt4 = wb4 + d * notch_depth

    def _make_box_from_8pts(pts):
        """Make a solid from 8 corner points (4 bottom + 4 top)."""
        def _quad(a, b, c, dd):
            wire = Part.makePolygon([a, b, c, dd, a])
            return Part.Face(wire)
        b = pts[:4]
        t = pts[4:]
        faces = [
            _quad(b[0], b[1], b[2], b[3]),  # bottom
            _quad(t[0], t[3], t[2], t[1]),  # top
            _quad(b[0], b[3], t[3], t[0]),  # side
            _quad(b[1], t[1], t[2], b[2]),  # side
            _quad(b[0], t[0], t[1], b[1]),  # side
            _quad(b[2], b[3], t[3], t[2]),  # side (not used, but needed for closed shell)
        ]
        # Simpler: just use extrude
        return None

    # Simpler approach: build notch as extruded rectangle
    # Wall-side notch
    notch_profile_start = point - r * hl + d * BRACE_OFFSET
    notch_profile_end = point - r * hl + d * (BRACE_OFFSET + notch_depth)
    notch_c1 = notch_profile_start - n * (hd + 0.1)
    notch_c2 = notch_profile_start + n * (hd + 0.1)
    notch_c3 = notch_profile_end + n * (hd + 0.1)
    notch_c4 = notch_profile_end - n * (hd + 0.1)
    notch_wire = Part.makePolygon([notch_c1, notch_c2, notch_c3, notch_c4, notch_c1])
    notch_face = Part.Face(notch_wire)
    wall_notch = notch_face.extrude(r * neck_length)
    notches.append(wall_notch)

    # Hollow-side notch (from outer edge of runner inward)
    notch_profile_start2 = point - r * hl + d * (BRACE_OFFSET + width - notch_depth)
    notch_profile_end2 = point - r * hl + d * (BRACE_OFFSET + width + 0.1)
    notch_c5 = notch_profile_start2 - n * (hd + 0.1)
    notch_c6 = notch_profile_start2 + n * (hd + 0.1)
    notch_c7 = notch_profile_end2 + n * (hd + 0.1)
    notch_c8 = notch_profile_end2 - n * (hd + 0.1)
    notch_wire2 = Part.makePolygon([notch_c5, notch_c6, notch_c7, notch_c8, notch_c5])
    notch_face2 = Part.Face(notch_wire2)
    hollow_notch = notch_face2.extrude(r * neck_length)
    notches.append(hollow_notch)

    return notches


def _is_floor_edge(edge, plane_normal, floor_normal_threshold=0.7):
    """
    Determine if an interior edge is a floor/ceiling edge vs a wall edge.

    Floor edges are those where the adjacent wall face is roughly
    horizontal (face normal has a large vertical component).
    For buildings, floor edges run horizontally at the bottom of walls.

    We approximate by checking if the edge direction is predominantly
    horizontal. Wall edges on a vertical split tend to be vertical or
    angled; floor edges are horizontal.

    For a typical building split horizontally (normal along Y):
    - Wall edges run vertically (along Z) or along X
    - Floor edges also run along X or Z but at the bottom

    Since we can't easily distinguish wall-bottom from floor edges by
    direction alone, we use a heuristic: edges near the bottom of the
    bounding box of the split face are floor candidates.

    Parameters
    ----------
    edge : Part.Edge
        The edge to check.
    plane_normal : Vector
        Split plane normal.
    floor_normal_threshold : float
        Not used in current heuristic but reserved.

    Returns
    -------
    bool
    """
    # For now, don't filter — let the caller decide.
    # The brace system connects pin-to-pin, and pins are placed by
    # the pin placement algorithm which already avoids floor areas
    # for typical building models.
    return False


def add_bracing(piece, plane_point, plane_normal, pin_positions,
                exclude_floor=True):
    """
    Add temporary sprue-like bracing connecting pin bases along interior walls.

    Braces are thin runners that connect adjacent pin positions along
    interior wall edges of the split face. They provide structural
    support during printing and are designed for snap-off removal via
    thin neck-down sections at each pin connection.

    Call this AFTER pin/socket placement, passing the pin positions used.

    Parameters
    ----------
    piece : Part.Shape
        The split half to add bracing to (typically the piece with pins).
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal of the split plane.
    pin_positions : list of Vector
        Pin center positions on the split face (from _pin_positions_on_face
        or equivalent).
    exclude_floor : bool
        If True, skip edges classified as floor edges (not yet implemented —
        reserved for future use).

    Returns
    -------
    Part.Shape
        The piece with bracing runners fused on.
    """
    if len(pin_positions) < 2:
        logger.info("Need at least 2 pins for bracing — skipping")
        return piece

    n = Vector(plane_normal)
    n.normalize()

    # Analyze the cut face
    analysis = analyze_cut_face(piece, plane_point, n)
    if analysis is None:
        logger.warning("Could not find split face for bracing")
        return piece

    interior_edges = analysis.interior_edges
    if not interior_edges:
        logger.warning("No interior edges found — skipping bracing")
        return piece

    runner_shapes = []
    notch_shapes = []
    connections_made = 0

    for edge in interior_edges:
        if exclude_floor and _is_floor_edge(edge, n):
            continue

        # Find which pins are near this edge
        pins_on_edge = _find_pins_near_edge(pin_positions, edge,
                                             max_dist=PIN_RADIUS + BRACE_WIDTH + 1.0)
        if len(pins_on_edge) < 2:
            continue

        # Compute interior direction at edge midpoint
        edge_mid = edge.valueAt(
            (edge.FirstParameter + edge.LastParameter) / 2)
        interior_dir = _interior_direction_at(
            edge_mid, edge, n, analysis.bb_center)
        if interior_dir is None:
            continue

        # Create runners between adjacent pin pairs along this edge
        for j in range(len(pins_on_edge) - 1):
            idx_a, param_a, proj_a = pins_on_edge[j]
            idx_b, param_b, proj_b = pins_on_edge[j + 1]

            # Use the actual pin positions (on the split face) as endpoints
            start = pin_positions[idx_a]
            end = pin_positions[idx_b]

            seg_length = (end - start).Length
            if seg_length < BRACE_NECK_LENGTH * 2 + 0.5:
                # Too short for a runner with two neck-downs
                continue

            runner = _make_runner_segment(start, end, n, interior_dir)
            if runner is None:
                continue
            runner_shapes.append(runner)

            # Add neck-down notches at each end
            run_dir = end - start
            run_dir.normalize()

            notches_start = _make_neck_notch(start, run_dir, n, interior_dir)
            notches_end = _make_neck_notch(end, run_dir, n, interior_dir)
            notch_shapes.extend(notches_start)
            notch_shapes.extend(notches_end)
            connections_made += 1

    if not runner_shapes:
        logger.info("No brace connections possible between pins")
        return piece

    # Fuse all runners together first
    if len(runner_shapes) == 1:
        runner_compound = runner_shapes[0]
    else:
        runner_compound = runner_shapes[0]
        for rs in runner_shapes[1:]:
            runner_compound = runner_compound.fuse(rs)

    # Cut notches for neck-downs
    if notch_shapes:
        notch_compound = Part.Compound(notch_shapes)
        runner_compound = runner_compound.cut(notch_compound)

    # Fuse bracing onto the piece
    result = piece.fuse(runner_compound)

    print(f"Added {connections_made} brace runner(s) connecting "
          f"{len(pin_positions)} pin positions")
    return result


def add_bracing_both(neg_half, pos_half, plane_point, plane_normal,
                     pin_positions):
    """
    Add bracing to both halves of a split model.

    Adds runners to the negative half (which has pins) and matching
    runners to the positive half. The bracing on each half extends
    from the split face into that half's material.

    Parameters
    ----------
    neg_half, pos_half : Part.Shape
        The two registered halves.
    plane_point : Vector
        A point on the split plane.
    plane_normal : Vector
        Normal of the split plane.
    pin_positions : list of Vector
        Pin positions used during registration.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape)
        (neg_with_bracing, pos_with_bracing)
    """
    neg_result = add_bracing(neg_half, plane_point, plane_normal,
                              pin_positions)
    pos_result = add_bracing(pos_half, plane_point, plane_normal,
                              pin_positions)
    return neg_result, pos_result
