"""
Registration feature utilities for split model reassembly.

Provides pin/socket, tab/slot, and blister registration features for
aligning split pieces during glue-up. Three strategies for different
wall thicknesses:

- **Pins/sockets** (default): Tapered cones on the split face.
  Works on solid cross-sections or thick walls.
- **Tabs/slots**: Tongue-and-groove on interior wall edges.
  Better for hollow models with walls >= 0.8mm.
- **Blisters**: Cylindrical bosses in the hollow interior with
  pins/sockets on the blister face. For thin walls < 0.8mm.

Also includes split-face analysis (CutFaceAnalysis) and edge
classification used by both registration and bracing.

Usage (from FreeCAD MCP execute_python):
    from registration import add_registration_plane, add_tab_registration_plane

All dimensions are in print-scale mm (not prototype scale).
"""

import Part
import FreeCAD
from FreeCAD import Vector
import math
import logging

from split import _plane_basis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants -- pin/socket registration
# ---------------------------------------------------------------------------

PIN_RADIUS = 0.6               # mm -- pin outer radius at print scale
PIN_HEIGHT = 1.5               # mm -- pin length (extends from split face)
PIN_DRAFT_ANGLE = 2.0          # degrees -- slight taper for press-fit
PIN_CLEARANCE = 0.12           # mm radial clearance for socket
PIN_SPACING = 15.0             # mm default spacing along split edge
PIN_EDGE_MARGIN = 3.0          # mm inset from ends of split edge

# ---------------------------------------------------------------------------
# Constants -- tab/slot registration
# ---------------------------------------------------------------------------

TAB_WIDTH = 2.0                # mm -- tab extent along the split edge
TAB_DEPTH = 1.5                # mm -- tab protrusion from split face
TAB_HEIGHT = 1.0               # mm -- tab extent inward from wall interior edge
TAB_CLEARANCE = 0.12           # mm -- slot oversize on each side
TAB_SPACING = 10.0             # mm -- default spacing along interior edges
TAB_EDGE_MARGIN = 2.0          # mm -- inset from ends of interior edges
TAB_MIN_WALL = 0.8             # mm -- minimum wall thickness for tabs
TAB_BASE = 0.3                 # mm -- shallow base anchoring tongue to source wall

# ---------------------------------------------------------------------------
# Constants -- blister registration
# ---------------------------------------------------------------------------

BLISTER_RADIUS = 1.5           # mm -- boss radius (must be > PIN_RADIUS)
BLISTER_DEPTH = 1.5            # mm -- boss extent from split face into each half
BLISTER_OVERLAP = 0.3          # mm -- embed into wall for solid bond
BLISTER_SPACING = 15.0         # mm -- default spacing along interior edges
BLISTER_EDGE_MARGIN = 3.0     # mm -- inset from ends of interior edges


# ---------------------------------------------------------------------------
# Split face detection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Interior/exterior edge classification
# ---------------------------------------------------------------------------

