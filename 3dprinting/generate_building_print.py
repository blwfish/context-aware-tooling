"""
generate_building_print.py — SecondaryBuilding print preparation

Generates a print-ready STL from the SecondaryBuilding.FCStd model with:
  - 1.2mm thin walls (from ThinBuilding object already in the document)
  - 4-axis orientation: 90 Z reorient + 18 X-tilt + 5 Y longitudinal tilt + 2 Z diagonal tilt
  - Context-aware supports on all four wall bases (not on display/brick faces)
  - Collision detection: supports rest on intervening panels when path is blocked
  - Angled neck approach: columns displaced toward interior, necks sweep to contact
  - Panel clipping: removes support protrusions through thin walls
  - Raft sized to model footprint + margin

Run from FreeCAD MCP execute_python or execute_python_async.

Usage:
    exec(open('/Volumes/Files/claude/tooling/3dprinting/generate_building_print.py').read())

Output:
    /Volumes/Files/claude/tooling/3dprinting/models/SecondaryBuilding_print.stl

Requires:
    - FreeCAD document with 'ThinBuilding' object (run wall-thinning first)
    - support_utils.py in same directory

Printer: AnyCubic M7 Pro (218 x 123 x 260 mm build volume)
"""

import FreeCAD, Part, math, sys, os, logging
sys.path.insert(0, '/Volumes/Files/claude/tooling/3dprinting')
from support_utils import (
    Contact, build_tapered_support, build_raft, check_build_fit,
    MODEL_RAISE, COLUMN_RADIUS, TIP_RADIUS, NECK_HEIGHT,
    BASE_PAD_RADIUS, BASE_PAD_HEIGHT,
)

logger = logging.getLogger(__name__)

from FreeCAD import Vector

# ---------------------------------------------------------------------------
# Building-specific constants
# ---------------------------------------------------------------------------
# Face selection uses TWO filters to isolate wall-base faces:
#
# 1. THRESH = -0.5: normal Z threshold catches strongly downward-facing faces
#    (n.z ~ -0.95 after tilts).  This includes wall bases AND clapboard plank
#    step faces (both were originally horizontal).
#
# 2. MIN_OVERHANG_DEPTH = 0.6mm: minimum shortest-edge length.  Wall bases
#    have min_edge ~ 0.80mm (1.2mm wall after boolean thinning, projected
#    through rotations).  Clapboard plank steps have min_edge ~ 0.40mm.
#    Threshold at 0.6mm cleanly separates them (0.2mm margin each side).
#
# EDGE_CLEAR: minimum distance between support column axis and face edge.
# INTERIOR_EDGE_CLEAR is tight; EXTERIOR_EDGE_CLEAR keeps column away from
# display surface.
#
THRESH              = -0.5
MIN_OVERHANG_DEPTH  = 0.6    # mm -- rejects clapboard steps (0.40mm), keeps wall bases (0.80mm)
GRID                = 8.0    # mm -- cluster spacing
INTERIOR_EDGE_CLEAR = COLUMN_RADIUS + 0.3   # 1.0mm from interior edge
EXTERIOR_EDGE_CLEAR = COLUMN_RADIUS + 1.5   # 2.2mm from exterior edge (clears 1.2mm wall)

NARROW_FACE_THRESH  = 2.0    # mm -- faces thinner than this get interior-biased placement
NARROW_EXT_BIAS     = COLUMN_RADIUS + TIP_RADIUS + 0.1  # 1.2mm from exterior edge

INWARD_NUDGE        = 0.3    # mm shift toward building interior
MODEL_REST_GAP      = 0.3    # mm gap between model surface and support base

EXT_SLAB_DEPTH      = 3.0    # mm outward from panel exterior surface
EXT_SLAB_MARGIN     = 1.0    # mm extra coverage in the non-clip directions

STL_OUTPUT = "/Volumes/Files/claude/tooling/3dprinting/models/SecondaryBuilding_print.stl"


