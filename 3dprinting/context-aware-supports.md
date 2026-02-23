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

### Build Volume as Hard Constraint

The build volume is not a preference -- it is a gate. The pipeline must check
fit *before* optimizing orientation or support strategy.

```
1. Compute bounding box of model + raft + supports at candidate orientation
2. Check against target printer build volume
3. If it doesn't fit: split is mandatory, not optional
```

This interacts with tilt angle: tilting a model increases its footprint in
one axis. A wall that fits upright may not fit tilted at the preferred 18
degrees. The pipeline may need to:

- **Reduce tilt angle** to fit, accepting worse peel characteristics
- **Split the model** to allow the preferred tilt on smaller pieces
- **Choose between printers** if multiple are available (M7 Pro: 218x123mm,
  M7 Max: 298x164mm)

Real examples at HO scale (1:87.1):

| Model | Print-scale size | Fits M7 Pro? | Fits M7 Max? | Notes |
|-------|-----------------|--------------|--------------|-------|
| CWM secondary building (4x3x2 bays) | ~140x100mm | Yes | Yes | One piece, room to tilt |
| CWM main building (7x5x3 bays) | ~240x160mm | No | Barely | Must minimize tilt or split |
| Roundhouse long wall | ~280mm+ | No | Only at specific tilt | Tilt is dictated by fit, not preference |

When fit dictates tilt, support strategy must adapt to whatever orientation
the build volume forces. This is the opposite of the normal flow (choose
orientation, then support). The pipeline must handle both directions:

- **Preferred path:** choose best orientation -> verify fit -> support
- **Constrained path:** determine what fits -> pick best orientation within
  that constraint -> support accordingly

Splitting is often the better answer. Two pieces printed at optimal tilt
will outperform one piece crammed in at a compromise angle.

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

### Automated Split + Registration

At scale (50-100+ buildings), manual splitting is a non-starter. The split
operation is highly automatable because it is the same geometric operation
every time:

1. Choose a split plane (mortar line, floor break, corner)
2. Boolean-cut the body into two halves
3. Add tapered registration pins on one half, matching sockets on the other
4. Both sides are cut from the same plane -- alignment is exact by construction

What makes this painful by hand is getting pin and socket to align precisely
across two separately-edited bodies. By machine, both sides derive from the
same split plane and pin centers are deterministic offsets -- there is nothing
to align manually.

#### Pin/Socket Geometry (print-scale mm)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Pin radius | 0.6 mm | Large enough to be structural, small enough for thin walls |
| Pin height | 1.5 mm | Deep enough for positive location, short enough to not weaken wall |
| Draft angle | 2 deg | Slight taper for press-fit insertion |
| Socket clearance | 0.12 mm radial | Accounts for resin shrinkage + print tolerance on M7 Pro |
| Pin spacing | ~15 mm along split edge | 2-4 pins per typical wall panel |
| Edge margin | 3 mm inset from ends | Avoids thin-wall blowout at corners |

The socket is slightly deeper than the pin (by the clearance amount) to
provide bottoming room -- the pin seats on its taper, not on the socket
floor.

#### Split Plane Selection Heuristics

The pipeline should prefer split planes that:

1. **Coincide with mortar lines** -- the seam disappears into existing texture
2. **Fall at floor breaks** -- natural horizontal split between stories
3. **Avoid window openings** -- splitting through a window creates fragile
   mullion fragments and alignment nightmares
4. **Produce pieces that fit the target printer** at the preferred tilt angle
5. **Keep structural features intact** -- don't split a lintel in half

For a typical multi-bay building, the search is:
- Identify mortar-line Y (or X) coordinates from the geometry
- Filter to those that don't cross window openings
- Pick the one(s) that produce pieces fitting the build volume at 18 deg tilt
- If no single split works, try two splits (thirds)

This is geometry inspection + constraint satisfaction -- exactly what the
LLM can do by inspecting the model via MCP and reasoning about the options.

#### Corner Joints (L-joints)

When a rectangular building is split into wall panels at corners, the
panels meet at 90-degree L-joints, not butt joints. Registration strategy
differs:

- **Butt joint** (wall split along a mortar line): flat faces meet.
  Pin/socket registration is straightforward -- pins on one face,
  sockets on the other.
- **L-joint** (corner where two walls meet at 90 degrees): the walls
  share a corner volume (wall_thickness x wall_thickness column). This
  overlap region itself provides alignment in both axes. A dab of CA
  glue is sufficient; pins/sockets are unnecessary for L-joints at
  HO scale wall thicknesses (~5mm print scale).

For centroid-based panel classification (no boolean splitting), both
panels include the corner overlap volume. This is correct for printing --
the slicer unions the overlap when each panel is sliced independently.
At assembly, the corner region interlocks.

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