def _classify_split_face_edges(shape, split_face):
    """
    Classify each edge of the split face as INTERIOR or EXTERIOR.

    Uses the adjacent face normal direction relative to the shape's
    bounding box center.  Interior wall faces have outward normals
    pointing toward the enclosed volume (toward BB center); exterior
    wall faces have normals pointing away.

    Uses a midpoint hash map for O(n+m) edge-face lookup instead of
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

    # Build edge midpoint -> adjacent face lookup.
    edge_mid_to_faces = {}
    rnd = 2  # decimal places for rounding -- 0.01mm precision

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

    # For each split face edge, look up adjacent face via midpoint.
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

            # Outward normal pointing toward BB center -> interior face
            to_center = bb_center - mid
            dot = normal.dot(to_center)
            results.append((edge, 'interior' if dot > 0 else 'exterior'))
            classified = True
            break

        if not classified:
            results.append((edge, 'exterior'))

    return results


# ---------------------------------------------------------------------------
# Cut face analysis -- reusable result from split face detection
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
        """Interior edges (alias for downstream use by bracing)."""
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
# Pin placement on face
# ---------------------------------------------------------------------------

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
    # face boundary (wire edges).
    on_face_tol = 0.01
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
            edge_dist = 0.0
            if boundary:
                edge_dist = boundary.distToShape(Part.Vertex(pt))[0]
            if edge_dist < _grid_resolution * 0.1:
                continue
            candidates.append((pt, edge_dist))

    if not candidates:
        return []

    # Determine how many pins to place
    if count is not None:
        if count < 1:
            return []
        n_pins = min(count, len(candidates))
    else:
        perim = sum(e.Length for e in face.Wires[0].Edges) if face.Wires else 0
        n_pins = max(2, int(perim / (2 * spacing)) + 1)
        n_pins = min(n_pins, len(candidates))

    # Warn if pins are wider than the wall
    max_edge_dist = max(ed for _, ed in candidates)
    if max_edge_dist < PIN_RADIUS:
        logger.warning(
            f"Wall thickness ({max_edge_dist * 2:.1f}mm) is less than "
            f"pin diameter ({PIN_RADIUS * 2:.1f}mm) -- pins will overhang")

    # Greedy farthest-point sampling for well-spaced selection.
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
# Pin/socket geometry
# ---------------------------------------------------------------------------

def _pin_positions_along_edge(shape, axis, split_pos, pin_axis):
    """
    Compute pin center positions along a split face (axis-aligned).

    Distributes pins at PIN_SPACING intervals along pin_axis, inset
    from the edges of the split face.
    """
    bb = shape.BoundBox
    remaining = [a for a in ['x', 'y', 'z'] if a != axis and a != pin_axis][0]

    def _range(ax):
        if ax == 'x': return bb.XMin, bb.XMax
        if ax == 'y': return bb.YMin, bb.YMax
        if ax == 'z': return bb.ZMin, bb.ZMax

    pa_min, pa_max = _range(pin_axis)
    ra_min, ra_max = _range(remaining)

    span = pa_max - pa_min - 2 * PIN_EDGE_MARGIN
    if span <= 0:
        return []
    count = max(2, int(span / PIN_SPACING) + 1)
    if count == 1:
        pa_positions = [(pa_min + pa_max) / 2.0]
    else:
        step = span / (count - 1)
        pa_positions = [pa_min + PIN_EDGE_MARGIN + i * step
                        for i in range(count)]

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
    sock_height = height + clearance
    socket = Part.makeCone(sock_radius, sock_tip, sock_height,
                           center, direction)
    return socket


# ---------------------------------------------------------------------------
# Tab/slot geometry
# ---------------------------------------------------------------------------

def _tab_positions_along_edge(edge, plane_normal, wall_dir,
                               spacing=TAB_SPACING, margin=TAB_EDGE_MARGIN,
                               count=None):
    """
    Compute tab center positions along an interior edge of the split face.
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

    c = corners
    faces = [
        _quad(c[0], c[1], c[3], c[2]),
        _quad(c[4], c[6], c[7], c[5]),
        _quad(c[0], c[4], c[5], c[1]),
        _quad(c[2], c[3], c[7], c[6]),
        _quad(c[0], c[2], c[6], c[4]),
        _quad(c[1], c[5], c[7], c[3]),
    ]
    shell = Part.makeShell(faces)
    return Part.makeSolid(shell)


def make_tab(center, plane_normal, wall_dir,
             width=TAB_WIDTH, depth=TAB_DEPTH, height=TAB_HEIGHT):
    """
    Make a registration tab tongue with a shallow base.
    """
    return _make_tab_box(center, plane_normal, wall_dir,
                         width, height, d_back=TAB_BASE, d_front=depth)


def make_tab_slot(center, plane_normal, wall_dir,
                  width=TAB_WIDTH, depth=TAB_DEPTH, height=TAB_HEIGHT,
                  clearance=TAB_CLEARANCE):
    """
    Make a slot matching a registration tab, with clearance.
    """
    return _make_tab_box(center, plane_normal, wall_dir,
                         width=width + 2 * clearance,
                         height=height + clearance,
                         d_back=clearance,
                         d_front=depth + clearance)


