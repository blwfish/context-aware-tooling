"""Tests for export_utils — mix of pure-Python and FreeCAD tests."""

import sys
import os
import pytest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import conftest markers
from conftest import freecad


# ---------------------------------------------------------------------------
# Pure-Python tests (no FreeCAD needed)
# ---------------------------------------------------------------------------

class TestConstants:
    """Verify constants are sensible."""

    def test_deflection_defaults(self):
        # Mock FreeCAD minimally for import
        import types
        _fc = types.ModuleType('FreeCAD')

        class _V:
            def __init__(self, x=0, y=0, z=0):
                self.x, self.y, self.z = x, y, z
            def normalize(self): pass
            def cross(self, o): return _V()
            def dot(self, o): return 0.0
            def __sub__(self, o): return _V()
            def __add__(self, o): return _V()
            def __mul__(self, s): return _V()
            def __rmul__(self, s): return _V()
            @property
            def Length(self): return 0.0

        _fc.Vector = _V
        _fc.Rotation = lambda *a, **kw: None
        _fc.Matrix = type('M', (), {'__init__': lambda s: None})
        _fc.Placement = lambda *a: None
        sys.modules.setdefault('FreeCAD', _fc)

        _part = types.ModuleType('Part')
        sys.modules.setdefault('Part', _part)

        from export_utils import (DEFAULT_LINEAR_DEFLECTION,
                                  DEFAULT_ANGULAR_DEFLECTION,
                                  DEFAULT_ORIGIN_MARGIN,
                                  PRINTER_VOLUMES)

        assert 0.01 <= DEFAULT_LINEAR_DEFLECTION <= 0.2
        assert 0.1 <= DEFAULT_ANGULAR_DEFLECTION <= 1.0
        assert DEFAULT_ORIGIN_MARGIN >= 0
        assert 'm7_pro' in PRINTER_VOLUMES
        assert 'm7_max' in PRINTER_VOLUMES

    def test_printer_volumes_reasonable(self):
        from export_utils import PRINTER_VOLUMES

        for name, (x, y, z) in PRINTER_VOLUMES.items():
            assert x > 100, f"{name} X too small"
            assert y > 100, f"{name} Y too small"
            assert z > 200, f"{name} Z too small"
            assert x < 500, f"{name} X too large"

    def test_check_fits_printer_unknown(self):
        from export_utils import check_fits_printer

        # Mock a shape with a BoundBox
        class MockBB:
            XLength = 50
            YLength = 50
            ZLength = 50

        class MockShape:
            BoundBox = MockBB()

        with pytest.raises(ValueError, match="Unknown printer"):
            check_fits_printer(MockShape(), printer='nonexistent')


# ---------------------------------------------------------------------------
# FreeCAD tests
# ---------------------------------------------------------------------------

@freecad
class TestShiftToPositive:
    """Test coordinate shifting — requires FreeCAD."""

    def test_negative_coords_shifted(self):
        import Part
        from export_utils import shift_to_positive

        # Box at (-10, -20, -5) to (10, 0, 15)
        box = Part.makeBox(20, 20, 20, FreeCAD.Vector(-10, -20, -5))
        shifted = shift_to_positive(box, margin=0.5)

        bb = shifted.BoundBox
        assert bb.XMin >= 0.4  # margin=0.5, allow tolerance
        assert bb.YMin >= 0.4
        assert bb.ZMin >= 0.4

    def test_already_positive_no_change(self):
        import Part
        from export_utils import shift_to_positive

        box = Part.makeBox(10, 10, 10, FreeCAD.Vector(1, 1, 1))
        shifted = shift_to_positive(box, margin=0.5)

        bb = shifted.BoundBox
        # Should be unchanged (already positive)
        assert abs(bb.XMin - 1.0) < 0.01
        assert abs(bb.YMin - 1.0) < 0.01

    def test_shift_preserves_dimensions(self):
        import Part
        from export_utils import shift_to_positive

        box = Part.makeBox(30, 40, 50, FreeCAD.Vector(-100, -200, -300))
        shifted = shift_to_positive(box, margin=1.0)

        bb = shifted.BoundBox
        assert abs(bb.XLength - 30) < 0.01
        assert abs(bb.YLength - 40) < 0.01
        assert abs(bb.ZLength - 50) < 0.01

    def test_zero_margin(self):
        import Part
        from export_utils import shift_to_positive

        box = Part.makeBox(10, 10, 10, FreeCAD.Vector(-5, -5, -5))
        shifted = shift_to_positive(box, margin=0.0)

        bb = shifted.BoundBox
        assert abs(bb.XMin) < 0.01
        assert abs(bb.YMin) < 0.01
        assert abs(bb.ZMin) < 0.01


