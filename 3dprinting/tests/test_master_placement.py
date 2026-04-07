"""Tests for master_placement module.

Two tiers:
  - Pure-math tests: matching logic, no FreeCAD needed
  - FreeCAD tests: opening detection, placement, marked with @freecad
"""

import sys
import os
import pytest

# Ensure 3dprinting package is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from conftest import freecad, HAS_FREECAD
from master_placement import (
    Opening, MasterInfo, PlacementResult,
    match_master, match_all,
    PERPENDICULAR_TOL, DEFAULT_MAX_MARGIN, DEFAULT_MIN_MARGIN,
)


# ---------------------------------------------------------------------------
# Helpers for pure-math tests
# ---------------------------------------------------------------------------

def _opening(width, height, cx=50, cy=1, cz=25,
             nx=0, ny=1, nz=0, ext_offset=2.0, thickness=2.0):
    return Opening(
        center_x=cx, center_y=cy, center_z=cz,
        width=width, height=height, wall_thickness=thickness,
        normal_x=nx, normal_y=ny, normal_z=nz,
        exterior_offset=ext_offset, wall_label="TestWall",
    )


def _master(label, width, height, depth=1.5, depth_axis=1):
    return MasterInfo(
        label=label, width=width, height=height,
        depth=depth, depth_axis=depth_axis,
        bbox_min=(0, 0, 0),
        bbox_max=(width, depth, height) if depth_axis == 1
        else (depth, width, height) if depth_axis == 0
        else (width, height, depth),
    )


# ---------------------------------------------------------------------------
# Pure-math tests: matching
# ---------------------------------------------------------------------------

class TestMatchMaster:
    def test_exact_fit_within_margin(self):
        opening = _opening(20, 30)
        catalog = [_master("win_a", 19.5, 29.5)]
        result = match_master(opening, catalog)
        assert result is not None
        assert result.label == "win_a"

    def test_rejects_oversized_master(self):
        opening = _opening(20, 30)
        catalog = [_master("too_big", 21, 31)]
        result = match_master(opening, catalog)
        assert result is None

    def test_rejects_too_small_master(self):
        opening = _opening(20, 30)
        catalog = [_master("too_small", 10, 15)]
        result = match_master(opening, catalog, max_margin=1.0)
        assert result is None

    def test_selects_best_fit(self):
        opening = _opening(20, 30)
        catalog = [
            _master("loose", 19.0, 29.0),   # gap 1.0 + 1.0 = 2.0
            _master("tight", 19.8, 29.8),   # gap 0.2 + 0.2 = 0.4
            _master("medium", 19.5, 29.5),  # gap 0.5 + 0.5 = 1.0
        ]
        result = match_master(opening, catalog)
        assert result.label == "tight"

    def test_respects_min_margin(self):
        opening = _opening(20, 30)
        # Master only 0.01mm smaller — below default min_margin of 0.05
        catalog = [_master("flush", 19.99, 29.99)]
        result = match_master(opening, catalog)
        assert result is None

    def test_custom_margins(self):
        opening = _opening(20, 30)
        catalog = [_master("win", 18.0, 28.0)]
        # Default max_margin=1.0 rejects 2.0mm gap
        assert match_master(opening, catalog) is None
        # With higher max_margin, it passes
        assert match_master(opening, catalog, max_margin=3.0) is not None

    def test_match_all_returns_pairs(self):
        openings = [_opening(20, 30), _opening(15, 25)]
        catalog = [
            _master("win_20x30", 19.5, 29.5),
            _master("win_15x25", 14.5, 24.5),
        ]
        matches = match_all(openings, catalog)
        assert len(matches) == 2
        labels = {m.label for _, m in matches}
        assert labels == {"win_20x30", "win_15x25"}

    def test_match_all_skips_unmatched(self):
        openings = [_opening(20, 30), _opening(50, 60)]
        catalog = [_master("win_20x30", 19.5, 29.5)]
        matches = match_all(openings, catalog)
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# FreeCAD tests: opening detection, placement
# ---------------------------------------------------------------------------

if HAS_FREECAD:
    import FreeCAD
    import Part
    from FreeCAD import Vector
    from master_placement import (
        detect_wall_normal, find_openings, catalog_masters,
        compute_placement, place_master,
    )