def _measure_wall_thickness(shape, point, direction, max_probe=10.0):
    """
    Measure wall thickness at a point by probing along a direction.
    """
    d = Vector(direction)
    d.normalize()
    lo, hi = 0.0, max_probe
    for _ in range(20):
        test = (lo + hi) / 2
        probe = point + d * test
        if shape.isInside(probe, 0.001, True):
            lo = test
        else:
            hi = test
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Blister geometry
# ---------------------------------------------------------------------------

def make_blister(center, plane_normal, blister_dir,
                 radius=BLISTER_RADIUS, depth=BLISTER_DEPTH,
                 overlap=BLISTER_OVERLAP):
    """
    Make a blister (cylindrical boss) on the interior wall surface.

    Returns a pair of half-blisters (neg_side, pos_side) split at
    the center point.
    """
    n = Vector(plane_normal)
    n.normalize()
    bd = Vector(blister_dir)
    bd.normalize()

    blister_center = center + bd * (radius - overlap)

    neg_base = blister_center - n * depth
    neg_blister = Part.makeCylinder(radius, depth, neg_base, n)

    pos_blister = Part.makeCylinder(radius, depth, blister_center, n)

    return neg_blister, pos_blister


def _blister_positions_along_edge(edge, plane_normal,
                                  spacing=BLISTER_SPACING,
                                  margin=BLISTER_EDGE_MARGIN,
                                  count=None):
    """
    Distribute blister positions along an interior edge.
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


# ---------------------------------------------------------------------------
# Pin/socket registration pipeline
# ---------------------------------------------------------------------------

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
        If True, also return the list of pin positions.

    Returns
    -------
    tuple of (Part.Shape, Part.Shape) or (Part.Shape, Part.Shape, list)
        (neg_with_pins, pos_with_sockets) or
        (neg_with_pins, pos_with_sockets, pin_positions) when return_positions=True.
    """
    n = Vector(plane_normal)
    n.normalize()

    split_face = _find_split_face(neg_half, plane_point, n)
    if split_face is None:
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

    pin_dir = n

    pin_shapes = []
    socket_shapes = []
    for pos in positions:
        pin_shapes.append(make_pin(pos, pin_dir))
        socket_shapes.append(make_socket(pos, pin_dir))

    pin_compound = Part.Compound(pin_shapes)
    neg_result = neg_half.fuse(pin_compound)

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
    """
    axis_map = {
        'x': (Vector(split_pos, 0, 0), Vector(1, 0, 0)),
        'y': (Vector(0, split_pos, 0), Vector(0, 1, 0)),
        'z': (Vector(0, 0, split_pos), Vector(0, 0, 1)),
    }
    point, normal = axis_map[axis]
    return add_registration_plane(neg_half, pos_half, point, normal,
                                  pin_count=pin_count)


# ---------------------------------------------------------------------------
# Tab/slot registration pipeline
# ---------------------------------------------------------------------------

def add_tab_registration_plane(neg_half, pos_half, plane_point, plane_normal,
                                tab_count=None):
    """
    Add tab/slot registration features on interior edges of two split halves.

    Tabs protrude from the negative half into slots cut into the
    positive half.  All registration geometry is on the interior side
    of the wall, keeping the exterior surface clean.
    """
    n = Vector(plane_normal)
    n.normalize()

    split_face = _find_split_face(neg_half, plane_point, n)
    if split_face is None:
        split_face = _find_split_face(pos_half, plane_point, n)
    if split_face is None:
        logger.warning("Could not find split face for tab placement")
        return neg_half, pos_half

    edge_classes = _classify_split_face_edges(neg_half, split_face)
    interior_edges = [(e, c) for e, c in edge_classes if c == 'interior']

    if not interior_edges:
        logger.warning("No interior edges found on split face -- "
                       "falling back to pin registration")
        return add_registration_plane(neg_half, pos_half, plane_point, n)

    interior_edges = [(e, c) for e, c in interior_edges
                      if e.Length >= TAB_WIDTH]
    total_interior_length = sum(e.Length for e, _ in interior_edges)
    logger.info(f"Found {len(interior_edges)} interior edges (>={TAB_WIDTH}mm), "
                f"total length {total_interior_length:.1f}mm")

    bb = neg_half.BoundBox
    bb_center = Vector(
        (bb.XMin + bb.XMax) / 2,
        (bb.YMin + bb.YMax) / 2,
        (bb.ZMin + bb.ZMax) / 2,
    )

    all_tab_params = []
    for edge, _ in interior_edges:
        if tab_count is not None:
            edge_count = max(1, round(tab_count * edge.Length / total_interior_length))
        else:
            edge_count = None

        tabs = _tab_positions_along_edge(edge, n, Vector(0, 0, 0), count=edge_count)

        for center, pn, _ in tabs:
            to_center = bb_center - center
            to_center = to_center - n * to_center.dot(n)
            param = edge.Curve.parameter(center)
            edge_tangent = edge.tangentAt(param)
            to_center = to_center - edge_tangent * to_center.dot(edge_tangent)
            if to_center.Length < 1e-6:
                continue
            to_center.normalize()
            wall_dir = to_center * -1

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
        slot_shapes.append(make_tab_slot(center, pn, wdir, height=th))

    tab_compound = Part.Compound(tab_shapes)
    neg_result = neg_half.fuse(tab_compound)

    slot_compound = Part.Compound(slot_shapes)
    pos_result = pos_half.cut(slot_compound)

    print(f"Added {len(all_tab_params)} registration tab/slot pairs "
          f"on {len(interior_edges)} interior edges")
    return neg_result, pos_result


# ---------------------------------------------------------------------------
# Blister registration pipeline
# ---------------------------------------------------------------------------

def add_blister_registration_plane(neg_half, pos_half, plane_point,
                                   plane_normal, blister_count=None):
    """
    Add blister + pin/socket registration on thin-walled interior edges.

    For hollow models where the wall is too thin for tabs, this adds
    cylindrical bosses (blisters) on the interior wall surface at the
    split boundary, then places pin/socket pairs on the blister faces.
    """
    n = Vector(plane_normal)
    n.normalize()

    split_face = _find_split_face(neg_half, plane_point, n)
    if split_face is None:
        split_face = _find_split_face(pos_half, plane_point, n)
    if split_face is None:
        logger.warning("Could not find split face for blister placement")
        return neg_half, pos_half

    classes = _classify_split_face_edges(neg_half, split_face)
    interior_edges = [(e, c) for e, c in classes if c == 'interior']
    if not interior_edges:
        logger.warning("No interior edges found -- cannot place blisters")
        return neg_half, pos_half

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
        if blister_count is not None:
            edge_count = max(1, round(
                blister_count * edge.Length / total_interior_length))
        else:
            edge_count = None

        positions = _blister_positions_along_edge(
            edge, n, count=edge_count)

        for center in positions:
            to_center = bb_center - center
            to_center = to_center - n * to_center.dot(n)
            param = edge.Curve.parameter(center)
            edge_tangent = edge.tangentAt(param)
            to_center = to_center - edge_tangent * to_center.dot(edge_tangent)
            if to_center.Length < 1e-6:
                continue
            to_center.normalize()
            blister_dir = to_center

            neg_b, pos_b = make_blister(center, n, blister_dir)
            neg_blisters.append(neg_b)
            pos_blisters.append(pos_b)

            pin_center = center + blister_dir * (BLISTER_RADIUS - BLISTER_OVERLAP)
            pin_shapes.append(make_pin(pin_center, n))
            socket_shapes.append(make_socket(pin_center, n))

    if not neg_blisters:
        logger.warning("No blister positions found")
        return neg_half, pos_half

    neg_compound = Part.Compound(neg_blisters + pin_shapes)
    neg_result = neg_half.fuse(neg_compound)

    pos_compound = Part.Compound(pos_blisters)
    pos_result = pos_half.fuse(pos_compound)
    sock_compound = Part.Compound(socket_shapes)
    pos_result = pos_result.cut(sock_compound)

    print(f"Added {len(pin_shapes)} blister registration pairs "
          f"on {len(interior_edges)} interior edges")
    return neg_result, pos_result
