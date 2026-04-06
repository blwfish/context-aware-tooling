"""Tests for auto_slice — automatic model slicing for build volume fitting.

Pure-math tests run anywhere; FreeCAD tests marked with @freecad.
"""

import sys
import os
import math
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from conftest import freecad, HAS_FREECAD
import auto_slice
from auto_slice import (
    compute_tilt_envelope, plan_cuts, avoid_detail_zones,
    CutSpec, SlicePlan, DetailZone, ModelAnalysis,
    PRINTER_VOLUMES, _merge_ranges, _min_cuts_for_axis,
)


# ---------------------------------------------------------------------------
# Pure-math tests: compute_tilt_envelope
# ---------------------------------------------------------------------------

class TestComputeTiltEnvelope:

    def test_zero_tilt_returns_original(self):
        dims = (100.0, 80.0, 60.0)
        result = compute_tilt_envelope(dims, tilt_x_deg=0.0, tilt_z_deg=0.0)
        assert result == pytest.approx(dims)

    def test_x_tilt_only(self):
        """18° X tilt: Y and Z change, X stays the same."""
        dims = (100.0, 80.0, 60.0)
        result = compute_tilt_envelope(dims, tilt_x_deg=18.0, tilt_z_deg=0.0)

        rx = math.radians(18.0)
        expected_x = 100.0
        expected_y = 80.0 * math.cos(rx) + 60.0 * math.sin(rx)
        expected_z = 80.0 * math.sin(rx) + 60.0 * math.cos(rx)

        assert result[0] == pytest.approx(expected_x)
        assert result[1] == pytest.approx(expected_y)
        assert result[2] == pytest.approx(expected_z)

    def test_z_tilt_only(self):
        """10° Z tilt: X and Y change, Z stays the same."""
        dims = (100.0, 80.0, 60.0)
        result = compute_tilt_envelope(dims, tilt_x_deg=0.0, tilt_z_deg=10.0)

        rz = math.radians(10.0)
        expected_x = 100.0 * math.cos(rz) + 80.0 * math.sin(rz)
        expected_y = 100.0 * math.sin(rz) + 80.0 * math.cos(rz)
        expected_z = 60.0

        assert result[0] == pytest.approx(expected_x)
        assert result[1] == pytest.approx(expected_y)
        assert result[2] == pytest.approx(expected_z)

    def test_both_tilts(self):
        """Combined X and Z tilt — verify the order (X first, then Z)."""
        dims = (100.0, 80.0, 60.0)
        result = compute_tilt_envelope(dims, tilt_x_deg=18.0, tilt_z_deg=8.0)

        # X tilt first
        rx = math.radians(18.0)
        y1 = 80.0 * math.cos(rx) + 60.0 * math.sin(rx)
        z1 = 80.0 * math.sin(rx) + 60.0 * math.cos(rx)

        # Then Z tilt on the X-tilted result
        rz = math.radians(8.0)
        x2 = 100.0 * math.cos(rz) + y1 * math.sin(rz)
        y2 = 100.0 * math.sin(rz) + y1 * math.cos(rz)

        assert result == pytest.approx((x2, y2, z1))

    def test_gordonsville_tilt(self):
        """The Gordonsville station at 18° X tilt should inflate Y."""
        dims = (185.4, 227.7, 60.6)
        result = compute_tilt_envelope(dims, tilt_x_deg=18.0, tilt_z_deg=0.0)

        # Y should be larger than original due to Z contributing
        assert result[0] == pytest.approx(185.4)  # X unchanged
        assert result[1] > 227.7  # Y inflated
        assert result[2] > 60.6   # Z inflated too

        # Check specific inflation from Z height
        rx = math.radians(18.0)
        z_contribution = 60.6 * math.sin(rx)
        assert z_contribution == pytest.approx(18.73, abs=0.1)

    def test_envelope_always_positive(self):
        dims = (50.0, 30.0, 20.0)
        for tilt_x in range(0, 90, 10):
            for tilt_z in range(0, 90, 10):
                result = compute_tilt_envelope(dims, tilt_x, tilt_z)
                assert all(d > 0 for d in result), \
                    f"Negative dim at tilt ({tilt_x}, {tilt_z}): {result}"


# ---------------------------------------------------------------------------
# Pure-math tests: plan_cuts
# ---------------------------------------------------------------------------