@freecad
class TestCheckFitsPrinter:
    """Test build volume checking — requires FreeCAD for real shapes."""

    def test_small_shape_fits(self):
        import Part
        from export_utils import check_fits_printer

        box = Part.makeBox(50, 50, 50)
        assert check_fits_printer(box, 'm7_pro') is True

    def test_oversized_shape_doesnt_fit(self):
        import Part
        from export_utils import check_fits_printer

        box = Part.makeBox(300, 300, 300)
        assert check_fits_printer(box, 'm7_pro') is False

    def test_fits_max_not_pro(self):
        import Part
        from export_utils import check_fits_printer

        # 250 x 150 x 250 — fits m7_max but not m7_pro
        box = Part.makeBox(250, 150, 250)
        assert check_fits_printer(box, 'm7_pro') is False
        assert check_fits_printer(box, 'm7_max') is True


@freecad
class TestExportSTL:
    """Test full STL export pipeline — requires FreeCAD."""

    def test_export_single_shape(self):
        import Part
        from export_utils import export_stl

        box = Part.makeBox(10, 10, 10, FreeCAD.Vector(-5, -5, 0))

        with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as f:
            filepath = f.name

        try:
            result = export_stl(box, filepath)
            assert os.path.exists(filepath)
            assert result['facets'] > 0
            assert result['file_size_kb'] >= 0
            assert result['shifted_by'] is not None  # was at negative X,Y
            assert result['filepath'] == os.path.abspath(filepath)
        finally:
            os.unlink(filepath)

    def test_export_multiple_shapes(self):
        import Part
        from export_utils import export_stl

        box1 = Part.makeBox(10, 10, 10, FreeCAD.Vector(0, 0, 0))
        box2 = Part.makeBox(5, 5, 5, FreeCAD.Vector(15, 0, 0))

        with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as f:
            filepath = f.name

        try:
            result = export_stl([box1, box2], filepath)
            assert result['facets'] > 0
            # Should have more facets than a single box
            result_single = export_stl(box1, filepath)
            assert result['facets'] > result_single['facets']
        finally:
            os.unlink(filepath)

    def test_export_with_printer_check(self):
        import Part
        from export_utils import export_stl

        box = Part.makeBox(10, 10, 10)

        with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as f:
            filepath = f.name

        try:
            result = export_stl(box, filepath, printer='m7_pro')
            assert result['fits_printer'] is True
        finally:
            os.unlink(filepath)

    def test_export_no_shift(self):
        import Part
        from export_utils import export_stl

        box = Part.makeBox(10, 10, 10, FreeCAD.Vector(-5, -5, 0))

        with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as f:
            filepath = f.name

        try:
            result = export_stl(box, filepath, shift=False)
            assert result['shifted_by'] is None
        finally:
            os.unlink(filepath)

    def test_export_with_validation(self):
        import Part
        from export_utils import export_stl

        box = Part.makeBox(10, 10, 10)

        with tempfile.NamedTemporaryFile(suffix='.stl', delete=False) as f:
            filepath = f.name

        try:
            result = export_stl(box, filepath, validate=True)
            assert result['validation'] is not None
            assert result['validation']['facets'] > 0
            assert result['validation']['valid'] is True
        finally:
            os.unlink(filepath)

    def test_export_empty_raises(self):
        from export_utils import export_stl

        with pytest.raises(ValueError, match="No shapes"):
            export_stl([], '/tmp/empty.stl')


@freecad
class TestExportPieces:
    """Test multi-piece export — requires FreeCAD."""

    def test_export_three_pieces(self):
        import Part
        from export_utils import export_pieces

        pieces = [
            Part.makeBox(10, 10, 10, FreeCAD.Vector(0, 0, 0)),
            Part.makeBox(10, 10, 10, FreeCAD.Vector(0, 15, 0)),
            Part.makeBox(10, 10, 10, FreeCAD.Vector(0, 30, 0)),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            results = export_pieces(pieces, tmpdir, base_name='test')

            assert len(results) == 3
            for i, r in enumerate(results):
                expected = os.path.join(tmpdir, f'test_{i+1}_of_3.stl')
                assert os.path.exists(expected)
                assert r['facets'] > 0

    def test_export_pieces_with_printer(self):
        import Part
        from export_utils import export_pieces

        pieces = [Part.makeBox(10, 10, 10)]

        with tempfile.TemporaryDirectory() as tmpdir:
            results = export_pieces(pieces, tmpdir, printer='m7_pro')
            assert results[0]['fits_printer'] is True
