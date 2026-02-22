# Context-Aware Support Generation

Rules for intelligent support placement when Claude drives the CAD-to-print pipeline.
These replace the "dumb overhang angle" approach used by slicers.

## Core Principle

A slicer sees triangles. We see *what the geometry is*. Support decisions depend on
the structural role, display visibility, and fragility of each feature -- not just
its overhang angle.

## Why This Exists

Auto-support in slicer software (Chitubox, Lychee, etc.) treats a model as an
anonymous triangle mesh. It cannot know that a surface is a brick facade facing a
layout aisle, or that a bottom edge will be sanded flat after assembly, or that
a door opening means the ground-floor interior is visible. It will wreck detail
surfaces with support forests and leave structural overhangs dangling.

The LLM-driven pipeline replaces that with a conversation:

1. **The user provides context the geometry can't encode.** Which wall faces the
   aisle. Which wall faces the backdrop. Where doors are (and therefore where
   interior detail is visible). What assembly method is planned. What post-
   processing is acceptable (sanding, filling, painting).

2. **The LLM inspects the model geometry via MCP** and combines that context with
   the physical rules below to produce a print strategy -- not just support
   placement, but potentially model splitting, reorientation, and registration
   features.

3. **The user reviews and adjusts.** The conversation is the design review.

This is fundamentally different from parameter-tuning a slicer. The pipeline has
access to semantic knowledge that no slicer can infer from mesh geometry alone.

## Print Strategy Pipeline

The full pipeline is broader than "add supports." For complex models (multi-wall
buildings, assemblies), the pipeline includes model preparation:

```
1. Inspect model geometry (FreeCAD MCP)
2. Receive context from user (which surfaces matter, layout placement,
   post-processing plans, assembly method)
3. Plan print strategy:
   - Print as one piece, or split?
   - If split: where? (along mortar lines, at floor breaks, at corners)
   - What tilt angle per piece?
   - Which faces get supports, which are untouchable?
4. Modify model if needed:
   - Split bodies along chosen planes
   - Add registration features (pins/sockets, alignment tabs)
   - Orient each piece for its print direction
5. Per piece: classify faces -> generate supports -> build raft
6. Export plate(s)
```

### Assembly-Aware Decisions

Splitting and assembly strategy affects support strategy. Options that are
painful by hand but straightforward via MCP:

- **Split along mortar lines** so seams disappear into brick pattern
- **Print non-display walls flat** (backdrop-facing wall doesn't need tilt)
- **Separate stories** -- shorter walls warp less, fit smaller build plates
- **Print corner columns separately** with registration pins, so each wall
  panel can be oriented independently
- **Add sacrificial tabs** at joints, trimmed after assembly
- **Add sanding datum surfaces** -- a flat bottom edge that will be sanded
  is a feature, not a defect; supports there are free

### Context the User Provides

The user tells the pipeline things that geometry alone cannot reveal:

| Context | Effect on Strategy |
|---------|--------------------|
| "This wall faces the aisle" | Display surface -- zero support contact |
| "This wall faces the backdrop" | Interior -- supports are acceptable |
| "There's a door at ground level" | Bottom edge will be sanded; heavy supports OK |
| "The interior is visible through windows" | Interior surface near windows becomes display |
| "I'm painting this wall" | Paint hides small support marks; lighter touch OK |
| "This joins to another piece at the corner" | Registration features needed; joint line is a seam |
| "The roof hides the top edge" | Top edge roughness is acceptable |

## Print Orientation

Orientation is chosen *before* supports. It determines what needs support at all.

### Orientation Rules

1. **Display surface away from plate.** The highest-quality surface (least support
   marks, no elephant's foot) faces away from the build plate.
2. **Tilt 15-30 degrees from vertical** for thin-walled structures (buildings, etc.).
   - Flat-on-plate: constant peel stress, no drainage, can't support internal voids.
   - Heavy tilt (45+): massive support forest, every surface detail becomes an overhang.
   - Moderate tilt: varying cross-section (good for peel), gravity-assisted resin
     drainage, internal features accessible for supports.
3. **Align thin features at an angle to layer lines.** A 0.3mm mullion printed with
   layers parallel to its axis has zero cross-layer bonding along its length -- one
   weak layer and it snaps. At 15-20 degrees, every layer crosses the feature,
   distributing load across many layer bonds.
4. **Minimize unsupported span.** Between two orientations that are otherwise equal,
   prefer the one with shorter maximum unsupported horizontal distance.

### Orientation for Common Model Railroad Structures

| Structure | Recommended Orientation | Rationale |
|-----------|------------------------|-----------|
| Building wall | Tilt back 15-20 deg, inner face toward plate | Brick/detail face unsupported; mullions get angled layers |
| Bridge/arch | Arch opening facing up or tilted | Soffit overhangs manageable; deck detail preserved |
| Roof panel | Tilt ~30 deg from horizontal | Reduces peel area; ridge detail faces away from plate |
| Cylindrical (tower, silo) | Axis 15-20 deg from vertical | Avoids flat cross-section at any layer |

## Surface Classification

Every face in the model is classified before support placement.

### Categories

| Category | Description | Support Policy |
|----------|-------------|----------------|
| **Display** | Exterior visible surface (brick, clapboard, stone, trim) | NEVER place supports |
| **Interior** | Inner wall surface, not visible when assembled | Preferred support contact surface |
| **Structural** | Load-bearing features (lintels, sills, jambs) | Support at junctions only |
| **Fragile** | Features < 0.6mm in any dimension (mullions, railings, trim) | No support contact; or only at structural intersections |
| **Cosmetic overhang** | Brick course lips, clapboard edges, molding undercuts | Ignore -- self-supporting micro-features |
| **Structural overhang** | Lintels, eaves, soffits, cornices (area >= 1mm²) | Requires support; place at load-bearing points |

### Classification Heuristics

1. **Display vs Interior by normal direction.** For a wall with known orientation,
   the outward-facing surface is display. The inward-facing surface is interior.
   Requires knowing the model's semantic orientation (which side is "outside").

2. **Fragile by dimension.** Measure the minimum bounding extent in each axis.
   If any cross-section dimension is < 0.6mm, classify as fragile. For the
   M7 Pro at 0.049mm XY resolution, features below ~0.2mm won't resolve at all;
   0.2-0.6mm is the fragile zone.

3. **Cosmetic vs structural overhang by area and pattern.** Brick courses produce
   hundreds/thousands of tiny overhang faces (< 1mm² each) at regular Z intervals.
   A lintel produces a few larger overhang faces (> 1mm²) at a specific Z.
   - Regular Z spacing + small area + many faces = cosmetic (brick/clapboard)
   - Irregular Z + larger area + few faces = structural (lintel/cornice)

4. **Mullion detection.** Window mullions are thin bars spanning openings:
   - Vertical mullion: thin in X and Y, tall in Z
   - Horizontal mullion: thin in Y and Z, wide in X
   - Cross-intersection points (where H meets V) are the strongest locations
     on a mullion -- the only acceptable support contact point if support
     is absolutely necessary.

## Support Placement Rules

Applied after orientation is chosen and surfaces are classified.

### Hard Rules (never violated)

1. **No support contact on display surfaces.** Period.
2. **No support contact on fragile features** unless at a structural intersection
   (e.g., mullion cross-point) AND the feature would fail without it.
3. **No support on cosmetic overhangs.** Brick courses, clapboard lips, etc.
   are self-supporting at print scale.
4. **Support tip diameter <= 0.3mm** for any contact on the model. Larger tips
   leave visible marks.
5. **Supports must connect to raft or build plate**, never free-standing.
6. **Raise model off raft.** The model must not rest directly on the raft.
   Elevate 2-3mm so that *all* contact between model and raft is through
   support tips. This allows supports to be removed after UV curing, when
   the resin is rigid and supports snap cleanly at the tip. Removing supports
   before curing (when the resin is still somewhat flexible) risks deforming
   or breaking fragile features.

### Soft Rules (prefer but may override with justification)

1. **Prefer interior surfaces** for support contact.
2. **Place supports at structural junctions** (wall corners, lintel-to-jamb,
   sill-to-jamb) rather than mid-span.
3. **Match support density to structural need.** A 14mm lintel span needs
   maybe 2 supports; a 50mm cornice needs more. Scale linearly with span,
   not with overhang area.
4. **Taper supports.** Cone tip (0.15-0.3mm contact) -> column (0.4-0.8mm) ->
   base pad (1.0-1.5mm on raft).
5. **Avoid support forests.** If a region needs > 1 support per 2mm², reconsider
   the print orientation instead.

### Window Opening Support Strategy

Windows are the hardest case: horizontal lintels overhang, mullions are fragile,
and the display surface is right there.

1. **Lintel support:** Place at jamb-to-lintel corners only (the wall-to-opening
   junction). These are the strongest points and hidden from view.
2. **Sill:** Usually doesn't need support (faces upward in normal orientation).
   If tilted, support at jamb-to-sill corners.
3. **Mullions:**
   - If mullion is > 0.6mm: no support needed, it's self-supporting.
   - If mullion is 0.3-0.6mm: support ONLY at cross-intersection point
     (horizontal meets vertical mullion), and ONLY if the overhang angle
     at that point exceeds 60 degrees from vertical.
   - If mullion is < 0.3mm: cannot be supported without destroying it.
     Adjust orientation to minimize overhang, or accept it prints unsupported.
4. **Glass plane:** The recessed plane where glass would be is interior --
   acceptable for light support if needed to reach a lintel.

## Raft Rules

1. **Raft extends 2mm beyond model footprint** in all directions.
2. **Raft thickness: 1.5mm.** Thick enough to peel cleanly, thin enough
   not to waste resin.
3. **Chamfer bottom face** (plate-facing) at 45 degrees, 0.3-0.5mm. Reduces
   suction on initial peel from build plate.
4. **Raft top surface** is the support attachment plane. All supports
   terminate on this surface.
5. **Bottom supports required.** Because the model is raised off the raft
   (see Hard Rule 6), the model's bottom face also needs supports. These
   are identical in geometry to other supports (tapered, cone-tipped) and
   should be distributed across the bottom face at ~5mm intervals along
   the longest axis, with front and back rows if the face depth > 3mm.

## Implementation Notes

### Face Count Reality

A building wall with brick texture can easily have 15,000+ faces. Iterating
all faces for classification is O(n) and fast, but generating supports naively
from face centroids produces absurd density. The pipeline is:

1. **Classify all faces** (one pass, < 1 second for 15k faces)
2. **Filter to supportable overhangs** (structural overhangs on interior surfaces)
3. **Cluster by spatial proximity** (merge nearby overhang faces into support regions)
4. **Place supports per region** (1-3 per region based on span)
5. **Snap to structural junctions** where possible
6. **Raise model off raft** (2-3mm) and add bottom-face supports
7. **Build raft** sized to model footprint + 2mm margin, chamfered bottom

### Prototype Scale

All modeling is at prototype (real-world) scale. For HO (1:87.1):
- 0.3mm at print scale = 26.1mm prototype
- 0.6mm at print scale = 52.3mm prototype
- Mullion thresholds are in print-scale mm, not prototype

Export applies scale factor: `scale = 1/87.1 = 0.01148`