class TestPlanCuts:

    def test_no_cuts_needed(self):
        """Model fits printer — no cuts."""
        dims = (100.0, 80.0, 50.0)
        origin = (0.0, 0.0, 0.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=0.0)
        assert len(plan.cuts) == 0
        assert plan.piece_count == 1

    def test_y_overflow_needs_cuts(self):
        """Gordonsville-like model: Y overflows, needs splitting."""
        dims = (185.4, 227.7, 60.6)
        origin = (-92.7, -119.5, -1.0)
        bv = PRINTER_VOLUMES['m7_pro']  # 218 x 123 x 260

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=18.0)

        assert len(plan.cuts) >= 2  # at least 2 Y cuts
        assert all(c.axis == 'y' for c in plan.cuts)  # only Y cuts
        assert plan.piece_count >= 3

    def test_y_cuts_are_inside_model(self):
        """Cut positions must be within the model's Y range."""
        dims = (185.4, 227.7, 60.6)
        origin = (-92.7, -119.5, -1.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=18.0)

        y_min = origin[1]
        y_max = origin[1] + dims[1]
        for cut in plan.cuts:
            assert y_min < cut.position < y_max, \
                f"Cut at {cut.position} outside [{y_min}, {y_max}]"

    def test_cuts_evenly_spaced(self):
        """Cuts should be roughly evenly spaced."""
        dims = (100.0, 300.0, 50.0)
        origin = (0.0, 0.0, 0.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=0.0)

        y_cuts = plan.cuts_on_axis('y')
        assert len(y_cuts) >= 2
        positions = [c.position for c in y_cuts]

        # Check spacing is roughly uniform
        spacings = [positions[i+1] - positions[i]
                    for i in range(len(positions)-1)]
        if len(spacings) > 1:
            avg = sum(spacings) / len(spacings)
            for s in spacings:
                assert s == pytest.approx(avg, rel=0.1)

    def test_no_x_cuts_for_gordonsville(self):
        """X dimension fits fine — no X cuts needed."""
        dims = (185.4, 227.7, 60.6)
        origin = (-92.7, -119.5, -1.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=18.0)
        x_cuts = plan.cuts_on_axis('x')
        assert len(x_cuts) == 0

    def test_both_axes_overflow(self):
        """Wide and long model needs cuts on X and Y."""
        dims = (300.0, 300.0, 50.0)
        origin = (0.0, 0.0, 0.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=0.0)

        assert plan.cuts_on_axis('x'), "Should have X cuts"
        assert plan.cuts_on_axis('y'), "Should have Y cuts"
        assert plan.piece_count >= 4  # at least 2x2

    def test_piece_count_multiplicative(self):
        """Piece count = product of (cuts+1) per axis."""
        dims = (300.0, 300.0, 50.0)
        origin = (0.0, 0.0, 0.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan = plan_cuts(dims, origin, bv, tilt_x_deg=0.0)

        expected = 1
        for axis in ('x', 'y', 'z'):
            n = len(plan.cuts_on_axis(axis))
            expected *= (n + 1)
        assert plan.piece_count == expected

    def test_tilt_increases_cuts(self):
        """Larger tilt should require same or more cuts."""
        dims = (185.4, 227.7, 60.6)
        origin = (-92.7, -119.5, -1.0)
        bv = PRINTER_VOLUMES['m7_pro']

        plan_0 = plan_cuts(dims, origin, bv, tilt_x_deg=0.0)
        plan_18 = plan_cuts(dims, origin, bv, tilt_x_deg=18.0)

        assert len(plan_18.cuts) >= len(plan_0.cuts)


# ---------------------------------------------------------------------------
# Pure-math tests: avoid_detail_zones
# ---------------------------------------------------------------------------

class TestAvoidDetailZones:

    def test_no_zones_no_change(self):
        positions = [50.0, 100.0]
        result = avoid_detail_zones(positions, [])
        assert result == positions

    def test_nudges_away_from_zone(self):
        """A cut at 106.5 near detail zone [105, 108] should be nudged."""
        positions = [106.5]
        zones = [DetailZone(axis='y', lo=105.0, hi=108.0)]
        result = avoid_detail_zones(positions, zones, clearance=3.0)

        # Should be pushed to 102.0 or 111.0
        assert result[0] < 105.0 - 3.0 + 0.01 or result[0] > 108.0 + 3.0 - 0.01

    def test_nudge_to_nearer_edge(self):
        """Cut near the low end of a zone nudges to lo - clearance."""
        positions = [104.0]
        zones = [DetailZone(axis='y', lo=105.0, hi=110.0)]
        result = avoid_detail_zones(positions, zones, clearance=3.0)
        assert result[0] == pytest.approx(102.0)  # lo - clearance

    def test_nudge_to_far_edge(self):
        """Cut near the high end of a zone nudges to hi + clearance."""
        positions = [109.0]
        zones = [DetailZone(axis='y', lo=105.0, hi=110.0)]
        result = avoid_detail_zones(positions, zones, clearance=3.0)
        assert result[0] == pytest.approx(113.0)  # hi + clearance

    def test_no_nudge_when_clear(self):
        """Cuts already clear of zones are unchanged."""
        positions = [50.0, 150.0]
        zones = [DetailZone(axis='y', lo=105.0, hi=108.0)]
        result = avoid_detail_zones(positions, zones, clearance=3.0)
        assert result == positions

    def test_multiple_zones(self):
        """Cuts near multiple zones are each nudged independently."""
        positions = [106.0, 206.0]
        zones = [
            DetailZone(axis='y', lo=105.0, hi=108.0),
            DetailZone(axis='y', lo=205.0, hi=208.0),
        ]
        result = avoid_detail_zones(positions, zones, clearance=3.0)
        assert result[0] != 106.0
        assert result[1] != 206.0


# ---------------------------------------------------------------------------
# Pure-math tests: _merge_ranges
# ---------------------------------------------------------------------------

class TestMergeRanges:

    def test_empty(self):
        assert _merge_ranges([]) == []

    def test_single(self):
        assert _merge_ranges([(1, 5)]) == [(1, 5)]

    def test_non_overlapping(self):
        result = _merge_ranges([(1, 3), (5, 7)])
        assert result == [(1, 3), (5, 7)]

    def test_overlapping(self):
        result = _merge_ranges([(1, 5), (3, 7)])
        assert result == [(1, 7)]

    def test_gap_merge(self):
        """Ranges within gap distance are merged."""
        result = _merge_ranges([(1, 3), (5, 7)], gap=2.0)
        assert result == [(1, 7)]

    def test_unsorted_input(self):
        result = _merge_ranges([(5, 7), (1, 3)])
        assert result == [(1, 3), (5, 7)]

    def test_multiple_overlaps(self):
        result = _merge_ranges([(1, 4), (3, 6), (5, 8)])
        assert result == [(1, 8)]


# ---------------------------------------------------------------------------
# Pure-math tests: SlicePlan dataclass
# ---------------------------------------------------------------------------

class TestSlicePlan:

    def test_axes_cut(self):
        plan = SlicePlan(
            model_dims=(100, 200, 50), build_volume=(218, 123, 260),
            tilt_envelope=(100, 200, 50),
            cuts=[CutSpec('y', 66.7, 0, 2), CutSpec('y', 133.3, 1, 2)],
            piece_count=3)
        assert plan.axes_cut == {'y'}

    def test_cuts_on_axis(self):
        cuts = [CutSpec('y', 133.3, 1, 2), CutSpec('y', 66.7, 0, 2),
                CutSpec('x', 100.0, 0, 1)]
        plan = SlicePlan(
            model_dims=(200, 200, 50), build_volume=(218, 123, 260),
            tilt_envelope=(200, 200, 50), cuts=cuts, piece_count=6)

        y_cuts = plan.cuts_on_axis('y')
        assert len(y_cuts) == 2
        assert y_cuts[0].position < y_cuts[1].position  # sorted

        x_cuts = plan.cuts_on_axis('x')
        assert len(x_cuts) == 1

        z_cuts = plan.cuts_on_axis('z')
        assert len(z_cuts) == 0


# ---------------------------------------------------------------------------
# FreeCAD tests: analyze_model, find_detail_zones
# ---------------------------------------------------------------------------

@freecad
class TestAnalyzeModel:

    @pytest.fixture
    def simple_box(self):
        import Part
        from FreeCAD import Vector
        return Part.makeBox(100, 80, 50, Vector(0, 0, 0))

    @pytest.fixture
    def compound_with_details(self):
        """Main box + several tiny detail cubes clustered near Y=100."""
        import Part
        from FreeCAD import Vector
        main = Part.makeBox(100, 120, 50, Vector(0, 0, 0))
        details = []
        for i in range(5):
            d = Part.makeBox(2, 0.2, 2, Vector(10 + i*5, 100, 25))
            details.append(d)
        return Part.makeCompound([main] + details)

    def test_simple_box_analysis(self, simple_box):
        analysis = auto_slice.analyze_model(simple_box)
        assert analysis.dims == pytest.approx((100, 80, 50))
        assert analysis.solid_count == 1
        assert len(analysis.detail_zones) == 0

    def test_compound_with_details(self, compound_with_details):
        analysis = auto_slice.analyze_model(compound_with_details)
        assert analysis.solid_count == 6  # 1 main + 5 details
        assert analysis.main_solid_volume > 100 * 120 * 50 * 0.9
        assert len(analysis.detail_zones) > 0

    def test_detail_zones_locate_cluster(self, compound_with_details):
        analysis = auto_slice.analyze_model(compound_with_details)
        y_zones = [dz for dz in analysis.detail_zones if dz.axis == 'y']
        assert len(y_zones) >= 1
        # The cluster is near Y=100
        assert any(99 < dz.lo < 101 for dz in y_zones)


@freecad
class TestExecuteSlicePlan:

    @pytest.fixture
    def long_box(self):
        """A 100x250x50 box that needs Y-splitting for M7 Pro."""
        import Part
        from FreeCAD import Vector
        return Part.makeBox(100, 250, 50, Vector(0, 0, 0))

    def test_no_cuts_returns_original(self, long_box):
        plan = SlicePlan(
            model_dims=(100, 250, 50), build_volume=(218, 123, 260),
            tilt_envelope=(100, 250, 50), cuts=[], piece_count=1)
        pieces = auto_slice.execute_slice_plan(long_box, plan)
        assert len(pieces) == 1

    def test_single_y_cut(self, long_box):
        plan = SlicePlan(
            model_dims=(100, 250, 50), build_volume=(218, 123, 260),
            tilt_envelope=(100, 125, 50),
            cuts=[CutSpec('y', 125.0, 0, 1)],
            piece_count=2)
        pieces = auto_slice.execute_slice_plan(long_box, plan)
        assert len(pieces) == 2

        # Volume should be roughly preserved (minus pin/socket material)
        original_vol = long_box.Volume
        total_vol = sum(p.Volume for p in pieces)
        assert total_vol == pytest.approx(original_vol, rel=0.05)

    def test_two_y_cuts(self, long_box):
        plan = SlicePlan(
            model_dims=(100, 250, 50), build_volume=(218, 123, 260),
            tilt_envelope=(100, 83, 50),
            cuts=[CutSpec('y', 83.3, 0, 2), CutSpec('y', 166.7, 1, 2)],
            piece_count=3)
        pieces = auto_slice.execute_slice_plan(long_box, plan)
        assert len(pieces) == 3

    def test_each_piece_smaller_than_original(self, long_box):
        plan = SlicePlan(
            model_dims=(100, 250, 50), build_volume=(218, 123, 260),
            tilt_envelope=(100, 125, 50),
            cuts=[CutSpec('y', 125.0, 0, 1)],
            piece_count=2)
        pieces = auto_slice.execute_slice_plan(long_box, plan)

        for piece in pieces:
            assert piece.BoundBox.YLength < long_box.BoundBox.YLength


@freecad
class TestAutoSliceEndToEnd:

    @pytest.fixture
    def doc_with_long_box(self):
        import FreeCAD
        import Part
        from FreeCAD import Vector
        doc = FreeCAD.newDocument("TestAutoSlice")
        obj = doc.addObject("Part::Feature", "LongModel")
        obj.Shape = Part.makeBox(100, 250, 50, Vector(0, 0, 0))
        doc.recompute()
        yield doc
        FreeCAD.closeDocument("TestAutoSlice")

    def test_auto_slice_creates_pieces(self, doc_with_long_box):
        doc = doc_with_long_box
        result = auto_slice.auto_slice("LongModel", printer='m7_pro',
                                        tilt_x_deg=0.0, doc=doc)
        assert len(result['pieces']) >= 2
        assert len(result['plan'].cuts) >= 1

    def test_auto_slice_provenance(self, doc_with_long_box):
        doc = doc_with_long_box
        result = auto_slice.auto_slice("LongModel", printer='m7_pro',
                                        tilt_x_deg=0.0, doc=doc)
        for obj in result['objects']:
            assert obj.GeneratorName == "print_pipeline"
            assert obj.SourceObject == "LongModel"
            assert "auto_slice" in obj.PipelineSteps

    def test_auto_slice_no_split_model(self):
        """A small model that fits shouldn't be split."""
        import FreeCAD
        import Part
        from FreeCAD import Vector
        doc = FreeCAD.newDocument("TestNoSplit")
        try:
            obj = doc.addObject("Part::Feature", "SmallModel")
            obj.Shape = Part.makeBox(50, 50, 30, Vector(0, 0, 0))
            doc.recompute()

            result = auto_slice.auto_slice("SmallModel", printer='m7_pro',
                                            doc=doc)
            assert len(result['pieces']) == 1
            assert len(result['plan'].cuts) == 0
        finally:
            FreeCAD.closeDocument("TestNoSplit")