# ---------------------------------------------------------------------------
# Pipeline functions
# ---------------------------------------------------------------------------

def orient_model(shape):
    """Apply 4-axis print orientation to the building shape.

    Rotation sequence:
      1. 90 CCW Z-rotation (long axis along X, short axis along Y)
      2. 18 X-tilt (lean back, interior toward build plate)
      3. 5 Y-tilt (progressive peel across building length)
      4. 2 Z-tilt (diagonal peel sweep)

    Then shift to XMin/YMin=0, ZMin=MODEL_RAISE.

    Returns (oriented_shape, bounding_box).
    """
    # 1. 90 CCW Z-rotation
    mat_z = FreeCAD.Matrix()
    mat_z.A11 = 0;  mat_z.A12 = 1; mat_z.A14 = 0
    mat_z.A21 = -1; mat_z.A22 = 0; mat_z.A24 = 90
    mat_z.A33 = 1
    s1 = shape.copy(); s1.transformShape(mat_z)

    # 2. 18 X-tilt
    tx = math.radians(18.0)
    mat_x = FreeCAD.Matrix()
    mat_x.A22 = math.cos(tx); mat_x.A23 = -math.sin(tx)
    mat_x.A32 = math.sin(tx); mat_x.A33 =  math.cos(tx)
    s2 = s1.copy(); s2.transformShape(mat_x)

    # 3. 5 Y-tilt
    ty = math.radians(5.0)
    mat_y = FreeCAD.Matrix()
    mat_y.A11 = math.cos(ty); mat_y.A13 = math.sin(ty)
    mat_y.A22 = 1.0
    mat_y.A31 = -math.sin(ty); mat_y.A33 = math.cos(ty)
    s3 = s2.copy(); s3.transformShape(mat_y)

    # 4. 2 Z-tilt
    tz = math.radians(2.0)
    mat_zr = FreeCAD.Matrix()
    mat_zr.A11 = math.cos(tz); mat_zr.A12 = -math.sin(tz)
    mat_zr.A21 = math.sin(tz); mat_zr.A22 =  math.cos(tz)
    s4 = s3.copy(); s4.transformShape(mat_zr)

    # Shift to all-positive coords, raised off raft
    bb = s4.BoundBox
    mat_sh = FreeCAD.Matrix()
    mat_sh.A14 = -bb.XMin; mat_sh.A24 = -bb.YMin
    mat_sh.A34 = -bb.ZMin + MODEL_RAISE
    s5 = s4.copy(); s5.transformShape(mat_sh)
    model_bb = s5.BoundBox
    print(f"Footprint: {model_bb.XLength:.1f} x {model_bb.YLength:.1f} mm, "
          f"height {model_bb.ZLength:.1f} mm")
    return s5, model_bb