@freecad
class TestDetectWallNormal:
    def test_simple_box_y_normal(self):
        """A thin box along Y should have normal along Y."""
        wall = Part.makeBox(100, 2, 50)
        normal, offset = detect_wall_normal(wall)
        # Normal should be along Y (either +Y or -Y)
        assert abs(abs(normal[1]) - 1.0) < 0.01
        # The other components should be ~0
        assert abs(normal[0]) < 0.01
        assert abs(normal[2]) < 0.01

    def test_simple_box_x_normal(self):
        """A thin box along X should have normal along X."""
        wall = Part.makeBox(2, 100, 50)
        normal, offset = detect_wall_normal(wall)
        assert abs(abs(normal[0]) - 1.0) < 0.01

    def test_exterior_offset_is_larger(self):
        """Exterior offset should be at the far face."""
        wall = Part.makeBox(100, 2, 50)
        normal, offset = detect_wall_normal(wall)
        # Wall spans Y=0 to Y=2. Exterior is at Y=2 (larger offset).
        if normal[1] > 0:
            assert abs(offset - 2.0) < 0.1
        else:
            assert abs(offset - 0.0) < 0.1


@freecad
class TestFindOpenings:
    def _wall_with_hole(self, hole_w=20, hole_h=30, hole_x=40, hole_z=10):
        """Wall box 100x2x50 with a rectangular hole."""
        wall = Part.makeBox(100, 2, 50)
        # Oversized along Y to cut clean through
        hole = Part.makeBox(hole_w, 4, hole_h, Vector(hole_x, -1, hole_z))
        return wall.cut(hole)

    def test_single_opening(self):
        shape = self._wall_with_hole()
        normal, _ = detect_wall_normal(shape)
        openings = find_openings(shape, normal)
        assert len(openings) == 1
        op = openings[0]
        assert abs(op.width - 20) < 1.0
        assert abs(op.height - 30) < 1.0

    def test_two_openings(self):
        wall = Part.makeBox(100, 2, 50)
        hole1 = Part.makeBox(15, 4, 20, Vector(10, -1, 15))
        hole2 = Part.makeBox(15, 4, 20, Vector(60, -1, 15))
        shape = wall.cut(hole1).cut(hole2)
        normal, _ = detect_wall_normal(shape)
        openings = find_openings(shape, normal)
        assert len(openings) == 2

    def test_opening_center_position(self):
        shape = self._wall_with_hole(hole_w=20, hole_h=30, hole_x=40, hole_z=10)
        normal, _ = detect_wall_normal(shape)
        openings = find_openings(shape, normal)
        assert len(openings) == 1
        op = openings[0]
        # Center should be near (50, 1, 25)
        assert abs(op.center_x - 50) < 2.0
        assert abs(op.center_z - 25) < 2.0

    def test_no_openings_in_solid_wall(self):
        wall = Part.makeBox(100, 2, 50)
        normal, _ = detect_wall_normal(wall)
        openings = find_openings(wall, normal)
        assert len(openings) == 0


@freecad
class TestCatalogMasters:
    def test_depth_detection(self, freecad_doc):
        doc = freecad_doc
        grp = doc.addObject("App::DocumentObjectGroup", "Imported Masters")
        grp.Label = "Imported Masters"

        # Window master: 19 wide x 1.5 deep x 29 tall
        win = doc.addObject("Part::Feature", "Window_2x3")
        win.Shape = Part.makeBox(19, 1.5, 29)
        grp.addObject(win)
        doc.recompute()

        catalog = catalog_masters(doc, ["Imported Masters"])
        assert len(catalog) == 1
        m = catalog[0]
        assert m.depth_axis == 1  # Y is thinnest
        assert abs(m.depth - 1.5) < 0.01
        assert abs(m.width - 19) < 0.01
        assert abs(m.height - 29) < 0.01

    def test_explicit_depth_axis(self, freecad_doc):
        doc = freecad_doc
        grp = doc.addObject("App::DocumentObjectGroup", "Imported Masters")
        grp.Label = "Imported Masters"

        # Cube-ish master where auto-detect might be ambiguous
        win = doc.addObject("Part::Feature", "DoorMaster")
        win.Shape = Part.makeBox(10, 11, 12)
        # Override depth axis to X
        win.addProperty("App::PropertyString", "MasterDepthAxis", "Metadata")
        win.MasterDepthAxis = "X"
        grp.addObject(win)
        doc.recompute()

        catalog = catalog_masters(doc, ["Imported Masters"])
        assert catalog[0].depth_axis == 0


