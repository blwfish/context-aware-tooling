# 3D Printing

Resin MSLA printing on the AnyCubic M7 Pro/Max.

## Printer

- **[AnyCubic M7 Pro/Max](anycubic-m7pro-max.md)** — specs, slicer setup, general notes

## Resins

Individual resin profiles are in [resins/](resins/). See [resins/README.md](resins/README.md) for the index.

Use [resins/_template.md](resins/_template.md) when adding a new resin.

## Environment Notes

- Both printers have **heated vats** — will not run if resin < 25°C
- Located in basement — ambient rarely exceeds 28°C even in summer
- Temperature is therefore a **controlled/stable variable** for this setup; resin profiles here do not need per-temperature variants. Note vat temp when logging if it was outside the 25–28°C range.

## Test Prints

For dialing in a new resin, print in this order:

1. **Exposure matrix** — `_tests/exposure-matrix.chitubox` (vary normal exposure ±20%)
2. **Ameralabs AMD-S** — tests fine detail, bridges, verticals, drain holes
3. **Target part at small scale** — usually 50% if it has relevant features

## Slicer: Chitubox

- Current version: 2.x (migrated 2026-02, settings rebuilt from scratch)
- Previous profiles are gone — rebuild per resin as needed
