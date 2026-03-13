"""
thin_walls.py — Generate a ThinBuilding test model (1.2mm walls, print scale).

Generates a fresh parametric building model with:
  - 32 wall panels (3x2 front/back, 5x2 left/right)
  - 1.2mm thick walls
  - Clapboard exterior detail (0.4mm deep)
  - Window openings with mullion crosses (0.6mm wide, 0.35mm deep)
  - Mullions positioned at 0.40-0.75mm from exterior (within the solid
    wall zone behind clapboard, but inside the 1.2mm wall)

This replaces the old boolean-cutting approach which destroyed mullions
because they sat at Y=2.42-2.77 in the original 4.8mm model, entirely
within the cut zone (Y=1.2 to 4.8).

Overall building dimensions: 90 x 150 x 90 mm (at print scale).

Run from FreeCAD MCP execute_python:
    exec(open('/Volumes/Files/claude/tooling/3dprinting/thin_walls.py').read())

Produces:
    - 'ThinBuilding' object in the active document (32-solid compound)

Face geometry after 4-axis print rotation (generate_building_print.py):
    - Mullion bottoms: min_edge ~0.30mm (96 faces)
    - Clapboard steps:  min_edge ~0.40mm (1440 faces)
    - Wall bases:       min_edge ~0.80mm (32 faces)
    MIN_OVERHANG_DEPTH=0.6mm cleanly separates detail from structural faces.
"""

import FreeCAD, Part
from FreeCAD import Vector

# --- Geometry parameters (all mm, print scale) ---
WALL_THICK = 1.2        # total wall thickness
CLAP_DEPTH = 0.4        # clapboard step depth on exterior
CLAP_PITCH = 1.6        # clapboard plank spacing

MULLION_WIDTH = 0.6     # mullion bar width
MULLION_DEPTH = 0.35    # mullion bar depth into wall
MULLION_START = CLAP_DEPTH                  # 0.40mm from exterior
MULLION_END = CLAP_DEPTH + MULLION_DEPTH    # 0.75mm from exterior

# Building envelope
BX, BY, BZ = 90.0, 150.0, 90.0
PANEL_W = 30.0
PANEL_H = 45.0

# Window insets from panel edges
WIN_INSET_W = 5.0       # horizontal inset from each side
WIN_INSET_BOT = 8.0     # bottom inset
WIN_INSET_TOP = 10.0    # top inset


