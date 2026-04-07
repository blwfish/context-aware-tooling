// ShapeUpgrade_UnifySameDomain introduces self-intersections
//
// Minimal reproducer: two triangular prisms (wedges) sharing a face,
// fused with a box that is coplanar on one face but offset so it doesn't
// fully cover the wedges along the extrusion axis.
//
// The fuse result passes BOPAlgo_ArgumentAnalyzer.
// After UnifySameDomain, it fails with BOPAlgo_SelfIntersect.
//
// Tested with OCCT 7.8.1 on macOS arm64.
//
// Build:
//   g++ -std=c++17 -I$OCCT_INCLUDE -L$OCCT_LIB \
//     -lTKernel -lTKMath -lTKBRep -lTKTopAlgo -lTKBO -lTKPrim \
//     -lTKShHealing -lTKG3d \
//     occ_unifysamedomain_selfintersect.cpp -o test_usd
//
// DRAW Tcl equivalent at bottom of file.

#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepBuilderAPI_Copy.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BOPAlgo_ArgumentAnalyzer.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include <iostream>

// Check shape for self-intersections using BOPAlgo_ArgumentAnalyzer
bool hasSelfIntersections(const TopoDS_Shape& shape) {
    TopoDS_Shape copy = BRepBuilderAPI_Copy(shape).Shape();
    BOPAlgo_ArgumentAnalyzer checker;
    checker.SetShape1(copy);
    checker.SelfInterMode() = true;
    checker.SetRunParallel(true);
    checker.Perform();
    return checker.HasFaulty();
}

int main() {
    // --- Build two triangular prisms (wedges) ---
    // Profile: triangle at X=0, vertices at (0,0,0), (0,1,0), (0,0,10)
    // Extruded along +X to X=40

    gp_Pnt p1(0, 0, 0), p2(0, 1, 0), p3(0, 0, 10);

    BRepBuilderAPI_MakeWire wireMaker;
    wireMaker.Add(BRepBuilderAPI_MakeEdge(p1, p2).Edge());
    wireMaker.Add(BRepBuilderAPI_MakeEdge(p2, p3).Edge());
    wireMaker.Add(BRepBuilderAPI_MakeEdge(p3, p1).Edge());
    TopoDS_Wire wire = wireMaker.Wire();

    BRepBuilderAPI_MakeFace faceMaker(wire);
    TopoDS_Face face = faceMaker.Face();

    gp_Vec extrudeDir(40, 0, 0);
    TopoDS_Shape wedge1 = BRepPrimAPI_MakePrism(face, extrudeDir).Shape();

    // Second wedge: translated by (0, 0, 10) so they share a face at Z=10
    gp_Trsf trsf;
    trsf.SetTranslation(gp_Vec(0, 0, 10));
    TopoDS_Shape wedge2 = BRepBuilderAPI_Transform(wedge1, trsf).Shape();

    // --- Build a box (wall) ---
    // Coplanar with wedge back face at Y=0, but offset in X:
    // starts at X=1, so it does NOT cover the wedge profile face at X=0
    TopoDS_Shape wall = BRepPrimAPI_MakeBox(
        gp_Pnt(1, -5, 0), gp_Pnt(40, 0, 20)
    ).Shape();

    // --- Fuse all three ---
    BRepAlgoAPI_Fuse fuse1(wall, wedge1);
    if (!fuse1.IsDone()) {
        std::cerr << "First fuse failed" << std::endl;
        return 1;
    }
    BRepAlgoAPI_Fuse fuse2(fuse1.Shape(), wedge2);
    if (!fuse2.IsDone()) {
        std::cerr << "Second fuse failed" << std::endl;
        return 1;
    }
    TopoDS_Shape fused = fuse2.Shape();

    // --- Verify fuse result is clean ---
    if (hasSelfIntersections(fused)) {
        std::cerr << "UNEXPECTED: Fuse result has self-intersections" << std::endl;
        return 1;
    }
    std::cout << "Fuse result: clean (no self-intersections)" << std::endl;

    // --- Apply UnifySameDomain ---
    ShapeUpgrade_UnifySameDomain unifier(fused);
    unifier.Build();
    TopoDS_Shape unified = unifier.Shape();

    // --- Check unified result ---
    if (hasSelfIntersections(unified)) {
        std::cout << "UnifySameDomain result: SELF-INTERSECTIONS DETECTED  <-- BUG"
                  << std::endl;
        std::cout << std::endl;
        std::cout << "Expected: no self-intersections" << std::endl;
        std::cout << "Got: self-intersections at the Z=10 boundary" << std::endl;
        std::cout << std::endl;
        std::cout << "The self-intersections are at the boundary where the two" << std::endl;
        std::cout << "wedges share a face (Z=10), in the X=0..1 region where" << std::endl;
        std::cout << "the box does not cover the wedges. UnifySameDomain" << std::endl;
        std::cout << "incorrectly merges faces across this boundary." << std::endl;
        return 1;
    } else {
        std::cout << "UnifySameDomain result: clean (bug may be fixed!)" << std::endl;
        return 0;
    }
}

// --- DRAW Tcl equivalent ---
//
// # Build wedge profile
// vertex v1 0 0 0
// vertex v2 0 1 0
// vertex v3 0 0 10
// edge e1 v1 v2
// edge e2 v2 v3
// edge e3 v3 v1
// wire w1 e1 e2 e3
// mkplane f1 w1
// prism wedge1 f1 40 0 0
//
// # Second wedge, translated
// copy wedge1 wedge2
// ttranslate wedge2 0 0 10
//
// # Box (wall)
// box wall 1 -5 0 39 5 20
//
// # Fuse
// bfuse temp wall wedge1
// bfuse fused temp wedge2
//
// # Check fuse
// bopargcheck fused
//
// # UnifySameDomain
// unifysamedom unified fused
//
// # Check result
// bopargcheck unified
