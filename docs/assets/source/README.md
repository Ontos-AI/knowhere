# Benchmark Figure Sources

Vector sources for the README performance benchmark figure, rendered to
`docs/assets/benchmark-light.png` and `docs/assets/benchmark-dark.png`.

| File | Origin |
|:---|:---|
| `benchmark-light.svg` | Exported from the benchmark design (Matplotlib 3.11.2). Authoritative for layout, numbers, and the light palette. |
| `benchmark-dark.svg` | Derived from `benchmark-light.svg` by replacing color literals only. Geometry, labels, and numbers are unchanged. |

## Palette

The light palette comes from the design. The dark palette keeps the same
relative contrast per element so the bars stay subdued and the Knowhere accent
stays dominant. Bar and grid colors are the light color scaled in linear RGB to
the target contrast; `mist white` and `mineral green` are brand tokens.

| Role | Light | Dark | Contrast on its own background |
|:---|:---|:---|:---|
| Figure and axes background | `#ffffff` | `#010909` | design-provided dark field |
| Primary text | `#2e2e2c` | `#F0F2E6` | 13.61:1 → 17.76:1 |
| Secondary text | `#70776f` | `#80887f` | 4.61:1 → 5.50:1 |
| Non-Knowhere bars | `#c8d1cb` | `#3c3f3d` | 1.56:1 → 1.89:1 |
| Grid lines | `#dde4dc` | `#303230` | 1.30:1 → 1.56:1 |
| Knowhere accent | `#19a88b` | `#19a88b` | 2.99:1 → 6.72:1 |

## Rendering

Both variants are rendered from a wrapper page that sizes the SVG at 1600x1120
on the matching background (`#FFFFFF` and `#010909`), then screenshotted at
2x device scale, which yields the 3200x2240 PNGs in `docs/assets/`:

```bash
chrome --headless --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1600,1120 \
  --screenshot=benchmark-light.png file:///path/to/benchmark-light.html
```

## Editing Numbers

Matplotlib exported the text as glyph paths, so the SVG contains no editable
strings (86 label groups are `<path>`/`<use>` defs). Changing a metric means
editing the generating script or data and re-exporting, not patching the SVG.