def _collect_face_contacts(face, center_y, center_x):
    """Compute contact points for one downward-facing face.

    Filters out clapboard/detail faces using MIN_OVERHANG_DEPTH (shortest
    edge).  Contact positions use asymmetric clearance: generous on the
    exterior side, tight on the interior side.

    Returns list of (x, y, z, nx, ny, nz) tuples (raw, pre-clustering).
    """
    if face.Area < 0.5:
        return []
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception as e:
        logger.debug("normalAt failed: %s", e)
        return []
    if abs(n.z) < 0.05:
        return []

    min_edge = min(e.Length for e in face.Edges) if face.Edges else 0
    if min_edge < MIN_OVERHANG_DEPTH:
        return []

    com = face.CenterOfMass
    fbb = face.BoundBox
    results = []

    # Detect face orientation: is the narrow axis X or Y?
    narrow_in_x = fbb.XLength < NARROW_FACE_THRESH and fbb.YLength > NARROW_FACE_THRESH

    if narrow_in_x:
        # --- NARROW-IN-X face (front/back wall base) ---
        ext_at_xmax = (com.x > center_x)
        if ext_at_xmax:
            cx = fbb.XMax - NARROW_EXT_BIAS
        else:
            cx = fbb.XMin + NARROW_EXT_BIAS
        xs = [cx]

        ext_at_ymax = (com.y > center_y)
        if ext_at_ymax:
            safe_y_min = fbb.YMin + INTERIOR_EDGE_CLEAR
            safe_y_max = fbb.YMax - EXTERIOR_EDGE_CLEAR
        else:
            safe_y_min = fbb.YMin + EXTERIOR_EDGE_CLEAR
            safe_y_max = fbb.YMax - INTERIOR_EDGE_CLEAR

        if safe_y_min > safe_y_max:
            if ext_at_ymax:
                ys = [fbb.YMin + INTERIOR_EDGE_CLEAR]
            else:
                ys = [fbb.YMax - INTERIOR_EDGE_CLEAR]
        elif fbb.YLength < GRID:
            ys = [max(safe_y_min, min(safe_y_max, com.y))]
        else:
            raw_ys = [fbb.YMin + GRID/2 + i*GRID
                      for i in range(int(fbb.YLength / GRID) + 1)]
            ys = list(dict.fromkeys(
                max(safe_y_min, min(safe_y_max, y)) for y in raw_ys
            ))
    else:
        # --- NARROW-IN-Y face (left/right wall base) or wide face ---
        narrow_in_y = fbb.YLength < NARROW_FACE_THRESH and fbb.XLength > NARROW_FACE_THRESH
        ext_at_ymax = (com.y > center_y)

        if narrow_in_y:
            if ext_at_ymax:
                cy = fbb.YMax - NARROW_EXT_BIAS
            else:
                cy = fbb.YMin + NARROW_EXT_BIAS
            ys = [cy]
        else:
            if ext_at_ymax:
                safe_y_min = fbb.YMin + INTERIOR_EDGE_CLEAR
                safe_y_max = fbb.YMax - EXTERIOR_EDGE_CLEAR
            else:
                safe_y_min = fbb.YMin + EXTERIOR_EDGE_CLEAR
                safe_y_max = fbb.YMax - INTERIOR_EDGE_CLEAR

            if safe_y_min > safe_y_max:
                if ext_at_ymax:
                    ys = [fbb.YMin + INTERIOR_EDGE_CLEAR]
                else:
                    ys = [fbb.YMax - INTERIOR_EDGE_CLEAR]
            elif fbb.YLength < GRID:
                ys = [max(safe_y_min, min(safe_y_max, com.y))]
            else:
                raw_ys = [fbb.YMin + GRID/2 + i*GRID
                          for i in range(int(fbb.YLength / GRID) + 1)]
                ys = list(dict.fromkeys(
                    max(safe_y_min, min(safe_y_max, y)) for y in raw_ys
                ))

        if fbb.XLength < GRID:
            xs = [com.x]
        else:
            xs = [fbb.XMin + GRID/2 + i*GRID
                  for i in range(int(fbb.XLength / GRID) + 1)]

    for x in xs:
        for y in ys:
            if not (fbb.XMin - 0.5 <= x <= fbb.XMax + 0.5 and
                    fbb.YMin - 0.5 <= y <= fbb.YMax + 0.5):
                continue
            z = com.z - (n.x / n.z) * (x - com.x) - (n.y / n.z) * (y - com.y)
            z = max(z, MODEL_RAISE + 0.1)
            results.append((x, y, z, n.x, n.y, n.z))

    return results


