# M7 Pro Calibration Framework

## Overview

The M7 Pro resin printer, unlike stable hardware like the Nikon Z9, is a **consumable-dependent, degrading system**. Material behavior changes over time due to component wear, LED degradation, and consumable lifecycle. This framework establishes empirical ground truth for Brian's specific M7 Pro hardware across its operating lifetime, replacing internet consensus with measured data.

The calibration approach mirrors Reikan FoCAL's methodology for camera AF tuning: systematic measurement of actual hardware behavior, tagged with system state, enabling context-aware decisions in the tooling.

## Motivation

Current state of the art in resin printing relies heavily on:
- Manufacturer specifications (which vary by region and batch)
- YouTube advice (often conflicting, rarely specific to individual hardware)
- Trial-and-error iteration (expensive in material and time)
- Intuition from accumulated experience (not transferable or reproducible)

By contrast, the FreeCAD generators and support strategies developed for this layout benefit from **objective, measurable baseline data** tied to the specific M7 Pro instance they'll run on. This allows the context-aware tooling to make intelligent recommendations rather than generic ones.

## Key Difference from One-Time Calibration

Unlike camera AF calibration (mechanically stable, calibrated once), the M7 Pro is subject to continuous degradation:

- **FEP Film**: Replaced periodically (~6-12 months depending on use). New FEP has different adhesion, cure dynamics, optical transmission, and surface finish characteristics than aged FEP.
- **LED Array**: Output degrades over operating hours. Spectrum shifts, intensity drops, uniformity may degrade.
- **Heating System**: Temperature regulation may drift slightly over time.
- **Resin Batch Variance**: Even sealed resin can absorb moisture or exhibit viscosity changes batch-to-batch.
- **Environmental Factors**: Ambient temperature and humidity affect cure behavior, especially with materials sensitive to these conditions.

**Calibration is therefore continuous monitoring**, not a one-time tune-up.

## Test Suite Design

### Test Part Geometry

A simple, standardized test plate optimized for measuring key failure modes:

- **Base**: 100mm × 100mm × 2mm flat rectangular slab (XY plane, Z-axis as height)
- **Measurement features**:
  - Four corner mounting holes (M3, for securing to reference frame)
  - Central 50mm × 50mm grid with 5mm spacing for dimensional accuracy measurement
  - Edge markings to detect warping (3mm tall ridges at cardinal edges)
  - Optional texture surface section (50mm × 20mm area with known surface detail)

### Support Strategy (Standardized)

Print with **light grid supports** at consistent spacing to establish baseline:
- Support diameter: 0.8mm (Chitubox "light")
- Grid spacing: 15mm × 15mm across the entire underside
- Orientation: **Perpendicular to build platform** (flat plate vertical, supports on one edge face)

Rationale: This isolates material behavior from support artifacts, avoiding the speckled-surface problem of sparse supports while keeping the test simple and repeatable.

### Test Execution Protocol

Each test print is tagged with this metadata:

```
Date: YYYY-MM-DD
Material: [Standard | ABS-like 2.0 | Texture]
Material Batch: [batch ID if available]
LED Operating Hours: [cumulative hours on LED array]
FEP State: [new | ~X months old | replaced today]
Build Platform State: [notes on condition]
Ambient Temperature: [°C]
Ambient Humidity: [%]
Slicing Software: [Chitubox version]
Exposure Settings: [if customized from defaults]
```

## Measurement Protocol

After each test print, measure and record:

### Dimensional Accuracy
- Measure the 5mm grid spacing at multiple points (center, edges, diagonals)
- Calculate deviation from 5mm ideal (tolerance should be ±0.2mm for HO scale work)
- Record min/max/mean deviation

### Surface Quality
- Visual inspection: speckles, striations, voids
- Texture section: does surface detail print cleanly?
- Support contact marks: severity and ease of removal

### Warping
- Measure the edge ridges with calipers; they should be 3mm tall
- If warped, measure the actual height deviation and direction (bowed up/down)
- For the flat plate itself, measure flatness with a straightedge or dial indicator if available

