"""
removeSplitter() introduces self-intersections on fused coplanar wedges

Bug: Shape.removeSplitter() (BRepBuilderAPI_MakeShapeModify / ShapeUpgrade_UnifySameDomain)
introduces BOPAlgo_SelfIntersect errors on a shape that was clean after fuse().

The geometry is two triangular prisms (wedges) sharing a face, fused with a box
that is coplanar on one face but offset so it doesn't fully cover the wedges
along the extrusion axis. The fuse result passes BOP check. After removeSplitter,
it fails with self-intersecting edges and faces.

This pattern arises naturally when modeling clapboard siding on buildings: an array
of wedge-shaped planks fused with the wall behind them. The wall is typically wider
than the clapboard area (extending to corners), creating the partial-coverage
geometry that triggers this bug.

Tested on:
  - FreeCAD 1.2.0 weekly-2026.03.25 (stock, commit 1bc5980a), OCC 7.8.1.1, macOS arm64
  - Also reproduced on FreeCAD weekly-2026.02.18 with local patches (same OCC)

Self-intersections scale linearly: 2 per wedge-wedge boundary (2 wedges = 2 SI,
3 wedges = 4 SI, 22 wedges = 42 SI).

Usage:
    exec(open('removeSplitter_self_intersection.py').read())
"""

import Part
from FreeCAD import Vector

# --- Minimal reproducer ---

# Two triangular prisms (wedges), stacked in Z, sharing a face at Z=10.
# Back face at Y=0. Profile face at X=0, extruded along +X to X=40.
p1 = Vector(0, 0, 0)
p2 = Vector(0, 1, 0)
p3 = Vector(0, 0, 10)
wire = Part.makePolygon([p1, p2, p3, p1])
face = Part.Face(wire)
wedge = face.extrude(Vector(40, 0, 0))

wedges = Part.makeCompound([
    wedge,
    wedge.translated(Vector(0, 0, 10)),
])

# Box (wall) coplanar with wedge back face at Y=0, but offset in X:
# starts at X=1, so it does NOT cover the wedge profile face at X=0.
wall = Part.makeBox(39, 5, 20, Vector(1, -5, 0))

# --- Verify inputs are clean ---

assert wall.isValid(), "Wall should be valid"
assert wedges.isValid(), "Wedges should be valid"

try:
    wall.check(True)
except Exception as e:
    raise AssertionError(f"Wall should pass BOP check: {e}")

# Note: the wedges compound fails BOP check because the two wedges share
# an exact face at Z=10. This is expected for a compound of touching solids
# and is not a problem -- the fuse resolves this boundary correctly.
for i, solid in enumerate(wedges.Solids):
    try:
        solid.check(True)
    except Exception as e:
        raise AssertionError(f"Wedge {i} should pass BOP check: {e}")

# --- Fuse is clean ---

fused = wall.fuse(wedges)
assert fused.isValid(), "Fused shape should be valid"

try:
    fused.check(True)
except Exception as e:
    raise AssertionError(f"Fused shape should pass BOP check: {e}")

print("Fuse result: valid, BOP-clean")

# --- removeSplitter introduces self-intersections ---

refined = fused.removeSplitter()

try:
    refined.check(True)
    print("removeSplitter result: BOP-clean (bug may be fixed!)")
except Exception as e:
    si_count = str(e).count("SelfIntersect")
    print(f"removeSplitter result: {si_count} self-intersections  <-- BUG")
    print()
    print("Expected: 0 self-intersections")
    print("Got:      2 self-intersections (1 edge + 1 face pair)")
    print()
    print("The self-intersections are at the Z=10 boundary where the two")
    print("wedges share a face, at the X=0..1 region where the wall does")
    print("not cover the wedges. removeSplitter appears to incorrectly")
    print("merge faces across this boundary.")

# --- Visualize in document (optional) ---

doc = FreeCAD.ActiveDocument
if doc is not None:
    for name in ("BugDemo_Fused", "BugDemo_Refined"):
        if doc.getObject(name):
            doc.removeObject(name)

    obj1 = doc.addObject("Part::Feature", "BugDemo_Fused")
    obj1.Shape = fused
    obj2 = doc.addObject("Part::Feature", "BugDemo_Refined")
    obj2.Shape = refined
    doc.recompute()
    print()
    print("Added BugDemo_Fused (clean) and BugDemo_Refined (broken) to document.")
