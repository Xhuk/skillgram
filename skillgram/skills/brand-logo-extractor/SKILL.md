---
name: brand-logo-extractor
description: Use when replicating, extracting, or adapting production brand assets, favicons, and daylight variants from an existing logo image.
metadata:
  origin: authored
---

# Brand logo extractor

Replicates, adapts, and exports production-grade brand assets from an existing raster logo (PNG, JPG, WebP) without degrading the original design into primitive geometric clipart.

Existing brand marks carry bespoke typography, subtle bevels, organic curves, and optical balancing that generic vectorizers and AI diffusion models destroy. This skill preserves 100% of the authentic artwork while generating the complete responsive asset ladder (favicons, touch-icons, PWA icons, header lockups, and Open Graph banners) for both Dark and Daylight themes.

## Activation Contract

**Use when:**
- a user provides an existing logo image and requires production assets (favicon.ico, apple-touch-icon, PWA icons, header lockup, og-image);
- an existing dark-mode logo washes out on white or daylight surfaces and requires a high-contrast adaptation without altering its core identity;
- extracting an emblem or custom wordmark from a solid background into clean anti-aliased transparency without dark fringe or halos.

**Not for:**
- designing a brand from scratch with no visual starting point (use `logo-design` for conceptual exploration);
- replacing polished client artwork with crude synthetic geometric primitives (rectangles, standard circles, or system sans-serif fonts);
- pure photo editing, background removal for portraits, or non-logo graphic design.

## Hard Rules

1. **Never substitute authentic artwork with synthetic primitives.** Bespoke lettering and organic curves must be preserved from the source artwork. Never replace custom wordmark glyphs with generic system fonts.
2. **Alpha extraction must be un-premultiplied.** Always restore pristine foreground colors using `(pixel - (1 - alpha) * bg) / alpha`. Raw thresholding leaves black fringe on light backgrounds.
3. **Daylight adaptations must use continuous mathematical HSV shifts.** Shift hue towards saturated, readable tones (e.g. mint green to deep emerald `#047857`, contrast ratio > 4.5:1 WCAG AAA) and scale value smoothly. Never replace shapes with flat monochrome blocks.
4. **Preserve custom wordmark segmentation.** When a wordmark features multi-colored words, segment them along their optical boundary and grade each token individually (e.g. first word in obsidian `#0f172a`, second in deep accent `#047857`).
5. **Enforce the complete production asset ladder.** Every extraction must deliver:
   - `favicon.ico` (multi-size 16x16, 32x32, 48x48) + standalone PNGs;
   - `apple-touch-icon.png` (180x180, solid plate, 18% safe padding);
   - `icon-192.png`, `icon-512.png`, and `icon-512-maskable.png` (25% safe padding);
   - `logo-lockup.png` and `@2x.png` (~3.5:1 horizontal ratio, 420x120 and 840x240);
   - `og-image.png` (1200x630, 1.91:1 ratio) with ambient lighting and metadata.
6. **No fake white knockouts.** Transparent holes (such as keyholes, negative space, or ring apertures) must be real transparent cutouts so the underlying surface shows through cleanly.

## Decision Gates

| Observation | Action |
|---|---|
| Source has dark background and pale neon accents | Extract dark master with un-premultiplied alpha, then apply HSV luminance shift for Daylight mode. |
| Logo features stacked layout but site needs header | Separate emblem and wordmark bounding boxes, then compose a horizontal lockup with ~3.5:1 ratio. |
| Color contrast on white background is < 4.5:1 | Shift lightness down by 40-50% in HSV space and boost saturation by 25-35% until WCAG AAA is met. |
| iOS Safari home screen bookmark required | Use solid opaque background plate (no alpha). Apple touch icons on transparent backgrounds render with black boxes. |
| Android launcher adaptive icon required | Export maskable icon with 25% outer padding to prevent circular or squircle clipping. |

## Execution Steps

1. **Inspect source dimensions and sample background color:**
   Read corners of the master image to calculate the median background RGB value.
   ```python
   import numpy as np
   from PIL import Image

   im = Image.open("master-logo.png").convert("RGB")
   arr = np.array(im, dtype=np.float32)
   bg = np.median(arr[:30, :30], axis=(0, 1))
   ```

2. **Compute un-premultiplied anti-aliased alpha matte:**
   Calculate Euclidean color distance from background and restore foreground values.
   ```python
   diff = np.linalg.norm(arr - bg, axis=2)
   alpha = np.clip((diff - 16.0) / (48.0 - 16.0), 0.0, 1.0)
   fg = np.zeros_like(arr)
   mask = alpha > 0.02
   fg[mask] = np.clip((arr[mask] - (1.0 - alpha[mask, None]) * bg) / alpha[mask, None], 0.0, 255.0)
   rgba = np.dstack([fg, alpha * 255.0]).astype(np.uint8)
   ```

3. **Separate emblem and wordmark bounding boxes:**
   Calculate horizontal and vertical pixel projection profiles to detect the optical gap dividing the mark from the text.

4. **Grade Daylight variant in HSV space:**
   Convert RGB to HSV, locate low-contrast pastel tones, shift hue to deep jewel tones, and reduce value by 45-50% for high contrast against pure white `#ffffff`.

5. **Render and export production asset matrix:**
   Resize with Lanczos resampling into exact standard sizes: 16, 32, 48, 180, 192, 512, 840x240, and 1200x630.

6. **Generate web manifest and HTML head snippet:**
   Output `site.webmanifest` and `meta-tags.html` referencing theme color, icons, and social tags.

## Output Contract

Report the operation using this exact table format:

```text
Asset                  Dimensions       Ratio    Mode       Surface / Target
----------------------------------------------------------------------------------
favicon.ico            16/32/48 multi   1:1      Dark/Day   Browser tabs
apple-touch-icon.png   180x180          1:1      Solid      iOS home screen & Safari
icon-192.png           192x192          1:1      Solid      Android PWA launcher
icon-512-maskable.png  512x512          1:1      Solid      Android adaptive squircle
logo-lockup@2x.png     840x240          3.5:1    Alpha      Site header & navbar
og-image.png           1200x630         1.91:1   Solid      Social cards (LinkedIn, X)
```