### Finish Quality
- Cure completeness: does the part feel fully cured, or slightly tacky?
- Brittleness: does it break easily if flexed or struck gently?
- Surface roughness: smooth or gritty?

### Post-Processing Notes
- How easily do support marks sand out?
- How does paint/stain adhere?

## Data Organization

Store results in a git-tracked directory structure:

```
context-aware-tooling/
  docs/
    M7_Calibration_Framework.md
  data/
    m7_calibration/
      results.csv
      2025-02-27_Standard_NewFEP_baseline.md
      2025-03-15_ABS-like2.0_400hrs_baseline.md
      ...
```

The `results.csv` file (append-only) records the key metrics for trend analysis:

```csv
Date,Material,LED_Hours,FEP_Age_Months,Ambient_Temp_C,Grid_Accuracy_Max_Dev_mm,Warping_mm,Surface_Quality_Score,Support_Removal_Ease_Score,Notes
2025-02-27,Standard,150,new,22,0.15,0.0,9,8,Baseline print - excellent
2025-03-15,ABS-like2.0,400,3,21,0.18,0.1,8,7,Slight warping appeared
...
```

Quality scores: 1-10 scale (1=failed/unacceptable, 10=perfect).

## Calibration Phases

### Phase 1: Material Baseline (This Sprint)

Print one test part per material at **current LED operating hours** and **current FEP age** to establish baseline behavior:
- Standard resin
- ABS-like 2.0
- Texture resin

This gives ground truth for "what does my printer produce *right now*?"

### Phase 2: LED Degradation Tracking

Continue printing a test part every ~100 LED operating hours (roughly monthly with normal use) using the same material. Track how performance changes as the LED ages.

### Phase 3: FEP Lifecycle

When FEP is replaced, immediately print a test part with new FEP, then track behavior over the FEP's lifetime until replacement.

### Phase 4: Batch Variance (Optional)

When resin is reordered, print a test part to check if a new batch behaves differently from the previous one.

## Using Calibration Data in Context-Aware Tooling

Once baseline data exists, the context-aware tooling can:

1. **Adjust support recommendations** based on LED age (older LED = tighter supports needed)
2. **Tune exposure settings** if degradation becomes apparent
3. **Flag material/hardware combinations** that are out of spec
4. **Predict when FEP replacement might improve results**
5. **Learn material-specific quirks** (e.g., "Texture always warps 0.2mm more than ABS-like at similar conditions")

Example decision logic:
```
If LED_hours > 600 and material == "ABS-like2.0":
  Recommend tighter support grid (12mm spacing instead of 15mm)
  Flag potential dimensional accuracy loss (0.25mm+)
```

## Long-Term Insights

Over 12+ months of data, patterns emerge:

- **Material performance degradation curve** as LED ages
- **FEP replacement benefit**: how much does a new FEP improve results?
- **Seasonal effects**: does winter vs. summer ambient temperature matter?
- **Batch consistency**: do different resin batches behave measurably differently?
- **Support strategy optimization**: what spacing/diameter produces best results with minimal artifact?

This becomes the **empirical baseline** for all downstream design decisions in the layout project.

## Success Criteria

The calibration framework succeeds when:

1. **Ground truth exists**: For any given material and printer state, you have measured data showing what to expect
2. **Trends are visible**: Data accumulated over months shows clear degradation/improvement patterns
3. **Context-aware tooling can use it**: Support strategies and material recommendations are data-driven, not guesswork
4. **Reproducibility improves**: Prints become more predictable because decisions are based on actual hardware behavior, not internet consensus
5. **Consumable lifecycle is visible**: You can see exactly when FEP replacement, LED refresh, or other maintenance would pay dividends

## Next Steps

1. Print baseline test parts for all three materials this week
2. Measure and document results using the protocol above
3. Create initial `results.csv` entry
4. Integrate calibration data into context-aware tooling decision logic (starting with support recommendations)
5. Establish recurring print schedule (monthly, or whenever significant hardware changes occur)
