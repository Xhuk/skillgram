import os
import sys
import argparse
import colorsys
from PIL import Image
import numpy as np

def extract_brand_suite(source_path, brand_name, out_dir="assets", emblem_bbox=None, wordmark_bbox=None):
    os.makedirs(out_dir, exist_ok=True)
    src = Image.open(source_path).convert("RGB")
    arr = np.array(src, dtype=np.float32)
    
    # Background estimation from border pixels
    outer_strip = np.concatenate([
        arr[:20, :, :].reshape(-1, 3), arr[-20:, :, :].reshape(-1, 3),
        arr[:, :20, :].reshape(-1, 3), arr[:, -20:, :].reshape(-1, 3)
    ], axis=0)
    bg = np.median(outer_strip, axis=0)
    DARK_BG = (int(bg[0]), int(bg[1]), int(bg[2]))
    WHITE_BG = (255, 255, 255)
    
    # Anti-aliased un-premultiplied alpha
    diff = np.linalg.norm(arr - bg, axis=2)
    alpha = np.clip((diff - 16.0) / (48.0 - 16.0), 0.0, 1.0)
    fg = np.zeros_like(arr)
    mask = alpha > 0.02
    fg[mask] = np.clip((arr[mask] - (1.0 - alpha[mask, None]) * bg) / alpha[mask, None], 0.0, 255.0)
    master_rgba = Image.fromarray(np.dstack([fg, alpha * 255.0]).astype(np.uint8), "RGBA")
    
    if emblem_bbox:
        y0, y1, x0, x1 = emblem_bbox
        emblem_dark = master_rgba.crop((x0, y0, x1, y1))
    else:
        # Default auto split for stacked emblem + wordmark
        h_half = int(arr.shape[0] * 0.58)
        emblem_dark = master_rgba.crop((0, 0, arr.shape[1], h_half))
    
    if wordmark_bbox:
        y0, y1, x0, x1 = wordmark_bbox
        wordmark_dark = master_rgba.crop((x0, y0, x1, y1))
    else:
        h_half = int(arr.shape[0] * 0.58)
        wordmark_dark = master_rgba.crop((0, h_half, arr.shape[1], arr.shape[0]))
        
    # Daylight HSV Shift for Emblem
    em_arr = np.array(emblem_dark, dtype=np.float32)
    em_rgb = em_arr[:, :, :3] / 255.0
    em_a = em_arr[:, :, 3] / 255.0
    day_em_rgb = np.zeros_like(em_rgb)
    for y in range(emblem_dark.height):
        for x in range(emblem_dark.width):
            if em_a[y, x] > 0.01:
                h, s, v = colorsys.rgb_to_hsv(em_rgb[y, x, 0], em_rgb[y, x, 1], em_rgb[y, x, 2])
                if 0.35 <= h <= 0.47: # Green/mint -> deep emerald
                    day_em_rgb[y, x] = colorsys.hsv_to_rgb(0.43, min(1.0, s * 1.3), v * 0.52)
                elif 0.47 < h <= 0.58: # Cyan -> deep cyan
                    day_em_rgb[y, x] = colorsys.hsv_to_rgb(0.54, min(1.0, s * 1.35), v * 0.72)
                else:
                    day_em_rgb[y, x] = colorsys.hsv_to_rgb(h, s, v * 0.6)
    emblem_day = Image.fromarray(np.dstack([day_em_rgb * 255.0, em_a * 255.0]).astype(np.uint8), "RGBA")
    
    # Wordmark Daylight Shift
    wm_arr = np.array(wordmark_dark, dtype=np.float32)
    wm_rgb = wm_arr[:, :, :3] / 255.0
    wm_a = wm_arr[:, :, 3] / 255.0
    day_wm_rgb = np.zeros_like(wm_rgb)
    split_x = int(wordmark_dark.width * 0.5)
    for y in range(wordmark_dark.height):
        for x in range(wordmark_dark.width):
            if wm_a[y, x] > 0.02:
                if x < split_x:
                    day_wm_rgb[y, x] = np.array([15.0/255.0, 23.0/255.0, 42.0/255.0])
                else:
                    v = wm_rgb[y, x, 1]
                    day_wm_rgb[y, x] = np.array([4.0/255.0 * v, 120.0/255.0 * v, 87.0/255.0 * v])
    wordmark_day = Image.fromarray(np.dstack([day_wm_rgb * 255.0, wm_a * 255.0]).astype(np.uint8), "RGBA")
    
    # Render Assets
    def plate(size, img, bg_col, pad=0.18):
        canvas = Image.new("RGBA", (size, size), (*bg_col, 255))
        inner = int(size * (1.0 - 2 * pad))
        res = img.resize((inner, inner), Image.Resampling.LANCZOS)
        canvas.paste(res, ((size - inner)//2, (size - inner)//2), res)
        return canvas
        
    def trans(size, img, pad=0.08):
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        inner = int(size * (1.0 - 2 * pad))
        res = img.resize((inner, inner), Image.Resampling.LANCZOS)
        canvas.paste(res, ((size - inner)//2, (size - inner)//2), res)
        return canvas

    # Favicons
    trans(16, emblem_dark, 0.02).save(os.path.join(out_dir, "favicon-16x16.png"))
    trans(32, emblem_dark, 0.05).save(os.path.join(out_dir, "favicon-32x32.png"))
    trans(48, emblem_dark, 0.05).save(os.path.join(out_dir, "favicon-48x48.png"))
    emblem_dark.save(os.path.join(out_dir, "favicon.ico"), format="ICO", sizes=[(16,16), (32,32), (48,48)])
    
    # Apple Touch & PWA
    plate(180, emblem_dark, DARK_BG, 0.18).convert("RGB").save(os.path.join(out_dir, "apple-touch-icon.png"))
    plate(180, emblem_day, WHITE_BG, 0.18).convert("RGB").save(os.path.join(out_dir, "apple-touch-icon-daylight.png"))
    plate(192, emblem_dark, DARK_BG, 0.18).save(os.path.join(out_dir, "icon-192.png"))
    plate(512, emblem_dark, DARK_BG, 0.18).save(os.path.join(out_dir, "icon-512.png"))
    plate(512, emblem_dark, DARK_BG, 0.25).save(os.path.join(out_dir, "icon-512-maskable.png"))
    
    # Lockups (3.5:1 ratio)
    def lockup(width, height, icon_img, wm_img, bg_val=None):
        canvas = Image.new("RGBA", (width, height), bg_val if bg_val else (0,0,0,0))
        ih = int(height * 0.68)
        iw = ih
        res_i = icon_img.resize((iw, ih), Image.Resampling.LANCZOS)
        wh = int(height * 0.38)
        ww = int(wm_img.width * (wh / wm_img.height))
        res_w = wm_img.resize((ww, wh), Image.Resampling.LANCZOS)
        gap = int(height * 0.10)
        sx = (width - (iw + gap + ww)) // 2
        canvas.paste(res_i, (sx, (height - ih)//2), res_i)
        canvas.paste(res_w, (sx + iw + gap, (height - wh)//2), res_w)
        return canvas
        
    lockup(840, 240, emblem_dark, wordmark_dark).save(os.path.join(out_dir, "logo-lockup@2x.png"))
    lockup(420, 120, emblem_dark, wordmark_dark).save(os.path.join(out_dir, "logo-lockup.png"))
    lockup(840, 240, emblem_day, wordmark_day).save(os.path.join(out_dir, "logo-lockup-daylight@2x.png"))
    lockup(420, 120, emblem_day, wordmark_day).save(os.path.join(out_dir, "logo-lockup-daylight.png"))
    
    print(f"Brand asset suite for '{brand_name}' successfully generated in '{out_dir}/'.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract production brand assets from master logo image.")
    parser.add_argument("--source", required=True, help="Path to master logo image")
    parser.add_argument("--name", default="Brand", help="Brand name")
    parser.add_argument("--out-dir", default="assets", help="Output directory")
    args = parser.parse_args()
    extract_brand_suite(args.source, args.name, args.out_dir)