def collect_contacts(shape, model_bb):
    """Generate all support contacts from wall-base faces.

    Scans all faces with n.z < THRESH, filters by MIN_OVERHANG_DEPTH,
    clusters to GRID cells, nudges toward building center.

    Returns list of Contact objects.
    """
    center_y = (model_bb.YMin + model_bb.YMax) / 2.0
    center_x = (model_bb.XMin + model_bb.XMax) / 2.0

    # Collect raw contacts from all downward-facing structural faces
    raw_contacts = []
    for face in shape.Faces:
        try:
            n = face.normalAt(0.5, 0.5)
        except Exception as e:
            logger.debug("normalAt failed for bottom face: %s", e)
            continue
        if n.z > THRESH:
            continue
        raw_contacts.extend(_collect_face_contacts(face, center_y, center_x))

    print(f"Raw contacts: {len(raw_contacts)}")

    # Cluster to GRID cells -- deduplicate, keep minimum-Z contact
    cells = {}
    for (x, y, z, nx, ny, nz) in raw_contacts:
        key = (round(x / GRID) * GRID, round(y / GRID) * GRID)
        if key not in cells or z < cells[key][2]:
            cells[key] = (x, y, z, nx, ny, nz)

    # Clip to model bbox and convert to Contact objects
    contacts = [
        Contact(x=max(model_bb.XMin, min(model_bb.XMax, cx)),
                y=max(model_bb.YMin, min(model_bb.YMax, cy)),
                z=cz, nx=nx, ny=ny, nz=nz)
        for (cx, cy, cz, nx, ny, nz) in cells.values()
    ]

    # Nudge contacts toward model center
    for c in contacts:
        dx = c.x - center_x
        dy = c.y - center_y
        d = math.sqrt(dx*dx + dy*dy)
        if d > 0.01:
            c.x -= INWARD_NUDGE * dx / d
            c.y -= INWARD_NUDGE * dy / d

    print(f"Clustered contacts: {len(contacts)} (nudged {INWARD_NUDGE}mm inward)")
    return contacts


def _find_support_base(cx, cy, cz, col_radius, panels, panel_bbs):
    """Return base_z for a support column. 0.0 = raft, >0 = model surface, <0 = skip."""
    max_z_hit = -1.0
    margin = col_radius + 0.5
    for i, pbb in enumerate(panel_bbs):
        if (cx + margin < pbb.XMin or cx - margin > pbb.XMax or
            cy + margin < pbb.YMin or cy - margin > pbb.YMax):
            continue
        if pbb.ZMin > cz - 3.0 or pbb.ZMax < 0.1:
            continue
        line_z1 = min(cz - 3.0, pbb.ZMax)
        line_z0 = max(0.1, pbb.ZMin)
        if line_z1 <= line_z0 + 0.1:
            continue
        line = Part.makeLine(Vector(cx, cy, line_z0),
                             Vector(cx, cy, line_z1))
        try:
            dist, _pts, _info = panels[i].distToShape(line)
        except Exception as e:
            logger.warning("distToShape failed for panel %d: %s", i, e)
            continue
        if dist > col_radius + 0.1:
            continue
        col_z0 = max(0.0, pbb.ZMin - 1.0)
        col_z1 = min(cz - 3.0, pbb.ZMax + 1.0)
        if col_z1 <= col_z0 + 0.5:
            continue
        col = Part.makeCylinder(col_radius, col_z1 - col_z0,
                                Vector(cx, cy, col_z0), Vector(0, 0, 1))
        try:
            intersection = panels[i].common(col)
            if intersection.Volume > 0.001:
                max_z_hit = max(max_z_hit, intersection.BoundBox.ZMax)
        except Exception as e:
            logger.warning("Boolean intersection failed for panel %d: %s", i, e)
            continue

    if max_z_hit < 0:
        return 0.0
    base_z = max_z_hit + MODEL_REST_GAP
    min_neck_base_z = cz - (TIP_RADIUS + NECK_HEIGHT) * 0.95 - 1.0
    if base_z > min_neck_base_z:
        return -1.0
    return base_z