3. **Cosmetic vs structural overhang by area, depth, and pattern.** Two tests,
   either of which classifies a face as cosmetic:

   **Area test:** Brick courses produce hundreds/thousands of tiny overhang faces
   (< 1mm² each) at regular Z intervals. A lintel produces a few larger overhang
   faces (> 1mm²) at a specific Z.
   - Regular Z spacing + small area + many faces = cosmetic (brick/clapboard)
   - Irregular Z + larger area + few faces = structural (lintel/cornice)

   **Depth test:** The overhang's projection depth (how far it sticks out
   unsupported) is a better discriminator than area alone. Brick course steps
   are ~0.3-0.4mm deep regardless of how wide (in X) the face is -- a 4mm-wide
   brick step face has area 1.6mm² which exceeds the area threshold, but it's
   still self-supporting because the overhang depth is tiny. Lintels are 4-5mm
   deep (spanning the full wall thickness).
   - Overhang depth < 1mm = cosmetic (self-supporting micro-feature)
   - Overhang depth >= 1mm = structural (needs evaluation for support)

   The depth test catches the case where a wide but shallow brick course face
   exceeds the area threshold. Use the face bounding box's second-smallest
   dimension as a proxy for overhang depth.

4. **Mullion detection.** Window mullions are thin bars spanning openings:
   - Vertical mullion: thin in X and Y, tall in Z
   - Horizontal mullion: thin in Y and Z, wide in X
   - Cross-intersection points (where H meets V) are the strongest locations
     on a mullion -- the only acceptable support contact point if support
     is absolutely necessary.

## Tilt Direction and Support Geometry

Getting tilt direction wrong is the single most catastrophic error the pipeline
can make. Supports end up on the display surface instead of the interior.

### Tilt Direction Rule

**Interior toward plate, display away from plate.** For a building wall:

- The wall's display surface (brick, clapboard, detail) faces UP and AWAY
  from the build plate.
- The wall's interior surface faces DOWN, TOWARD the build plate.
- Supports attach to the interior (underside) and grow upward to the raft.

For a front wall with display at Y=0 facing -Y, interior at Y=4.8 facing +Y:
- **Correct:** rotate so the top tilts toward +Y (interior side). The wall
  leans back, display face on top. Supports are below/behind on the interior.
- **Wrong:** rotate so the top tilts toward -Y (display side). Display face
  ends up underneath, supports would need to touch it.

### Vertical Supports on Tilted Walls

When the tilt is correct (interior toward plate), vertical supports work
naturally for all features:

- **Bottom face supports:** contact at wall base, short vertical columns
  to raft. Always safe.
- **Lintel/floor supports:** contact on the interior underside of the
  overhang, vertical column drops to raft. Since the interior face tilts
  downward, these contacts are "below" the wall in the Y direction.
  The support column stays on the interior side at all heights because
  the wall leans *away* from it.

If tilt were reversed (display toward plate), a support contacting the
interior side of a high lintel would need to cross *in front of* the
display surface at lower heights to reach the raft. This is why tilt
direction is critical -- it's not just about which surface gets marked,
it determines whether vertical supports are geometrically feasible.

### Tilt Direction Validation

Before placing any supports, verify:

```
For each support contact point (x, y_contact, z_contact):
  At all heights z from raft to z_contact:
    y_display(z) = position of display surface at height z
    Assert: y_contact is on the interior side of y_display(z)
```

If any contact fails this check, either the tilt direction is wrong or
the contact point is on the wrong side of the wall.

## Support Placement Rules

Applied after orientation is chosen and surfaces are classified.

### Hard Rules (never violated)

1. **No support contact on display surfaces.** Period.
2. **No support contact on fragile features** unless at a structural intersection
   (e.g., mullion cross-point) AND the feature would fail without it.
3. **No support on cosmetic overhangs.** Brick courses, clapboard lips, etc.
   are self-supporting at print scale.
4. **Support tip diameter ~0.5mm** for interior contacts. Larger tips leave
   marks, but interior surfaces are hidden after assembly. On display-adjacent
   contacts (rare, only at structural junctions), use 0.3mm tips.
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
4. **Taper supports.** Cone tip (0.25mm contact) -> column (0.7mm radius) ->
   base pad (1.5mm radius on raft). These dimensions are deliberately heavy
   to resist MSLA peel forces; the contact is on interior surfaces where
   marks are acceptable.
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
5. **Raft sized to support footprint, not just model footprint.** The raft
   must cover all support base pad positions, which may extend beyond the
   model's bounding box (e.g., supports for high features on a tilted wall
   land further back on the raft than the model's base).
6. **Bottom supports required.** Because the model is raised off the raft
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
7. **Dual Y-rows** for bottom and large overhang faces: one row at interior
   edge, second row 2mm inboard. Both on interior half. This resists
   peel-force tipping (single row tips; two rows 2mm apart act as truss).
