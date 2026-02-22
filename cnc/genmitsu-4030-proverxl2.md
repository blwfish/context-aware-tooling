# Genmitsu 4030 ProVerXL2

## Specs

| | |
|---|---|
| Controller | GRBL |
| Work area | 400 × 300 mm (XY), ~90 mm Z |
| 4th axis | Available (A axis, rotary) |
| Spindle | — |
| Units | mm |

## GRBL Configuration

<!-- Paste $$ output here after confirming -->

```
(paste $$ output)
```

## Coordinate System

- Home: front-left corner
- Work zero: typically set per job
- 4th axis zero: —

## Feed & Speed Guidelines

### Wood / MDF

| Operation | Spindle (RPM) | Feed (mm/min) | DOC (mm) | Notes |
|-----------|--------------|---------------|----------|-------|
| Roughing | — | — | — | |
| Finishing | — | — | — | |
| Profile | — | — | — | |

### Aluminum

| Operation | Spindle (RPM) | Feed (mm/min) | DOC (mm) | Notes |
|-----------|--------------|---------------|----------|-------|
| Roughing | — | — | — | Flood/mist recommended |
| Finishing | — | — | — | |

## 4th Axis

- Attachment type: —
- A-axis steps/degree: —
- Useful for: undercuts on terrain, cylindrical parts

## Workflow

1. Generate toolpaths in FreeCAD Path workbench
2. Post-process with `grbl` post processor
3. Load G-code in — (UGS / bCNC / CNCjs)
4. Set work zero, verify feeds, run air cut first

## Maintenance Log

| Date | Action |
|------|--------|
| | |