def detect_collisions(contacts, panels, panel_bbs):
    """Classify contacts as raft-based or model-resting via collision detection.

    For each contact, checks if a vertical column from z=0 to z=cz would
    intersect any panel.  If blocked, sets contact.base_z to the top of the
    intersection (model-resting).

    Returns (raft_contacts, model_contacts, skipped_count).
    """
    print(f"Checking {len(contacts)} support paths for model collisions "
          f"({len(panels)} panels)...")
    raft_contacts = []
    model_contacts = []
    skipped = 0

    for c in contacts:
        base_z = _find_support_base(c.x, c.y, c.z, COLUMN_RADIUS,
                                    panels, panel_bbs)
        if base_z < 0:
            skipped += 1
        elif base_z < 0.1:
            raft_contacts.append(c)
        else:
            c.base_z = base_z
            model_contacts.append(c)

    print(f"  Raft-based: {len(raft_contacts)}, model-resting: {len(model_contacts)}, "
          f"skipped: {skipped}")
    return raft_contacts, model_contacts, skipped


def build_all_supports(raft_contacts, model_contacts):
    """Build support geometry for all contacts.

    Returns list of Part.Shape (individual support pieces, not compounded).
    """
    all_shapes = []

    # Raft-based supports
    for c in raft_contacts:
        all_shapes.extend(build_tapered_support(c, raft_top_z=0.0))

    # Model-resting supports (no base pad)
    for c in model_contacts:
        all_shapes.extend(build_tapered_support(c, include_base_pad=False))

    print(f"Built {len(raft_contacts) + len(model_contacts)} supports "
          f"({len(all_shapes)} shapes)")
    return all_shapes


def _build_exterior_slabs(panels, panel_bbs, model_bb):
    """Create exterior slab boxes for each panel.

    Each slab covers the region just beyond the panel's exterior surface,
    used for clipping support protrusions through thin walls.

    Returns (slabs, slab_bbs) lists parallel to panels.
    """
    model_cx = (model_bb.XMin + model_bb.XMax) / 2
    model_cy = (model_bb.YMin + model_bb.YMax) / 2
    slabs = []
    slab_bbs = []
    d = EXT_SLAB_DEPTH
    m = EXT_SLAB_MARGIN

    for j, panel in enumerate(panels):
        pcom = panel.CenterOfMass
        pbb = panel_bbs[j]
        dx = pcom.x - model_cx
        dy = pcom.y - model_cy

        if abs(dx) > abs(dy):
            if dx > 0:
                slab = Part.makeBox(d, pbb.YLength + 2*m, pbb.ZLength + 2*m,
                                    Vector(pbb.XMax - 0.1, pbb.YMin - m, pbb.ZMin - m))
            else:
                slab = Part.makeBox(d, pbb.YLength + 2*m, pbb.ZLength + 2*m,
                                    Vector(pbb.XMin - d + 0.1, pbb.YMin - m, pbb.ZMin - m))
        else:
            if dy > 0:
                slab = Part.makeBox(pbb.XLength + 2*m, d, pbb.ZLength + 2*m,
                                    Vector(pbb.XMin - m, pbb.YMax - 0.1, pbb.ZMin - m))
            else:
                slab = Part.makeBox(pbb.XLength + 2*m, d, pbb.ZLength + 2*m,
                                    Vector(pbb.XMin - m, pbb.YMin - d + 0.1, pbb.ZMin - m))

        slabs.append(slab)
        slab_bbs.append(slab.BoundBox)

    return slabs, slab_bbs


def clip_supports(support_shapes, panels, panel_bbs, model_bb):
    """Clip support shapes against building panels + exterior slabs.

    For each support that intersects a panel, sequentially cuts the panel
    (removes wall intersection) then an exterior slab (removes protrusion).

    Returns list of (possibly clipped) Part.Shape.
    """
    ext_slabs, _slab_bbs = _build_exterior_slabs(panels, panel_bbs, model_bb)

    clipped_shapes = []
    clip_count = 0
    for sup in support_shapes:
        result = sup
        clipped = False
        for j, pbb in enumerate(panel_bbs):
            rbb = result.BoundBox
            if (rbb.XMax < pbb.XMin - 0.5 or rbb.XMin > pbb.XMax + 0.5 or
                rbb.YMax < pbb.YMin - 0.5 or rbb.YMin > pbb.YMax + 0.5 or
                rbb.ZMax < pbb.ZMin - 0.5 or rbb.ZMin > pbb.ZMax + 0.5):
                continue
            try:
                inter = panels[j].common(result)
                if inter.Volume < 0.001:
                    continue
                r2 = result.cut(panels[j])
                r3 = r2.cut(ext_slabs[j])
                if r3.Volume > 0.01:
                    result = r3
                    clipped = True
            except Exception as e:
                logger.warning("Clip failed for panel %d: %s", j, e)
        if clipped:
            clip_count += 1
        clipped_shapes.append(result)

    print(f"Clipped {clip_count} support shapes (panel + exterior slab)")
    return clipped_shapes