8. **Build raft** sized to model footprint + 2mm margin, chamfered bottom

### Avoiding Boolean Operations

Boolean operations (fuse, cut, removeSplitter) on textured building geometry
are extremely expensive in OCCT. A 6-bay wall panel fuse+removeSplitter
takes 3+ minutes; adding pin/socket booleans on top can crash FreeCAD.

**Rule: use compounds, not booleans, wherever possible.**

| Operation | Boolean needed? | Alternative |
|-----------|----------------|-------------|
| Combine bay solids into panel | No | `Part.makeCompound()` -- instant |
| Add support columns to assembly | No | Compound -- slicer unions overlapping mesh |
| Add registration pins | No | Compound -- pin overlaps panel, slicer unions it |
| Cut registration sockets | **Yes** | Cut from single corner bay only (~260 faces), not fused panel (~1000+ faces) |
| Split model at plane | **Yes** | Half-space boolean, but only on the original compound |

For STL export, a compound of touching/overlapping solids produces valid
mesh. The slicer treats all enclosed volume as filled. Internal shared
faces between bays are harmless -- they don't affect the printed result.

The only operation that truly requires a boolean subtraction is cutting
socket holes for registration. Minimize the cost by operating on the
smallest possible geometry (one bay, not the whole panel).

**Always use `execute_python_async` + `poll_job` for boolean operations.**
The synchronous `execute_python` times out at 30s and a timeout during a
boolean can crash FreeCAD. Async has no timeout penalty -- just poll until
done. The cost of polling is negligible compared to the cost of a crash
and restart. Don't try to guess whether a boolean will be fast enough for
sync; booleans on textured geometry are unpredictable.

### transformShape vs transformGeometry

When rotating or translating compound geometry (e.g., rotating a wall
90 degrees for Left/Right orientation), **always use `transformShape`,
never `transformGeometry`**.

| Method | What it does | Performance |
|--------|-------------|-------------|
| `transformShape(matrix)` | Moves vertices directly | Instant, even on 14-solid compounds with 3700+ faces |
| `transformGeometry(matrix)` | Recomputes all OCCT BRep surface definitions | Minutes to hang/crash on compounds; 280%+ CPU |

`transformGeometry` is meant for operations that change the parametric
definition of surfaces (e.g., non-uniform scaling). For rigid transforms
(rotation, translation, uniform scaling), `transformShape` produces
identical results with zero computational cost.

```python
# Correct: rotate a compound 90 degrees around Z
mat = FreeCAD.Matrix()
rad = math.radians(90)
c, s = math.cos(rad), math.sin(rad)
mat.A11 = c; mat.A12 = -s; mat.A21 = s; mat.A22 = c
rotated = compound.copy()
rotated.transformShape(mat)
```

### Interior Side Determination

When tilting a wall for printing, the pipeline must know which side of
the tilted geometry is interior (toward plate) vs display (away from
plate). This depends on the wall's display orientation:

| Wall | display_faces_negative_y | After tilt, interior is at... | interior_y_side |
|------|-------------------------|-------------------------------|-----------------|
| Front | True (display at -Y) | YMax | `'max'` |
| Back | False (display at +Y) | YMin | `'min'` |
| Left | True (after -90° Z rotation) | YMax | `'max'` |
| Right | False (after +90° Z rotation) | YMin | `'min'` |

The math: for a front wall with display at -Y, tilting by -18° around X
rotates points so that Y' = Y*cos(t) - Z*sin(t). The interior surface
(at higher Y in the original) maps to higher Y' after tilt. So
interior_y_side='max' is correct for display_faces_negative_y=True.

### Binary STL Export

FreeCAD's `exportStl()` produces ASCII STL, which is 5-6x larger than
binary. For compounds with millions of triangles (typical for textured
walls + supports), use Python's `struct.pack` to write binary STL:

```python
import struct
triangles = shape.tessellate(0.01)  # or use Mesh.Mesh
with open(path, 'wb') as f:
    f.write(b'\x00' * 80)  # header
    f.write(struct.pack('<I', num_triangles))
    for tri in triangles:
        f.write(struct.pack('<12fH', *normal, *v0, *v1, *v2, 0))
```

Typical sizes for a 4-bay HO building wall with supports:
- ASCII STL: 669MB - 1.4GB
- Binary STL: 139MB - 307MB

### Prototype Scale

All modeling is at prototype (real-world) scale. For HO (1:87.1):
- 0.3mm at print scale = 26.1mm prototype
- 0.6mm at print scale = 52.3mm prototype
- Mullion thresholds are in print-scale mm, not prototype

Export applies scale factor: `scale = 1/87.1 = 0.01148`
