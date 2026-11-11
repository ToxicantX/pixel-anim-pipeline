"""Robust sprite preprocessing utilities for the pixel-anim pipeline.

Public API
----------
hex_to_rgb(value) -> (r, g, b)
remove_bg_and_despill(image) -> RGBA Image
clean_orphan_pixels(image) -> RGBA Image
add_1px_outline(image, color=(0, 0, 0)) -> RGBA Image
process_sprite(input_path, output_path, canvas_size=32, max_colors=16,
               add_outline=False, use_endesga=False) -> dict

The pipeline removes a #FF00FF (magenta) background with an edge-aware /
border-connected keyer, despills the surviving silhouette fringe, optionally
cleans isolated noise pixels, crops to the visible bounding box, nearest-neighbour
resizes, quantizes the visible pixels, and bottom-center composites the sprite
on a square RGBA canvas.  The final opaque bottom row is always canvas_size - 2,
including when a 1px outline is requested.

CLI
---
    python scripts/pixel_processor.py input.png output.png --size 32 --colors 16 [--outline] [--endesga]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from typing import Sequence, Tuple

import numpy as np
from PIL import Image

__all__ = [
    "BLACK",
    "MAGENTA",
    "ENDESGA_32_HEX",
    "ENDESGA_PALETTE",
    "MIN_CANVAS_SIZE",
    "MAX_PALETTE_COLORS",
    "hex_to_rgb",
    "remove_bg_and_despill",
    "clean_orphan_pixels",
    "add_1px_outline",
    "process_sprite",
    "main",
]

BLACK: Tuple[int, int, int] = (0, 0, 0)
MAGENTA: Tuple[int, int, int] = (255, 0, 255)

# Smallest canvas this module is willing to composite onto.
MIN_CANVAS_SIZE = 4
# Hard upper bound for the max_colors argument (8-bit palette).
MAX_PALETTE_COLORS = 256

# Endesga 32 (EDG32) palette.  Only real, opaque colours - no black padding.
ENDESGA_32_HEX: Tuple[str, ...] = (
    "#be4a2f",
    "#d77643",
    "#ead4aa",
    "#e4a672",
    "#b86f50",
    "#733e39",
    "#3e2731",
    "#a22633",
    "#e43b44",
    "#f77622",
    "#feae34",
    "#fee761",
    "#63c74d",
    "#3e8948",
    "#265c42",
    "#193c3e",
    "#124e89",
    "#0099db",
    "#2ce8f5",
    "#ffffff",
    "#c0cbdc",
    "#8b9bb4",
    "#5a6988",
    "#3a4466",
    "#262b44",
    "#181425",
    "#ff0044",
    "#68386c",
    "#b55088",
    "#f6757a",
    "#e8b796",
    "#c28569",
)

# Thresholds for the magenta keyer.  A pixel is core background when it is
# strongly magenta, and fringe background when it carries a heavy magenta
# cast (typical of anti-aliased edges).  Interior purple is intentionally not
# matched: it lacks the required brightness on both red and blue.
_CORE_MIN_RB = 200
_CORE_MAX_G = 70
_FRINGE_MIN_RB = 165
_FRINGE_MAX_G = 150
_FRINGE_MAX_SKEW = 80
_FRINGE_MIN_EXCESS = 40

# Despill only touches the 1px fringe around removed background and only when
# the colour is magenta-leaning and bright (so dark/purple interiors survive).
_DESPILL_MIN_RB = 160
_DESPILL_MAX_SKEW = 75

_ALPHA_THRESHOLD = 128


def hex_to_rgb(value: str) -> Tuple[int, int, int]:
    """Convert '#RRGGBB' / 'RRGGBB' / '#RGB' / '0xRRGGBB' to a tuple.

    Raises ValueError for anything that is not a well-formed hex colour.
    """
    if not isinstance(value, str):
        raise ValueError(f"hex colour must be a string, got {type(value).__name__}")
    text = value.strip()
    if text[:1] == "#":
        text = text[1:]
    if text[:2].lower() == "0x":
        text = text[2:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"invalid hex colour {value!r}: expected 3 or 6 digits")
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError as exc:
        raise ValueError(f"invalid hex colour {value!r}") from exc
    return (r, g, b)


# Numpy palette built from the real Endesga colours (shape (32, 3), uint8).
ENDESGA_PALETTE: np.ndarray = np.array(
    [hex_to_rgb(h) for h in ENDESGA_32_HEX], dtype=np.uint8
)


def _normalize_color(color) -> Tuple[int, int, int]:
    """Accept (r, g, b) or a hex string and return a validated RGB tuple."""
    if isinstance(color, str):
        rgb = hex_to_rgb(color)
    else:
        try:
            channels = tuple(color)
        except TypeError as exc:
            raise ValueError(f"invalid colour {color!r}") from exc
        if len(channels) != 3:
            raise ValueError(f"invalid colour {color!r}: expected 3 channels")
        rgb = tuple(int(channel) for channel in channels)
    if any(channel < 0 or channel > 255 for channel in rgb):
        raise ValueError(f"invalid colour {color!r}: channels must be 0..255")
    return rgb  # type: ignore[return-value]


def _neighbor_any(mask: np.ndarray) -> np.ndarray:
    """Boolean 4-neighbourhood dilation (keeps the original mask)."""
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def _neighbor_count(mask: np.ndarray) -> np.ndarray:
    """Count in-bounds 4-neighbours for every True cell."""
    count = np.zeros(mask.shape, dtype=np.int32)
    count[1:, :] += mask[:-1, :]
    count[:-1, :] += mask[1:, :]
    count[:, 1:] += mask[:, :-1]
    count[:, :-1] += mask[:, 1:]
    return count


def _flood_from_border(mask: np.ndarray) -> np.ndarray:
    """Return the subset of mask connected to the image border (4-connected)."""
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    if height == 0 or width == 0:
        return visited
    queue: "deque[Tuple[int, int]]" = deque()

    def _seed(y: int, x: int) -> None:
        if mask[y, x] and not visited[y, x]:
            visited[y, x] = True
            queue.append((y, x))

    for x in range(width):
        _seed(0, x)
        if height > 1:
            _seed(height - 1, x)
    for y in range(height):
        _seed(y, 0)
        if width > 1:
            _seed(y, width - 1)

    while queue:
        y, x = queue.popleft()
        if y > 0:
            _seed(y - 1, x)
        if y + 1 < height:
            _seed(y + 1, x)
        if x > 0:
            _seed(y, x - 1)
        if x + 1 < width:
            _seed(y, x + 1)
    return visited


def remove_bg_and_despill(image: Image.Image) -> Image.Image:
    """Remove a #FF00FF background and despill the resulting silhouette.

    The keyer is edge-aware: only magenta-ish pixels connected to the border
    are treated as background, so legitimate purple interiors are preserved.
    Alpha is normalised to strictly 0 or 255 and fully transparent pixels have
    their RGB set to (0, 0, 0).
    """
    if not isinstance(image, Image.Image):
        raise TypeError("remove_bg_and_despill expects a PIL Image")
    rgba = image.convert("RGBA")
    arr = np.array(rgba, dtype=np.uint8).copy()

    # Binarise any pre-existing soft alpha so the result is strictly 0/255.
    alpha = arr[..., 3]
    if np.any((alpha != 0) & (alpha != 255)):
        arr[..., 3] = np.where(alpha >= _ALPHA_THRESHOLD, 255, 0).astype(np.uint8)

    rgb = arr[..., :3].astype(np.int32)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    min_rb = np.minimum(red, blue)
    max_rb = np.maximum(red, blue)

    core = (red >= _CORE_MIN_RB) & (green <= _CORE_MAX_G) & (blue >= _CORE_MIN_RB)
    fringe = (
        (min_rb >= _FRINGE_MIN_RB)
        & (green <= _FRINGE_MAX_G)
        & ((max_rb - min_rb) <= _FRINGE_MAX_SKEW)
        & ((min_rb - green) >= _FRINGE_MIN_EXCESS)
    )
    transparent = arr[..., 3] == 0
    candidates = core | fringe | transparent

    removed = _flood_from_border(candidates)
    if removed.any():
        arr[..., 3][removed] = 0

    # Despill the surviving ring that borders removed background.
    if removed.any():
        ring = _neighbor_any(removed) & ~removed & (arr[..., 3] > 0)
        if ring.any():
            magenta_excess = min_rb - green
            despill = (
                ring
                & (min_rb >= _DESPILL_MIN_RB)
                & ((max_rb - min_rb) <= _DESPILL_MAX_SKEW)
                & (magenta_excess > 0)
            )
            if despill.any():
                arr[..., 0][despill] = np.clip(
                    red[despill] - magenta_excess[despill], 0, 255
                ).astype(np.uint8)
                arr[..., 2][despill] = np.clip(
                    blue[despill] - magenta_excess[despill], 0, 255
                ).astype(np.uint8)

    # Normalise transparent RGB.
    arr[..., :3][arr[..., 3] == 0] = 0
    return Image.fromarray(arr, "RGBA")


def clean_orphan_pixels(image: Image.Image) -> Image.Image:
    """Remove opaque pixels that have no opaque 4-neighbour (including edges)."""
    if not isinstance(image, Image.Image):
        raise TypeError("clean_orphan_pixels expects a PIL Image")
    rgba = image.convert("RGBA")
    arr = np.array(rgba, dtype=np.uint8).copy()
    opaque = arr[..., 3] > 0
    if not opaque.any():
        return rgba
    orphans = opaque & (_neighbor_count(opaque) == 0)
    if orphans.any():
        arr[..., 3][orphans] = 0
        arr[..., :3][orphans] = 0
    return Image.fromarray(arr, "RGBA")


def add_1px_outline(image: Image.Image, color=BLACK) -> Image.Image:
    """Draw a 1px, 4-neighbour outline around opaque pixels (same canvas size).

    The outline is only painted on already transparent pixels, so existing
    sprite pixels are never overwritten.  Callers are responsible for leaving a
    transparent margin; process_sprite reserves exactly 1px for this.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("add_1px_outline expects a PIL Image")
    rgb = _normalize_color(color)
    rgba = image.convert("RGBA")
    arr = np.array(rgba, dtype=np.uint8).copy()
    opaque = arr[..., 3] > 0
    if not opaque.any():
        return rgba
    ring = _neighbor_any(opaque) & ~opaque
    if ring.any():
        arr[..., 0][ring] = rgb[0]
        arr[..., 1][ring] = rgb[1]
        arr[..., 2][ring] = rgb[2]
        arr[..., 3][ring] = 255
    return Image.fromarray(arr, "RGBA")