def build_print_raft(shape, raft_contacts):
    """Build raft sized to model footprint + displaced support pad positions.

    Pads are at the neck-base XY (displaced from contact position along
    the face normal toward building interior).

    Returns Part.Shape.
    """
    pad_contacts = []
    for c in raft_contacts:
        fnx, fny, fnz = c.face_normal
        fn_len = math.sqrt(fnx*fnx + fny*fny + fnz*fnz)
        if fn_len > 0.01:
            fnx, fny, fnz = fnx/fn_len, fny/fn_len, fnz/fn_len
        else:
            fnx, fny, fnz = 0, 0, -1
        pad_x = c.x + TIP_RADIUS * fnx - NECK_HEIGHT * fnx
        pad_y = c.y + TIP_RADIUS * fny - NECK_HEIGHT * fny
        pad_contacts.append(Contact(x=pad_x, y=pad_y, z=c.z))

    raft = build_raft(shape, contacts=pad_contacts if pad_contacts else None,
                      margin=1.0)
    raft_bb = raft.BoundBox
    print(f"Raft: {raft_bb.XLength:.1f} x {raft_bb.YLength:.1f} mm "
          f"(M7 Pro limit: 218 x 123 mm)")
    return raft


def export_print(doc, model, supports_compound, raft):
    """Update FreeCAD document and export STL.

    Returns the export file path.
    """
    all_shapes = [model] + list(supports_compound.Solids) + [raft]
    final = Part.makeCompound(all_shapes)

    check_build_fit(Part.makeCompound([model, supports_compound, raft]),
                    printer='m7_pro', margin=0)

    for name in ("BuildingPrint", "BuildingPrintSupported"):
        if doc.getObject(name):
            doc.removeObject(name)
    out = doc.addObject("Part::Feature", "BuildingPrintSupported")
    out.Shape = final
    doc.recompute()

    import MeshPart
    mesh = MeshPart.meshFromShape(Shape=final, LinearDeflection=0.05,
                                  AngularDeflection=0.3)
    mesh.write(STL_OUTPUT)
    print(f"Exported: {mesh.CountFacets:,} facets, "
          f"{os.path.getsize(STL_OUTPUT)//1024} KB")
    print(f"  -> {STL_OUTPUT}")
    return STL_OUTPUT


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    """Run the full SecondaryBuilding print preparation pipeline."""
    doc = FreeCAD.ActiveDocument
    thin = doc.getObject("ThinBuilding")
    if thin is None:
        raise ValueError("ThinBuilding object not found -- run wall-thinning step first")

    # 1. Orient model for printing
    model, model_bb = orient_model(thin.Shape)

    # 2. Generate support contacts from wall-base faces
    contacts = collect_contacts(model, model_bb)

    # 3. Check for cross-panel collisions
    panels = list(model.Solids)
    panel_bbs = [p.BoundBox for p in panels]
    raft_contacts, model_contacts, _skipped = detect_collisions(
        contacts, panels, panel_bbs)

    # 4. Build support geometry
    support_shapes = build_all_supports(raft_contacts, model_contacts)

    # 5. Clip supports against building panels
    clipped = clip_supports(support_shapes, panels, panel_bbs, model_bb)
    supports = Part.Compound(clipped)

    # 6. Build raft
    raft = build_print_raft(model, raft_contacts)

    # 7. Export
    export_print(doc, model, supports, raft)


main()
