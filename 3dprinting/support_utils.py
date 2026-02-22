"""
Context-aware support generation utilities for resin MSLA printing.

Designed to be executed inside FreeCAD's Python environment via MCP.
Implements the pipeline described in context-aware-supports.md.

Usage (from FreeCAD MCP execute_python):
    exec(open('/Volumes/Files/claude/tooling/3dprinting/support_utils.py').read())

    # Then call pipeline functions:
    classified = classify_faces(doc.getObject("MyWall").Shape, wall_normal, window_bounds)
    contacts = generate_support_points(shape, classified, ...)
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
OVERHANG_DOT_THRESHOLD = -0.3  # normal.z below this = downward-facing overhang

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

    if abs(dot_wall) > 0.5:
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

        # Cosmetic vs structural overhang
        if area < COSMETIC_AREA_MAX:
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


def generate_bottom_supports(shape, classified, raise_amount=MODEL_RAISE):
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

    # Y positions
    y_margin = 0.5
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


# ---------------------------------------------------------------------------
# Full Pipeline
# ---------------------------------------------------------------------------

def run_support_pipeline(doc, object_name, wall_outward_normal,
                          window_bounds=None, mullion_x=None,
                          raise_amount=MODEL_RAISE):
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
                                                raise_amount)
    all_contacts.extend(bottom_contacts)

    # 4. Build supports
    if all_contacts:
        support_compound = build_supports(all_contacts)
        sup_obj = doc.addObject("Part::Feature", "Supports")
        sup_obj.Shape = support_compound

    # 5. Build raft (sized to cover all support base pads)
    raft_shape = build_raft(raised_shape, contact_points=all_contacts)
    raft_obj = doc.addObject("Part::Feature", "Raft")
    raft_obj.Shape = raft_shape

    doc.recompute()
    print("Pipeline complete!")

    return {
        'classified': classified,
        'lintel_contacts': lintel_contacts,
        'bottom_contacts': bottom_contacts,
    }
