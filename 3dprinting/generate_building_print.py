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
# THRESH = -0.5: main pass catches wall-base faces (n.z ≈ -0.95 after tilts)
#   but NOT display/clapboard face (n.z ≈ -0.31) or vertical wall faces.
#
# EDGE_CLEAR: minimum Y distance between support column axis and face edge.
#   Prevents column (radius COLUMN_RADIUS=0.7mm from support_utils) from
#   overlapping adjacent wall faces. With 0.3mm air gap: 0.7 + 0.3 = 1.0mm.
#   For faces shallower than 2×EDGE_CLEAR (~1.14mm wall bases after tilt),
#   the column axis is clamped to face center — there is no fully safe position,
#   but centering minimizes intrusion on both sides.
#
# TOP_THRESH = -0.20: second pass catches shallower downward-facing faces in
#   the upper portion of the model (interior wall faces near wall tops, which
#   have n.z ≈ -0.31 after 18° tilt — too shallow for THRESH=-0.5 but still
#   need support to prevent upper-edge warping during peel).
#
THRESH      = -0.5
GRID        = 8.0    # mm — cluster spacing
EDGE_CLEAR  = COLUMN_RADIUS + 0.3   # 1.0mm clearance from face edge to column axis

raw_contacts = []


def _collect_contacts(face, raw_contacts):
    """
    Compute and append contact points for one downward-facing face.

    Y positions are clamped by EDGE_CLEAR so the support column (radius
    COLUMN_RADIUS) doesn't overlap the face's adjacent wall surfaces.
    Points outside the face bounding box are discarded.
    """
    if face.Area < 0.5:
        return
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception:
        return
    if abs(n.z) < 0.05:       # skip near-vertical faces
        return

    com = face.CenterOfMass
    fbb = face.BoundBox

    # X positions
    if fbb.XLength < GRID:
        xs = [com.x]
    else:
        xs = [fbb.XMin + GRID/2 + i*GRID
              for i in range(int(fbb.XLength / GRID) + 1)]

    # Y positions with EDGE_CLEAR clamping.
    # safe_y_min/max is the zone where the column fits without overlapping
    # the face's YMin (display side) or YMax (interior wall side).
    safe_y_min = fbb.YMin + EDGE_CLEAR
    safe_y_max = fbb.YMax - EDGE_CLEAR

    if safe_y_min > safe_y_max:
        # Face too shallow for full clearance on both sides.
        # Best we can do: center the column; it will intrude slightly on both.
        ys = [com.y]
    elif fbb.YLength < GRID:
        # Face narrower than grid: one contact, clamped to safe zone.
        ys = [max(safe_y_min, min(safe_y_max, com.y))]
    else:
        raw_ys = [fbb.YMin + GRID/2 + i*GRID
                  for i in range(int(fbb.YLength / GRID) + 1)]
        # Clamp each grid position to safe zone and deduplicate.
        ys = list(dict.fromkeys(
            max(safe_y_min, min(safe_y_max, y)) for y in raw_ys
        ))

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
# These are the bottom edges of all four walls; n.z ≈ -0.95 after 18° tilt.
for face in s5.Faces:
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception:
        continue
    if n.z > THRESH:
        continue
    _collect_contacts(face, raw_contacts)

print(f"Raw contacts after main pass: {len(raw_contacts)}")

# -- Top-zone pass: shallower overhangs near the wall tops --------------------
# After 18° X-tilt the interior faces of wall tops have n.z ≈ -0.31, which
# is above THRESH=-0.5 and so missed by the main pass.  These faces are at
# the highest Z values of the model; without support the wall edge furthest
# from the raft warps during peel.
#
# Filter: n.z in (-0.50, -0.20] AND face center in top 30% of model height.
# This catches interior/back wall faces near the top while excluding:
#   - display face (n.z ≈ +0.31, above TOP_THRESH)
#   - interior face of display wall (n.z ≈ +0.31, above TOP_THRESH)
#   - main-pass faces already handled (n.z ≤ THRESH = -0.50)
#
TOP_THRESH   = -0.20
Z_TOP_FRAC   = 0.70    # consider faces whose center is in the top 30% of height
z_top_cutoff = model_bb.ZMin + Z_TOP_FRAC * model_bb.ZLength

for face in s5.Faces:
    if face.CenterOfMass.z < z_top_cutoff:
        continue
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception:
        continue
    if n.z > TOP_THRESH:
        continue           # upward-facing or near-horizontal → skip
    if n.z <= THRESH:
        continue           # already caught by main pass → skip
    _collect_contacts(face, raw_contacts)

print(f"Raw contacts after top-zone pass: {len(raw_contacts)}")

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

# Filter contacts too close to the display face (Y ≈ 0 after translation).
# A column (radius COLUMN_RADIUS) centred at cy < DISPLAY_CLEARANCE would
# extend into negative Y and physically intersect the clapboard surface.
DISPLAY_CLEARANCE = COLUMN_RADIUS + 0.3   # 1.0mm from model YMin
n_before = len(contacts)
contacts = [(cx, cy, cz) for cx, cy, cz in contacts
            if cy >= model_bb.YMin + DISPLAY_CLEARANCE]
print(f"Clustered contacts: {len(contacts)} "
      f"({n_before - len(contacts)} dropped by display-clearance filter)")

# ---------------------------------------------------------------------------
# Build supports and raft
# ---------------------------------------------------------------------------
supports = build_supports(contacts, raft_top_z=0.0)

# Size raft from model footprint only (not contact pads) so raft_Y = model_Y + 4mm.
# All support base pads (radius 1.5mm) stay within the 2mm raft margin.
raft = build_raft(s5, contact_points=None)
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
