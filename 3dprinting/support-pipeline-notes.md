# Support pipeline — investigation notes

Living doc for tuning and diagnosing the orientation / support-contact
pipeline in this directory.  Append-only on the bottom; insertions at the
top only when reorganizing.

Related modules: `orientation.py`, `part_pipeline.py`, `support_utils.py`,
`diagnostics.py`.

---

## Known issues

### Scorer / contact-gen blind spot
**Status:** partially mitigated (NON_DISPLAY_NOT_DOWN_WEIGHT tuned down 2026-04-17)
**Still open:** visibility buffer occluding real display-side overhangs (see below)

Two filters, tuned independently, can silently disagree about which
downward-facing facets matter:

1. `orientation.py` scorer: `SEVERE_OVERHANG_NZ = -0.6` — facets in the
   range -0.6 < nz < -0.2 are treated as "cosmetic shingle step" and
   excluded from `display_down_area`
2. `part_pipeline.py` `collect_downward_facets`: requires facet centroid
   to sit within the 0.5 mm non-display band in part frame; everything
   else is dropped

Failure mode: orientation scorer picks an orientation with
`display_down_area = 0` based on filter (1); contact generator then
refuses to put supports on display-side facets via filter (2); the
downward-facing facets in between are unsupported in the final print.

Observed case: **Bay** part (cabin-v5 bay window) with
`non_display_dir=(0, 1, 0)`, winning orientation `part+Zdown+tiltx30`.
2085 mm² of top-front facets at `nz = -0.5` scored as zero `display_down_area`
but got zero contacts.  See diagnostic overlay `Bay_orphans_display_side_mild`.

### Visibility-buffer occlusion hides real display-side overhangs
**Status:** open — discovered 2026-04-17
**Affected parts:** Bay

The scorer's visibility check culls display-side facets that are occluded
from a straight-on display-axis projection.  For shelled / protruding
parts (bay windows, cornices, awnings), the UNDERSIDE of a front-facing
ledge projects to the same XZ grid as the FRONT of that ledge; the front
wins the visibility check, the underside is marked "hidden," and the
scorer excludes it from `display_down_area`.

Real viewers looking from below (or at any angle off the display axis)
WILL see the underside, so treating it as hidden is a cosmetic
underestimate.  More importantly, these facets are downward-facing and
will droop / magic-island in print — but they receive no supports
because contact-gen also rejects display-side facets.

Evidence: Bay at tiltx30y10 has 2884 mm² of diagnostic-flagged
display-side orphans (nz < -0.4), but scorer reports `display_down_area = 0`.
Lowering `SEVERE_OVERHANG_NZ` to -0.4 did NOT increase
`display_down_area` in the winning orientation, because the visibility
buffer is rejecting these facets before the NZ threshold is even
consulted.

**Possible fixes (none tried yet):**
- Expand visibility check to a cone of angles, not just perpendicular axis
- Drop visibility culling entirely for `display_down_area` (accept
  false positives in exchange for no false negatives)
- Add a third orphan category in the diagnostic: "hidden from display
  but still needs structural support" — and allow supports on these
  (they land on down-facing hidden geometry, not visible detail)

---

## Tuning log

Newest entries at the top.

### 2026-04-17 — NON_DISPLAY_NOT_DOWN_WEIGHT: 2000 → 200
**Commit:** `ea68c87`
**Why:** the 2000× weight was a proxy for "supports will land on display
geometry," but `DISPLAY_DOWN_WEIGHT * display_down_area` already
measures that directly.  At 2000×, the proxy dominated the sum and
rejected all mild tilts that kept `display_down_area` at 0 — the very
cases where the direct measure said the orientation was safe.
**Effect on Walls (`cabin-v5-bricked trimmed walls`):**
- winner: `part+Zdown` (flat) → `part+Zdown+tiltx10`
- `peel_force`: 147 → 47 (3× reduction)
- `display_down_area` stayed 0 (cosmetic risk unchanged)
- orphan display-side: 17 facets (34 mm²) → 23 facets (42 mm²) — noise
**Effect on Bay:**
- winner: `part+Zdown+tiltx30` → `part+Zdown+tiltx30y10`
- orphan area: 3582 mm² (83.6%) → 2894 mm² (67.3%) — partial improvement
- blind-spot orphan count essentially unchanged (root cause is visibility, not weight)
**Regressions:** none detected across tested parts (Walls, Bay)

