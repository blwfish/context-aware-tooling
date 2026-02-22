# AnyCubic M7 Pro/Max

## Specs

| | |
|---|---|
| Technology | MSLA (mono LCD) |
| Build volume | 218 × 123 × 260 mm (Pro) / 298 × 164 × 300 mm (Max) |
| XY resolution | 0.049 mm (Pro) / 0.052 mm (Max) |
| Z resolution | 0.01 mm |
| Light source | Mono LCD, 405 nm |
| Release mechanism | Tilt (TSMC) |

## Key Slicer Settings (Chitubox 2.x)

### Layer / Exposure

| Setting | Starting Point | Notes |
|---------|---------------|-------|
| Layer height | 0.05 mm | 0.03 for fine detail |
| Normal exposure | resin-specific | see resin profiles |
| Bottom exposure | 6–10× normal | |
| Bottom layers | 4–6 | |
| Transition layers | 6–8 | gradual ramp from bottom to normal |

### Lift (TSMC tilt mechanism)

| Setting | Starting Point | Notes |
|---------|---------------|-------|
| Lift distance | 5 mm | |
| Lift speed 1 | 40 mm/min | slow initial pull (suction) |
| Lift speed 2 | 120 mm/min | faster after FEP releases |
| Retract speed 1 | 80 mm/min | |
| Retract speed 2 | 150 mm/min | |
| Rest time before lift | 0 s | increase if delaminating |
| Rest time after retract | 0.5 s | |

> Tilt speeds matter a lot for large cross-sections — slow lift 1 prevents
> delamination and FEP damage. Tune this before tuning exposure.

### Anti-Aliasing / Image

| Setting | Value |
|---------|-------|
| Anti-aliasing | On, level 4 |
| Image blur | 1–2 px |
| Grey level | 0 (off unless needed) |

## Maintenance Notes

<!-- Date-stamped entries -->
- 2026-02 — Upgraded to Chitubox 2.x; all resin profiles need to be rebuilt from scratch