def _opaque_bbox(arr: np.ndarray) -> Tuple[int, int, int, int]:
    ys, xs = np.nonzero(arr[..., 3] > 0)
    if ys.size == 0:
        raise ValueError("image contains no visible pixels")
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _nearest_index(pixels: np.ndarray, palette: np.ndarray) -> np.ndarray:
    """Index of the nearest palette entry for each pixel (int64 math)."""
    px = pixels.astype(np.int64, copy=False)
    pal = palette.astype(np.int64, copy=False)
    diff = px[:, None, :] - pal[None, :, :]
    # Squaring in int64 avoids the classic int16 overflow for bright colours.
    dist = np.einsum("npk,npk->np", diff, diff)
    return np.argmin(dist, axis=1)


def _remap_visible(arr: np.ndarray, palette: np.ndarray) -> np.ndarray:
    visible = arr[..., 3] > 0
    if not visible.any():
        return arr
    pixels = arr[..., :3][visible]
    indices = _nearest_index(pixels, palette)
    arr[..., :3][visible] = palette[indices]
    return arr


def _quantize_median(arr: np.ndarray, max_colors: int) -> np.ndarray:
    """Median-cut quantize only the visible pixels to max_colors colours."""
    visible = arr[..., 3] > 0
    if not visible.any():
        return arr
    pixels = arr[..., :3][visible]
    unique = np.unique(pixels, axis=0)
    if len(unique) <= max_colors:
        return arr
    if max_colors <= 1:
        values, counts = np.unique(pixels, axis=0, return_counts=True)
        dominant = values[int(np.argmax(counts))]
        arr[..., :3][visible] = dominant
        return arr
    strip = Image.fromarray(pixels.reshape(-1, 1, 3), "RGB")
    quantized = strip.quantize(
        colors=int(max_colors),
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    indices = np.asarray(quantized).reshape(-1).astype(np.int64)
    # Only trust palette entries that are actually referenced: PIL pads the
    # palette with zeroes, which would otherwise map to accidental black.
    used_max = int(indices.max())
    flat_palette = np.asarray(quantized.getpalette(), dtype=np.uint8)
    palette = flat_palette[: 3 * (used_max + 1)].reshape(-1, 3)
    arr[..., :3][visible] = palette[indices]
    return arr


def _reduce_to_limit(arr: np.ndarray, limit: int) -> np.ndarray:
    """Collapse the visible palette to at most limit colours (by frequency)."""
    visible = arr[..., 3] > 0
    if not visible.any():
        return arr
    pixels = arr[..., :3][visible]
    unique, inverse, counts = np.unique(
        pixels, axis=0, return_inverse=True, return_counts=True
    )
    if len(unique) <= limit:
        return arr
    order = np.argsort(-counts, kind="stable")[:limit]
    kept = unique[order]
    nearest = _nearest_index(unique, kept)
    arr[..., :3][visible] = kept[nearest][inverse]
    return arr


def _anchor_position(
    width: int, height: int, canvas_size: int, outline: bool
) -> Tuple[int, int]:
    """Bottom-center anchor for a block of width/height plus optional outline.

    The block's bottom row lands on canvas_size - 2, so the final opaque bottom
    row is always canvas_size - 2 whether or not an outline is drawn.
    """
    outline_pad = 1 if outline else 0
    block_w = width + 2 * outline_pad
    block_h = height + 2 * outline_pad
    paste_x = (canvas_size - block_w) // 2 + outline_pad
    block_top = canvas_size - 1 - block_h
    paste_y = block_top + outline_pad
    return paste_x, paste_y


def _layout(
    content_w: int, content_h: int, canvas_size: int, outline: bool
) -> Tuple[int, int, int, int]:
    """Return (new_w, new_h, paste_x, paste_y) for the content.

    Reservation: 3px left/right, 4px top, 1px bottom.  When an outline is
    requested an extra 1px is reserved on every side so the outline lives inside
    the padding and the final opaque bottom row stays at canvas_size - 2.
    """
    side = 3
    top = 4
    bottom = 1
    if canvas_size < 16:
        side = min(side, max(0, (canvas_size - 2) // 4))
        top = min(top, max(0, canvas_size // 4))

    outline_pad = 1 if outline else 0
    block_max_w = canvas_size - 2 * side
    block_max_h = canvas_size - top - bottom
    content_max_w = block_max_w - 2 * outline_pad
    content_max_h = block_max_h - 2 * outline_pad

    if content_max_w < 1:
        side = 0
        content_max_w = max(1, canvas_size - 2 * outline_pad)
    if content_max_h < 1:
        top = 0
        content_max_h = max(1, canvas_size - bottom - 2 * outline_pad)

    scale = min(content_max_w / content_w, content_max_h / content_h)
    new_w = min(content_max_w, max(1, int(round(content_w * scale))))
    new_h = min(content_max_h, max(1, int(round(content_h * scale))))

    paste_x, paste_y = _anchor_position(new_w, new_h, canvas_size, outline)
    return new_w, new_h, paste_x, paste_y


def _visible_color_count(arr: np.ndarray) -> int:
    visible = arr[..., 3] > 0
    if not visible.any():
        return 0
    return int(len(np.unique(arr[..., :3][visible], axis=0)))


def process_sprite(
    input_path,
    output_path,
    canvas_size: int = 32,
    max_colors: int = 16,
    add_outline: bool = False,
    use_endesga: bool = False,
) -> dict:
    """Preprocess one sprite and write it to output_path.

    Parameters
    ----------
    input_path, output_path:
        Paths (str or path-like) to the source and destination images.
    canvas_size:
        Side length of the square output canvas (default 32).  Must be >= 4.
    max_colors:
        Maximum number of visible colours in the output, including a pure black
        outline when one is requested.  Must be in 1..256.
    add_outline:
        Draw a 1px pure black 4-neighbour outline inside the reserved padding.
    use_endesga:
        Quantize to the Endesga 32 palette instead of median cut.

    Returns
    -------
    dict
        Statistics about the conversion.
    """
    input_path = os.fspath(input_path)
    output_path = os.fspath(output_path)
    canvas_size = int(canvas_size)
    max_colors = int(max_colors)

    if canvas_size < MIN_CANVAS_SIZE:
        raise ValueError(f"canvas_size must be >= {MIN_CANVAS_SIZE}, got {canvas_size}")
    if not 1 <= max_colors <= MAX_PALETTE_COLORS:
        raise ValueError(
            f"max_colors must be between 1 and {MAX_PALETTE_COLORS}, got {max_colors}"
        )

    with Image.open(input_path) as source:
        base = source.convert("RGBA")
    base_arr = np.array(base, dtype=np.uint8)

    keyed = remove_bg_and_despill(base)
    keyed_arr = np.array(keyed, dtype=np.uint8)
    if not (keyed_arr[..., 3] > 0).any():
        raise ValueError("image contains no visible pixels after background removal")

    cleaned = clean_orphan_pixels(keyed)
    cleaned_arr = np.array(cleaned, dtype=np.uint8)
    if not (cleaned_arr[..., 3] > 0).any():
        # Never let noise removal erase the only content.
        cleaned = keyed
        cleaned_arr = keyed_arr

    bbox = _opaque_bbox(cleaned_arr)
    content = cleaned.crop(bbox)
    content_w, content_h = content.size

    new_w, new_h, paste_x, paste_y = _layout(
        content_w, content_h, canvas_size, bool(add_outline)
    )
    resized = content.resize((new_w, new_h), Image.Resampling.NEAREST)
    # Nearest downsampling can drop extreme rows/columns (e.g. a narrow terminal
    # tip), so re-crop to the *actual* opaque bbox and anchor from real sizes.
    # Otherwise the advertised baseline (canvas_size - 2) would be wrong.
    resized_arr = np.array(resized, dtype=np.uint8)
    actual_bbox = _opaque_bbox(resized_arr)
    if actual_bbox != (0, 0, new_w, new_h):
        resized = resized.crop(actual_bbox)
    actual_w, actual_h = resized.size
    paste_x, paste_y = _anchor_position(
        actual_w, actual_h, canvas_size, bool(add_outline)
    )

    quantized = np.array(resized, dtype=np.uint8).copy()
    # A pure black outline consumes one palette slot; with max_colors == 1 there
    # is no slot left for the subject, so render a pure black silhouette.
    sprite_limit = max_colors - (1 if add_outline else 0)
    if sprite_limit <= 0:
        quantized[..., :3][quantized[..., 3] > 0] = 0
    else:
        if use_endesga:
            quantized = _remap_visible(quantized, ENDESGA_PALETTE)
        else:
            quantized = _quantize_median(quantized, sprite_limit)
        quantized = _reduce_to_limit(quantized, sprite_limit)
    quantized[..., :3][quantized[..., 3] == 0] = 0
    sprite = Image.fromarray(quantized, "RGBA")

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    canvas.paste(sprite, (paste_x, paste_y))
    if add_outline:
        canvas = add_1px_outline(canvas, BLACK)
    # Final guarantee: transparent RGB normalisation across the whole canvas.
    final_arr = np.array(canvas, dtype=np.uint8).copy()
    final_arr[..., :3][final_arr[..., 3] == 0] = 0
    result = Image.fromarray(final_arr, "RGBA")

    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    extension = os.path.splitext(output_path)[1].lower()
    save_format = {
        ".png": "PNG",
        ".webp": "WEBP",
        ".bmp": "BMP",
        ".tga": "TGA",
    }.get(extension, "PNG")
    result.save(output_path, format=save_format)

    result_arr = np.array(result, dtype=np.uint8)
    alpha = result_arr[..., 3]
    visible = alpha > 0
    ys, xs = np.nonzero(visible)
    removed_background = int(
        np.count_nonzero((base_arr[..., 3] > 0) & (keyed_arr[..., 3] == 0))
    )
    removed_noise = int(
        np.count_nonzero((keyed_arr[..., 3] > 0) & (cleaned_arr[..., 3] == 0))
    )

    return {
        "input_path": input_path,
        "output_path": output_path,
        "canvas_size": canvas_size,
        "max_colors": max_colors,
        "add_outline": bool(add_outline),
        "use_endesga": bool(use_endesga),
        "input_size": [int(base.width), int(base.height)],
        "background_pixels_removed": removed_background,
        "noise_pixels_removed": removed_noise,
        "cropped_size": [int(content_w), int(content_h)],
        "resized_size": [int(actual_w), int(actual_h)],
        "opaque_pixels": int(np.count_nonzero(visible)),
        "colors_used": _visible_color_count(result_arr),
        "top_y": int(ys.min()) if ys.size else -1,
        "bottom_y": int(ys.max()) if ys.size else -1,
        "left_x": int(xs.min()) if xs.size else -1,
        "right_x": int(xs.max()) if xs.size else -1,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.  Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Preprocess a sprite: remove magenta background, clean, "
        "quantize and bottom-center it on a square canvas."
    )
    parser.add_argument("input", help="input image path")
    parser.add_argument("output", help="output image path")
    parser.add_argument(
        "--size",
        type=int,
        default=32,
        dest="canvas_size",
        help="square canvas side length (default: 32)",
    )
    parser.add_argument(
        "--colors",
        type=int,
        default=16,
        dest="max_colors",
        help="maximum visible colours (default: 16)",
    )
    parser.add_argument(
        "--outline",
        action="store_true",
        help="add a 1px pure black outline",
    )
    parser.add_argument(
        "--endesga",
        action="store_true",
        help="quantize to the Endesga 32 palette",
    )
    args = parser.parse_args(argv)

    try:
        stats = process_sprite(
            args.input,
            args.output,
            canvas_size=args.canvas_size,
            max_colors=args.max_colors,
            add_outline=args.outline,
            use_endesga=args.endesga,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