### 2026-04-17 — Orphan-facet diagnostic
**Commits:** `3b7e6a6` (module), `1aa9b25` (pipeline hook)
Added `diagnostics.py`.  After every `process_part`, walks all downward
facets, flags those with no support contact within `grid_spacing`, and
classifies each as `display-side`, `display-side-mild`, `non-display-gap`,
or `tiny`.  Emits one Mesh::Feature per class into the FreeCAD doc so
you can toggle overlays alongside the support compound.
Wired into `add_result_to_doc`; default on, pass `diagnostics=False` to
skip.

### 2026-04-17 — Neck collision avoidance + extra Y-tilt
**Commit:** `49e8fb7`
Support neck direction: 8 candidate directions (centroid + 45° rotations),
first one whose column clears all part geometry wins.  Falls back to
centroid if all collide.  Fixes neck columns spearing interior walls on
hollow shells.
Added `y=5°` to the dual-axis tilt candidate set.

---

## Ideas tried and rejected

### SEVERE_OVERHANG_NZ: -0.6 → -0.4 (2026-04-17)
**Reason rejected:** does not change Bay's winning orientation or
scorer `display_down_area`.  The visibility buffer culls the target
facets upstream of this threshold, so tightening the NZ cutoff is
inert for this class of issue.
**When this MIGHT help:** for parts where display-side overhangs are
NOT visibility-occluded (flat display faces tilted into the dead zone).
None tested yet.

---

## Per-part orphan baselines

Snapshots captured at known pipeline versions so we can detect
regressions.  Format: `part-name @ commit — supported% / display-side
mm² / display-side-mild mm²`.

### 2026-04-17 @ ea68c87 (current HEAD at time of baseline)
- **Bay** (cabin-v5-Bay sans windows.stl, `nd=(0,1,0)`):
  32.7% supported; display-side 881 mm² / 45 facets; display-side-mild
  2008 mm² / 75 facets.  Winning: `part+Zdown+tiltx30y10`.
- **Walls** (cabin-v5-bricked trimmed walls.stl, `nd=(0,0,1)`):
  60.6% supported; display-side 42 mm² / 23 facets; tiny 416 mm² (brick
  micro-texture, self-supporting, ignore).  Winning: `part+Zdown+tiltx10`.

### 2026-04-17 @ 1aa9b25 (pre-weight-tuning baseline, for reference)
- **Bay:** 16.4% supported; display-side 1493 mm² / 51 facets;
  display-side-mild 2085 mm² / 70 facets.  Winning: `part+Zdown+tiltx30`.
- **Walls:** 90.6% supported; display-side 34 mm² / 17 facets; tiny 75
  mm².  Winning: `part+Zdown` (flat).

---

## Open questions

- Is the visibility buffer culling "correctly" for Bay (no real cosmetic
  risk) or "incorrectly" (real cosmetic risk that the current projection
  model can't see)?  Need a visual audit of the flagged facets in the
  FreeCAD doc to decide.
- If we add cone-angle visibility, what cone angle?  ±30°?  And should
  it widen for shelled / protruding parts specifically?
- Can we distinguish "hidden from display but still structurally
  needs support" from "hidden AND inside a cavity where supports can't
  reach anyway"?  The former wants supports; the latter is genuinely
  unsupportable.
- Is brick micro-texture contributing meaningful print quality risk,
  or is `min_interesting_area=0.5` mm² already the right cutoff?
  (Gut: it's right.  But a print with tilt will be the test.)

---

## Scoring weights quick reference

Current values as of 2026-04-17 (in `orientation.py`):

| constant | value | what it controls |
|---|---|---|
| `DISPLAY_DOWN_WEIGHT` | 10.0 | per mm² of visible down-facing surface |
| `OVERHANG_WEIGHT` | 0.1 | per mm² of non-display underside (cheap) |
| `FOOTPRINT_WEIGHT` | 0.05 | per mm² of XY bbox (peel proxy) |
| `PEEL_FORCE_WEIGHT` | 0.5 | per unit worst-layer peel force |
| `NON_DISPLAY_NOT_DOWN_WEIGHT` | 200.0 | tiebreaker, was 2000 (over-weighted) |
| `SEVERE_OVERHANG_NZ` | -0.6 | cosmetic/structural cutoff for display-side facets |
| `DOWNWARD_NZ_THRESHOLD` | -0.2 | anything flatter isn't considered downward at all |
| `NON_DISPLAY_BAND_MM` | 0.5 | absolute width of non-display band |
| `VISIBILITY_GRID_MM` | 1.0 | visibility-projection cell size |
| `VISIBILITY_TOL_MM` | 1.0 | depth tolerance for "close to topmost" |