def make_panel(origin, wall_dir, length_dir, length, height, ext_sign):
    """Build one wall panel with clapboard, window opening, and mullion cross.

    Args:
        origin: Vector — corner of the panel
        wall_dir: Vector — direction of wall thickness (X or Y, unit)
        length_dir: Vector — direction of panel length (X or Y, unit)
        length: float — panel width along length_dir
        height: float — panel height (Z)
        ext_sign: -1 if exterior is at min side, +1 if at max side
    """
    # Base wall box
    wb = Part.makeBox(
        abs(length_dir.x) * length + abs(wall_dir.x) * WALL_THICK,
        abs(length_dir.y) * length + abs(wall_dir.y) * WALL_THICK,
        height, origin)

    # Clapboard steps — cut planks from exterior face
    n_planks = int(height / CLAP_PITCH)
    for p in range(n_planks):
        z_bot = origin.z + p * CLAP_PITCH
        step_h = CLAP_PITCH * 0.75

        if wall_dir.x != 0:
            if ext_sign < 0:
                cb = Part.makeBox(CLAP_DEPTH, length + 2, step_h,
                                  Vector(origin.x - 0.01, origin.y - 1, z_bot))
            else:
                cb = Part.makeBox(CLAP_DEPTH, length + 2, step_h,
                                  Vector(origin.x + WALL_THICK - CLAP_DEPTH + 0.01,
                                         origin.y - 1, z_bot))
        else:
            if ext_sign < 0:
                cb = Part.makeBox(length + 2, CLAP_DEPTH, step_h,
                                  Vector(origin.x - 1, origin.y - 0.01, z_bot))
            else:
                cb = Part.makeBox(length + 2, CLAP_DEPTH, step_h,
                                  Vector(origin.x - 1,
                                         origin.y + WALL_THICK - CLAP_DEPTH + 0.01,
                                         z_bot))
        wb = wb.cut(cb)

    # Window opening — cut through entire wall
    win_w = length - 2 * WIN_INSET_W
    win_h = height - WIN_INSET_BOT - WIN_INSET_TOP

    if wall_dir.x != 0:
        wc = Part.makeBox(WALL_THICK + 2, win_w, win_h,
                          Vector(origin.x - 1, origin.y + WIN_INSET_W,
                                 origin.z + WIN_INSET_BOT))
    else:
        wc = Part.makeBox(win_w, WALL_THICK + 2, win_h,
                          Vector(origin.x + WIN_INSET_W, origin.y - 1,
                                 origin.z + WIN_INSET_BOT))
    wb = wb.cut(wc)

    # Mullion cross — vertical + horizontal bars inside window opening
    wcl = length / 2.0
    wcz = origin.z + WIN_INSET_BOT + win_h / 2.0

    if wall_dir.x != 0:
        mx = (origin.x + MULLION_START if ext_sign < 0
              else origin.x + WALL_THICK - MULLION_END)
        vm = Part.makeBox(MULLION_DEPTH, MULLION_WIDTH, win_h,
                          Vector(mx, origin.y + wcl - MULLION_WIDTH / 2,
                                 origin.z + WIN_INSET_BOT))
        hm = Part.makeBox(MULLION_DEPTH, win_w, MULLION_WIDTH,
                          Vector(mx, origin.y + WIN_INSET_W,
                                 wcz - MULLION_WIDTH / 2))
    else:
        my = (origin.y + MULLION_START if ext_sign < 0
              else origin.y + WALL_THICK - MULLION_END)
        vm = Part.makeBox(MULLION_WIDTH, MULLION_DEPTH, win_h,
                          Vector(origin.x + wcl - MULLION_WIDTH / 2, my,
                                 origin.z + WIN_INSET_BOT))
        hm = Part.makeBox(win_w, MULLION_DEPTH, MULLION_WIDTH,
                          Vector(origin.x + WIN_INSET_W, my,
                                 wcz - MULLION_WIDTH / 2))

    mc = vm.fuse(hm)
    return wb.fuse(mc)


# --- Build all 32 panels ---
solids = []

# Front wall (Y=0, exterior at YMin): 3 columns x 2 rows
for c in range(3):
    for r in range(2):
        solids.append(make_panel(
            Vector(c * PANEL_W, 0, r * PANEL_H),
            Vector(0, 1, 0), Vector(1, 0, 0),
            PANEL_W, PANEL_H, -1))

# Back wall (Y=BY-WALL_THICK, exterior at YMax): 3 columns x 2 rows
for c in range(3):
    for r in range(2):
        solids.append(make_panel(
            Vector(c * PANEL_W, BY - WALL_THICK, r * PANEL_H),
            Vector(0, 1, 0), Vector(1, 0, 0),
            PANEL_W, PANEL_H, +1))

# Left wall (X=0, exterior at XMin): 5 columns x 2 rows
for c in range(5):
    for r in range(2):
        solids.append(make_panel(
            Vector(0, c * PANEL_W, r * PANEL_H),
            Vector(1, 0, 0), Vector(0, 1, 0),
            PANEL_W, PANEL_H, -1))

# Right wall (X=BX-WALL_THICK, exterior at XMax): 5 columns x 2 rows
for c in range(5):
    for r in range(2):
        solids.append(make_panel(
            Vector(BX - WALL_THICK, c * PANEL_W, r * PANEL_H),
            Vector(1, 0, 0), Vector(0, 1, 0),
            PANEL_W, PANEL_H, +1))

# --- Create compound and add to document ---
compound = Part.makeCompound(solids)

doc = FreeCAD.ActiveDocument
if doc.getObject("ThinBuilding"):
    doc.removeObject("ThinBuilding")

obj = doc.addObject("Part::Feature", "ThinBuilding")
obj.Shape = compound
doc.recompute()

# --- Verify ---
n_faces = sum(len(s.Faces) for s in compound.Solids)
bb = compound.BoundBox
print(f"ThinBuilding: {len(solids)} panels, {n_faces} faces")
print(f"Bbox: {bb.XLength:.1f} x {bb.YLength:.1f} x {bb.ZLength:.1f} mm")
print(f"Wall thickness: {WALL_THICK}mm, clapboard: {CLAP_DEPTH}mm, "
      f"mullion: {MULLION_DEPTH}mm at {MULLION_START}-{MULLION_END}mm")
