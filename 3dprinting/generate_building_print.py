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
# Threshold -0.5 catches wall-base faces (n.z ≈ -0.95 after tilts) but
# not the display/brick face (n.z ≈ -0.31) or vertical faces.
THRESH = -0.5
GRID   = 8.0      # mm — cluster spacing
raw_contacts = []

for face in s5.Faces:
    if face.Area < 1.5:
        continue
    try:
        n = face.normalAt(0.5, 0.5)
    except Exception:
        continue
    if n.z > THRESH:
        continue
    if abs(n.z) < 0.05:   # skip vertical faces
        continue

    com = face.CenterOfMass
    fbb = face.BoundBox

    # For dimensions smaller than the grid, use face center to stay on the face.
    # For larger dimensions, distribute at GRID intervals.
    if fbb.XLength < GRID:
        xs = [com.x]
    else:
        xs = [fbb.XMin + GRID/2 + i*GRID for i in range(int(fbb.XLength / GRID) + 1)]

    if fbb.YLength < GRID:
        ys = [com.y]
    else:
        ys = [fbb.YMin + GRID/2 + i*GRID for i in range(int(fbb.YLength / GRID) + 1)]

    for x in xs:
        for y in ys:
            if not (fbb.XMin - 0.5 <= x <= fbb.XMax + 0.5 and
                    fbb.YMin - 0.5 <= y <= fbb.YMax + 0.5):
                continue
            # Z from face plane equation: exact Z at this (x, y) for tilted face
            z = com.z - (n.x / n.z) * (x - com.x) - (n.y / n.z) * (y - com.y)
            z = max(z, MODEL_RAISE + 0.1)
            raw_contacts.append((x, y, z))

print(f"Raw contacts: {len(raw_contacts)}")

# Cluster to GRID cells — deduplicate, keep minimum-Z contact.
# IMPORTANT: keep original (x, y, z) coordinates, NOT the grid cell center.
# Using the cell center would place supports off the face, giving wrong Z.
cells = {}
for (x, y, z) in raw_contacts:
    key = (round(x / GRID) * GRID, round(y / GRID) * GRID)
    if key not in cells or z < cells[key][2]:
        cells[key] = (x, y, z)

# Clip to model bbox — prevents clustering from pushing contacts outside
contacts = [
    (max(model_bb.XMin, min(model_bb.XMax, cx)),
     max(model_bb.YMin, min(model_bb.YMax, cy)),
     cz)
    for (cx, cy, cz) in cells.values()
]
print(f"Clustered contacts: {len(contacts)}")

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
