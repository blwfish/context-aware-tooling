"""
generate_building_print.py — SecondaryBuilding print preparation

Generates a print-ready STL from the SecondaryBuilding.FCStd model with:
  - 1.2mm thin walls (from ThinBuilding object already in the document)
  - 4-axis orientation: 90° Z reorient + 18° X-tilt + 5° Y longitudinal tilt + 2° Z diagonal tilt
  - Context-aware supports on all four wall bases (not on display/brick faces)
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

import FreeCAD, Part, math, sys, os
sys.path.insert(0, '/Volumes/Files/claude/tooling/3dprinting')
exec(open('/Volumes/Files/claude/tooling/3dprinting/support_utils.py').read())

doc = FreeCAD.ActiveDocument
thin = doc.getObject("ThinBuilding")
if thin is None:
    raise ValueError("ThinBuilding object not found — run wall-thinning step first")

# ---------------------------------------------------------------------------
# Four-axis orientation
# ---------------------------------------------------------------------------
# 1. 90° CCW Z-rotation (reorient: long axis along X, short axis along Y)
mat_z = FreeCAD.Matrix()
mat_z.A11 =  0; mat_z.A12 = 1; mat_z.A14 = 0
mat_z.A21 = -1; mat_z.A22 = 0; mat_z.A24 = 90
mat_z.A33 =  1
s1 = thin.Shape.copy(); s1.transformShape(mat_z)

# 2. 18° X-tilt (lean back, interior side faces build plate)
tx = math.radians(18.0)
mat_x = FreeCAD.Matrix()
mat_x.A22 = math.cos(tx); mat_x.A23 = -math.sin(tx)
mat_x.A32 = math.sin(tx); mat_x.A33 =  math.cos(tx)
s2 = s1.copy(); s2.transformShape(mat_x)

# 3. 5° Y-tilt (longitudinal: one end lower than other → diagonal bottom edge,
#    progressive peel across building length rather than simultaneous)
ty = math.radians(5.0)
mat_y = FreeCAD.Matrix()
mat_y.A11 = math.cos(ty); mat_y.A13 = math.sin(ty)
mat_y.A22 = 1.0
mat_y.A31 = -math.sin(ty); mat_y.A33 = math.cos(ty)
s3 = s2.copy(); s3.transformShape(mat_y)

# 4. 2° Z-tilt (slight plan-view diagonal for peel sweep)
tz = math.radians(2.0)
mat_zr = FreeCAD.Matrix()
mat_zr.A11 = math.cos(tz); mat_zr.A12 = -math.sin(tz)
mat_zr.A21 = math.sin(tz); mat_zr.A22 =  math.cos(tz)
s4 = s3.copy(); s4.transformShape(mat_zr)

# Translate: XMin/YMin → 0, ZMin → MODEL_RAISE (raise off raft)
bb = s4.BoundBox
mat_sh = FreeCAD.Matrix()
mat_sh.A14 = -bb.XMin; mat_sh.A24 = -bb.YMin; mat_sh.A34 = -bb.ZMin + MODEL_RAISE
s5 = s4.copy(); s5.transformShape(mat_sh)
model_bb = s5.BoundBox
print(f"Footprint: {model_bb.XLength:.1f} x {model_bb.YLength:.1f} mm, "
      f"height {model_bb.ZLength:.1f} mm")

# ---------------------------------------------------------------------------
# Support contact generation
# ---------------------------------------------------------------------------
#
# Face selection uses TWO filters to isolate wall-base faces:
#
# 1. THRESH = -0.5: normal Z threshold catches strongly downward-facing faces
#    (n.z ≈ -0.95 after tilts).  This includes wall bases AND clapboard plank
#    step faces (both were originally horizontal).
#
# 2. MIN_OVERHANG_DEPTH = 0.6mm: minimum shortest-edge length.  Wall bases
#    have min_edge ≈ 0.80mm (1.2mm wall after boolean thinning, projected
#    through rotations).  Clapboard plank steps have min_edge ≈ 0.40mm.
#    Threshold at 0.6mm cleanly separates them (0.2mm margin each side)
#    without relying on position (which fails for a 4-walled building
#    where exterior faces exist at all Y positions, not just YMax).
#
# EDGE_CLEAR: minimum distance between support column axis and face edge.
#   Prevents column (radius COLUMN_RADIUS=0.7mm) from overlapping adjacent
#   wall faces.  INTERIOR_EDGE_CLEAR is tight (column just fits inside the
#   face).  EXTERIOR_EDGE_CLEAR is generous (keeps column well away from
#   the display surface).
#
#   For 1.2mm walls after 18° tilt, the wall thickness projects to ~1.14mm
#   in Y (side walls) or ~1.2mm in X (front/back walls).  A column at
#   EDGE_CLEAR=1.0mm from the exterior edge would penetrate the exterior
#   surface.  Asymmetric clearance fixes this: large on exterior side,
#   small on interior side.
#
THRESH              = -0.5
MIN_OVERHANG_DEPTH  = 0.6    # mm — shortest edge; rejects clapboard steps (0.40mm), keeps wall bases (0.80mm)
GRID                = 8.0    # mm — cluster spacing
INTERIOR_EDGE_CLEAR = COLUMN_RADIUS + 0.3   # 1.0mm from interior edge
EXTERIOR_EDGE_CLEAR = COLUMN_RADIUS + 1.5   # 2.2mm from exterior edge (clears 1.2mm wall)

# Narrow face threshold: faces thinner than this in one axis get special
# handling — contact biased toward interior so column only overshoots
# on the invisible interior side.
# After 4-axis tilt, left/right wall bases have y_span ≈ 1.81mm.
# The clapboard of the adjacent panel overhangs the face edge, so even
# though the column (1.4mm dia) nominally fits, it intersects the
# overhanging clapboard.  Threshold must exceed 1.81mm.
NARROW_FACE_THRESH  = 2.0   # mm

raw_contacts = []


def _collect_contacts(face, raw_contacts, model_center_y, model_center_x):
    """
    Compute and append contact points for one downward-facing face.

    Filters out clapboard/detail faces using MIN_OVERHANG_DEPTH (shortest
    edge).  Contact positions use asymmetric clearance: generous on the
    exterior side (to avoid penetrating display surfaces), tight on the
    interior side.

    Handles two face orientations:
    - Narrow-in-Y (left/right walls): thin dimension is Y, long axis is X.
      Asymmetric clearance in Y.
    - Narrow-in-X (front/back walls): thin dimension is X, long axis is Y.
      Contact X biased toward interior so column only overshoots on
      interior side.  Column radius (0.7mm) > half face width (0.5mm),
      so some interior overshoot is unavoidable but invisible.
    """
    if face.Area < 0.5:
        return
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception:
        return
    if abs(n.z) < 0.05:       # skip near-vertical faces
        return

    # Reject clapboard plank steps and other thin detail faces.
    # Wall bases have min_edge ≈ 0.80mm (1.2mm walls); clapboard steps ≈ 0.40mm.
    min_edge = min(e.Length for e in face.Edges) if face.Edges else 0
    if min_edge < MIN_OVERHANG_DEPTH:
        return

    com = face.CenterOfMass
    fbb = face.BoundBox

    # Detect face orientation: is the narrow axis X or Y?
    narrow_in_x = fbb.XLength < NARROW_FACE_THRESH and fbb.YLength > NARROW_FACE_THRESH

    if narrow_in_x:
        # --- NARROW-IN-X face (front/back wall base) ---
        # Thin dimension is X (~0.99mm).  Column (1.4mm dia) can't fit.
        # Bias contact X toward interior so column only overshoots interior side.
        ext_at_xmax = (com.x > model_center_x)
        if ext_at_xmax:
            # Exterior at XMax → push contact toward XMin (interior)
            # Column extends from (contact_x - R) to (contact_x + R)
            # Prevent exterior overshoot: contact_x + R <= fbb.XMax
            cx = fbb.XMax - COLUMN_RADIUS
        else:
            # Exterior at XMin → push contact toward XMax (interior)
            # Prevent exterior overshoot: contact_x - R >= fbb.XMin
            cx = fbb.XMin + COLUMN_RADIUS
        xs = [cx]

        # Y positions: face is long in Y, use grid spacing with Y clearance
        # For front/back walls, determine exterior Y side for clearance.
        ext_at_ymax = (com.y > model_center_y)
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
        ext_at_ymax = (com.y > model_center_y)

        if narrow_in_y:
            # Thin dimension is Y (~1.40mm).  Column (1.4mm dia) barely fits.
            # Bias contact Y toward interior so column only overshoots interior.
            if ext_at_ymax:
                # Exterior at YMax → push contact toward YMin (interior)
                cy = fbb.YMax - COLUMN_RADIUS
            else:
                # Exterior at YMin → push contact toward YMax (interior)
                cy = fbb.YMin + COLUMN_RADIUS
            ys = [cy]
        else:
            # Wide face: use asymmetric clearance in Y.
            if ext_at_ymax:
                safe_y_min = fbb.YMin + INTERIOR_EDGE_CLEAR
                safe_y_max = fbb.YMax - EXTERIOR_EDGE_CLEAR
            else:
                safe_y_min = fbb.YMin + EXTERIOR_EDGE_CLEAR
                safe_y_max = fbb.YMax - INTERIOR_EDGE_CLEAR

            # Y positions
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

        # X positions
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
            # Z from face plane equation: exact height at (x,y) on this face.
            z = com.z - (n.x / n.z) * (x - com.x) - (n.y / n.z) * (y - com.y)
            z = max(z, MODEL_RAISE + 0.1)
            raw_contacts.append((x, y, z))


# -- Main pass: wall-base faces (strongly downward-facing, n.z < -0.5) -------
# After 18° X-tilt, both wall bases and clapboard steps have n.z ≈ -0.95.
# The MIN_OVERHANG_DEPTH filter inside _collect_contacts separates them:
# wall bases have min_edge ≈ 0.80mm, clapboard steps ≈ 0.3–0.4mm.
center_y = (model_bb.YMin + model_bb.YMax) / 2.0
center_x = (model_bb.XMin + model_bb.XMax) / 2.0
for face in s5.Faces:
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception:
        continue
    if n.z > THRESH:
        continue
    _collect_contacts(face, raw_contacts, center_y, center_x)

print(f"Raw contacts: {len(raw_contacts)}")

# Cluster to GRID cells — deduplicate, keep minimum-Z contact.
# IMPORTANT: keep original (x, y, z) coordinates, NOT the grid cell center.
# Using the cell center would place supports off the face, giving wrong Z.
cells = {}
for (x, y, z) in raw_contacts:
    key = (round(x / GRID) * GRID, round(y / GRID) * GRID)
    if key not in cells or z < cells[key][2]:
        cells[key] = (x, y, z)

# Clip to model bbox — prevents clustering from pushing contacts outside.
contacts = [
    (max(model_bb.XMin, min(model_bb.XMax, cx)),
     max(model_bb.YMin, min(model_bb.YMax, cy)),
     cz)
    for (cx, cy, cz) in cells.values()
]

print(f"Clustered contacts: {len(contacts)}")

# ---------------------------------------------------------------------------
# Collision check: find per-contact base_z (raft or model-resting)
# ---------------------------------------------------------------------------
# For a tilted 4-walled building, support columns for far-wall contacts
# are very tall and pass through near-wall panels.  For each contact,
# check if the vertical column from z=0 to z=cz intersects the model.
# If blocked, start the support from the TOP of the intersection
# ("support-on-model") instead of from the raft.
#
# Uses distToShape for fast pre-screening, then boolean intersection
# only for blocked contacts to find exact ZMax.

MODEL_REST_GAP = 0.3   # mm gap between model surface and support base

# Pre-compute per-panel bounding boxes for fast XY overlap tests.
# Only panels whose XY footprint overlaps the column need boolean checks.
_panels = list(s5.Solids)
_panel_bbs = [p.BoundBox for p in _panels]

def _find_support_base(cx, cy, cz, col_radius):
    """Return base_z for a support column. 0.0 = raft, >0 = model surface."""
    # Check each panel whose bbox overlaps the column in XY and Z
    max_z_hit = -1.0
    margin = col_radius + 0.5
    for i, pbb in enumerate(_panel_bbs):
        # Fast bbox reject: no XY overlap
        if (cx + margin < pbb.XMin or cx - margin > pbb.XMax or
            cy + margin < pbb.YMin or cy - margin > pbb.YMax):
            continue
        # No Z overlap in the check region (below cz - 3mm)
        if pbb.ZMin > cz - 3.0 or pbb.ZMax < 0.1:
            continue
        # This panel's bbox overlaps — do precise distance check
        line_z1 = min(cz - 3.0, pbb.ZMax)
        line_z0 = max(0.1, pbb.ZMin)
        if line_z1 <= line_z0 + 0.1:
            continue
        line = Part.makeLine(Vector(cx, cy, line_z0),
                             Vector(cx, cy, line_z1))
        try:
            dist, _pts, _info = _panels[i].distToShape(line)
        except Exception:
            continue
        if dist > col_radius + 0.1:
            continue
        # Blocked by this panel. Find exact ZMax via boolean.
        # Only check the region BELOW the contact (cz - 3mm).
        # Intersections near cz are the contact's OWN panel — the column
        # always intersects its own wall near the cone tip transition.
        # We only care about CROSS-PANEL collisions lower down.
        col_z0 = max(0.0, pbb.ZMin - 1.0)
        col_z1 = min(cz - 3.0, pbb.ZMax + 1.0)
        if col_z1 <= col_z0 + 0.5:
            continue  # panel is entirely near the contact, skip
        col = Part.makeCylinder(col_radius, col_z1 - col_z0,
                                Vector(cx, cy, col_z0), Vector(0, 0, 1))
        try:
            intersection = _panels[i].common(col)
            if intersection.Volume > 0.001:
                max_z_hit = max(max_z_hit, intersection.BoundBox.ZMax)
        except Exception:
            continue

    if max_z_hit < 0:
        return 0.0  # clear path to raft
    base_z = max_z_hit + MODEL_REST_GAP
    # Sanity: base must be well below contact
    if base_z > cz - TIP_HEIGHT - 1.0:
        return -1.0  # can't fit a support, skip this contact
    return base_z


print(f"Checking {len(contacts)} support paths for model collisions "
      f"({len(_panels)} panels)...")
raft_contacts = []       # (cx, cy, cz) — normal raft-based supports
model_contacts = []      # (cx, cy, cz, base_z) — model-resting supports
skipped = 0

for (cx, cy, cz) in contacts:
    base_z = _find_support_base(cx, cy, cz, COLUMN_RADIUS)
    if base_z < 0:
        skipped += 1
    elif base_z < 0.1:
        raft_contacts.append((cx, cy, cz))
    else:
        model_contacts.append((cx, cy, cz, base_z))

print(f"  Raft-based: {len(raft_contacts)}, model-resting: {len(model_contacts)}, "
      f"skipped: {skipped}")

# ---------------------------------------------------------------------------
# Build supports and raft
# ---------------------------------------------------------------------------
# Raft-based supports (normal)
all_support_shapes = []
for (cx, cy, cz) in raft_contacts:
    all_support_shapes.extend(build_tapered_support(cx, cy, cz, raft_top_z=0.0))

# Model-resting supports (no base pad — column starts from model surface)
for (cx, cy, cz, base_z) in model_contacts:
    # Small adhesion pad on model surface (thinner than raft pad)
    pad = Part.makeCylinder(BASE_PAD_RADIUS, 0.4,
                            Vector(cx, cy, base_z), Vector(0, 0, 1))
    all_support_shapes.append(pad)
    col_bot = base_z + 0.4
    col_top = cz - TIP_HEIGHT
    if col_top > col_bot:
        col = Part.makeCylinder(COLUMN_RADIUS, col_top - col_bot,
                                Vector(cx, cy, col_bot), Vector(0, 0, 1))
        all_support_shapes.append(col)
    else:
        col_top = col_bot
    cone = Part.makeCone(COLUMN_RADIUS, TIP_RADIUS, TIP_HEIGHT,
                         Vector(cx, cy, col_top), Vector(0, 0, 1))
    all_support_shapes.append(cone)

supports = Part.Compound(all_support_shapes)
print(f"Built {len(raft_contacts) + len(model_contacts)} supports "
      f"({len(all_support_shapes)} shapes)")

# Size raft from model footprint + raft-based support pads.
# Model-resting supports don't touch the raft, so excluded.
raft = build_raft(s5, contact_points=raft_contacts if raft_contacts else None)
raft_bb = raft.BoundBox
print(f"Raft: {raft_bb.XLength:.1f} x {raft_bb.YLength:.1f} mm "
      f"(M7 Pro limit: 218 x 123 mm)")

check_build_fit(Part.makeCompound([s5, supports, raft]), printer='m7_pro', margin=0)

all_shapes = [s5] + list(supports.Solids) + [raft]
final = Part.makeCompound(all_shapes)

# Update FreeCAD document
for name in ("BuildingPrint", "BuildingPrintSupported"):
    if doc.getObject(name):
        doc.removeObject(name)
out = doc.addObject("Part::Feature", "BuildingPrintSupported")
out.Shape = final
doc.recompute()

# Export STL
import MeshPart
mesh = MeshPart.meshFromShape(Shape=final, LinearDeflection=0.05, AngularDeflection=0.3)
OUT = "/Volumes/Files/claude/tooling/3dprinting/models/SecondaryBuilding_print.stl"
mesh.write(OUT)
print(f"Exported: {mesh.CountFacets:,} facets, {os.path.getsize(OUT)//1024} KB")
print(f"  -> {OUT}")