@freecad
class TestComputePlacement:
    def test_back_face_flush_with_exterior(self):
        """Placed master's back should be at wall exterior."""
        opening = _opening(20, 30, cx=50, cy=2, cz=25,
                           nx=0, ny=1, nz=0, ext_offset=2.0, thickness=2.0)
        master = _master("win", 19, 29, depth=1.5, depth_axis=1)

        placement = compute_placement(opening, master)

        # Create the master shape and apply placement
        master_shape = Part.makeBox(19, 1.5, 29)
        master_shape.Placement = placement
        bb = master_shape.BoundBox

        # Back face (max Y) should be near exterior_offset (2.0)
        assert abs(bb.YMax - 2.0) < 0.5

    def test_centered_in_opening(self):
        """Placed master should be centered on the opening."""
        opening = _opening(20, 30, cx=50, cy=1, cz=25,
                           nx=0, ny=1, nz=0, ext_offset=2.0)
        master = _master("win", 18, 28, depth=1.5, depth_axis=1)

        placement = compute_placement(opening, master)

        master_shape = Part.makeBox(18, 1.5, 28)
        master_shape.Placement = placement
        bb = master_shape.BoundBox

        placed_cx = (bb.XMin + bb.XMax) / 2
        placed_cz = (bb.ZMin + bb.ZMax) / 2
        assert abs(placed_cx - 50) < 1.0
        assert abs(placed_cz - 25) < 1.0


@freecad
class TestPlaceMaster:
    def test_separate_mode(self, freecad_doc):
        doc = freecad_doc

        # Create wall with opening
        wall_obj = doc.addObject("Part::Feature", "Wall")
        wall = Part.makeBox(100, 2, 50)
        hole = Part.makeBox(20, 4, 30, Vector(40, -1, 10))
        wall_obj.Shape = wall.cut(hole)

        # Create master group
        grp = doc.addObject("App::DocumentObjectGroup", "Imported Masters")
        grp.Label = "Imported Masters"
        win = doc.addObject("Part::Feature", "WindowMaster")
        win.Shape = Part.makeBox(19, 1.5, 29)
        grp.addObject(win)
        doc.recompute()

        opening = _opening(20, 30, cx=50, cy=1, cz=25,
                           nx=0, ny=1, nz=0, ext_offset=2.0,
                           thickness=2.0)
        opening.wall_label = "Wall"
        master_info = MasterInfo(
            label="WindowMaster", width=19, height=29,
            depth=1.5, depth_axis=1,
            bbox_min=(0, 0, 0), bbox_max=(19, 1.5, 29),
        )

        result = place_master(doc, opening, master_info, mode='separate')
        assert result.mode == 'separate'
        assert result.placed_label != ""
        # Check that a new object was created
        placed = doc.getObject(result.placed_label)
        assert placed is not None

    def test_fuse_mode(self, freecad_doc):
        doc = freecad_doc

        wall_obj = doc.addObject("Part::Feature", "Wall")
        wall = Part.makeBox(100, 2, 50)
        hole = Part.makeBox(20, 4, 30, Vector(40, -1, 10))
        wall_obj.Shape = wall.cut(hole)
        original_volume = wall_obj.Shape.Volume

        # Create master group
        grp = doc.addObject("App::DocumentObjectGroup", "Imported Masters")
        grp.Label = "Imported Masters"
        win = doc.addObject("Part::Feature", "WindowMaster")
        win.Shape = Part.makeBox(19, 1.5, 29)
        grp.addObject(win)
        doc.recompute()

        opening = _opening(20, 30, cx=50, cy=1, cz=25,
                           nx=0, ny=1, nz=0, ext_offset=2.0,
                           thickness=2.0)
        opening.wall_label = "Wall"
        master_info = MasterInfo(
            label="WindowMaster", width=19, height=29,
            depth=1.5, depth_axis=1,
            bbox_min=(0, 0, 0), bbox_max=(19, 1.5, 29),
        )

        result = place_master(doc, opening, master_info, mode='fuse')
        assert result.mode == 'fuse'
        # Fused wall should have more volume than before
        assert wall_obj.Shape.Volume > original_volume
