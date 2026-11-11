"""Focused tests for scripts/pixel_processor.py (no integration concerns)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pixel_processor as pp  # noqa: E402


MAGENTA = (255, 0, 255)
PURPLE = (128, 0, 128)


def make_magenta_sprite(size=(24, 24), box=(6, 6, 17, 17), fill=PURPLE):
    """Return an RGB image with a magenta background and a solid rectangle."""
    image = Image.new("RGB", size, MAGENTA)
    ImageDraw.Draw(image).rectangle(box, fill=fill)
    return image


def visible_colors(arr):
    visible = arr[..., 3] > 0
    if not visible.any():
        return np.empty((0, 3), dtype=np.uint8)
    return np.unique(arr[..., :3][visible], axis=0)


class HexToRgbTests(unittest.TestCase):
    def test_full_hex_with_hash(self):
        self.assertEqual(pp.hex_to_rgb("#FF00FF"), (255, 0, 255))

    def test_bare_and_short_and_prefixed_hex(self):
        self.assertEqual(pp.hex_to_rgb("ff00ff"), (255, 0, 255))
        self.assertEqual(pp.hex_to_rgb("#f0f"), (255, 0, 255))
        self.assertEqual(pp.hex_to_rgb("0x102030"), (16, 32, 48))

    def test_invalid_hex_raises(self):
        for bad in ("#GG0000", "#12345", "", "not-a-color", None):
            with self.assertRaises(ValueError):
                pp.hex_to_rgb(bad)


class ProcessSpriteTests(unittest.TestCase):
    def process(self, image, **kwargs):
        temp_dir = tempfile.mkdtemp()
        input_path = os.path.join(temp_dir, "in.png")
        output_path = os.path.join(temp_dir, "out.png")
        image.save(input_path)
        stats = pp.process_sprite(input_path, output_path, **kwargs)
        result = Image.open(output_path).convert("RGBA")
        result.load()
        return stats, np.array(result)

    def test_baseline_geometry_palette_and_alpha(self):
        stats, arr = self.process(make_magenta_sprite(), canvas_size=32, max_colors=16)
        self.assertEqual(arr.shape, (32, 32, 4))

        alpha = arr[..., 3]
        self.assertTrue(set(np.unique(alpha)).issubset({0, 255}))

        ys, xs = np.nonzero(alpha > 0)
        self.assertEqual(int(ys.max()), 30)  # canvas_size - 2
        self.assertGreaterEqual(int(ys.min()), 4)
        self.assertGreaterEqual(int(xs.min()), 3)
        self.assertLessEqual(int(xs.max()), 28)

        colors = visible_colors(arr)
        self.assertLessEqual(len(colors), 16)
        self.assertEqual(tuple(arr[16, 16, :3]), PURPLE)

        # Magenta background is gone: no strongly magenta visible pixel.
        magenta = (
            (arr[..., 0] >= 200) & (arr[..., 1] <= 70) & (arr[..., 2] >= 200)
            & (alpha > 0)
        )
        self.assertFalse(magenta.any())

        self.assertEqual(stats["bottom_y"], 30)
        self.assertGreater(stats["background_pixels_removed"], 0)

    def test_outline_keeps_baseline_and_is_pure_black(self):
        stats, arr = self.process(
            make_magenta_sprite(), canvas_size=32, max_colors=16, add_outline=True
        )
        alpha = arr[..., 3]
        ys, _ = np.nonzero(alpha > 0)
        self.assertEqual(int(ys.max()), 30)

        black = (arr[..., 0] == 0) & (arr[..., 1] == 0) & (arr[..., 2] == 0) & (alpha > 0)
        self.assertTrue(black.any())
        self.assertLessEqual(len(visible_colors(arr)), 16)
        self.assertEqual(tuple(arr[16, 16, :3]), PURPLE)
        self.assertTrue(stats["add_outline"])

    def test_color_limit_is_enforced(self):
        height = width = 64
        gradient = np.zeros((height, width, 3), dtype=np.uint8)
        gradient[..., 0] = (np.arange(width, dtype=np.uint16) * 4).astype(np.uint8)[None, :]
        gradient[..., 1] = (np.arange(height, dtype=np.uint16) * 4).astype(np.uint8)[:, None]
        gradient[..., 2] = 128
        image = Image.fromarray(gradient, "RGB")

        stats, arr = self.process(image, canvas_size=32, max_colors=4)
        colors = visible_colors(arr)
        self.assertGreater(len(colors), 0)
        self.assertLessEqual(len(colors), 4)
        self.assertLessEqual(stats["colors_used"], 4)

    def test_empty_and_all_magenta_images_raise(self):
        temp_dir = tempfile.mkdtemp()
        empty_path = os.path.join(temp_dir, "empty.png")
        out_path = os.path.join(temp_dir, "out.png")
        Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(empty_path)
        with self.assertRaises(ValueError):
            pp.process_sprite(empty_path, out_path)

        magenta_path = os.path.join(temp_dir, "magenta.png")
        Image.new("RGB", (8, 8), MAGENTA).save(magenta_path)
        with self.assertRaises(ValueError):
            pp.process_sprite(magenta_path, out_path)

    def test_argument_bounds(self):
        stats, _ = self.process(make_magenta_sprite(), canvas_size=32, max_colors=8)
        self.assertEqual(stats["max_colors"], 8)
        with self.assertRaises(ValueError):
            self.process(make_magenta_sprite(), canvas_size=32, max_colors=0)
        with self.assertRaises(ValueError):
            self.process(make_magenta_sprite(), canvas_size=2, max_colors=8)

    def test_single_color_cap_with_outline_is_black_silhouette(self):
        stats, arr = self.process(
            make_magenta_sprite(),
            canvas_size=32,
            max_colors=1,
            add_outline=True,
        )
        alpha = arr[..., 3]
        colors = visible_colors(arr)
        self.assertEqual(len(colors), 1)
        self.assertEqual(tuple(colors[0]), (0, 0, 0))
        ys, _ = np.nonzero(alpha > 0)
        self.assertEqual(int(ys.max()), 30)
        self.assertLessEqual(stats["colors_used"], 1)

    def test_nearest_downsampling_keeps_exact_baseline(self):
        # Tall sprite whose only bottom-row content cannot be sampled when
        # nearest-neighbour downsampling 60 rows into 27.  The old code trusted
        # the requested resized height and floated above the baseline.
        image = Image.new("RGB", (40, 60), MAGENTA)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 39, 50), fill=PURPLE)
        draw.rectangle((0, 59, 1, 59), fill=PURPLE)

        stats, arr = self.process(image, canvas_size=32, max_colors=16)
        alpha = arr[..., 3]
        ys, _ = np.nonzero(alpha > 0)
        self.assertEqual(int(ys.max()), 30)
        self.assertEqual(stats["bottom_y"], 30)
        self.assertGreaterEqual(int(ys.min()), 4)

    def test_endesga_uses_real_palette_and_int_math(self):
        stats, arr = self.process(
            make_magenta_sprite(fill=(255, 255, 255)),
            canvas_size=32,
            max_colors=16,
            use_endesga=True,
        )
        colors = {tuple(int(c) for c in color) for color in visible_colors(arr)}
        palette = {tuple(int(c) for c in color) for color in pp.ENDESGA_PALETTE}
        self.assertTrue(colors.issubset(palette))
        self.assertIn((255, 255, 255), colors)  # exact match despite squaring
        self.assertNotIn((0, 0, 0), colors)  # no accidental padded black
        self.assertLessEqual(stats["colors_used"], 16)


class RemoverTests(unittest.TestCase):
    def test_magenta_removed_but_purple_interior_kept(self):
        image = make_magenta_sprite(size=(20, 20), box=(5, 5, 14, 14)).convert("RGBA")
        # Anti-aliased-ish magenta fringe pixel next to the sprite.
        image.putpixel((4, 4), (240, 90, 240, 255))

        result = np.array(pp.remove_bg_and_despill(image))

        self.assertEqual(result[0, 0, 3], 0)
        self.assertEqual(tuple(result[0, 0, :3]), (0, 0, 0))
        self.assertEqual(result[9, 9, 3], 255)
        self.assertEqual(tuple(result[9, 9, :3]), PURPLE)

        alpha = result[..., 3]
        self.assertTrue(set(np.unique(alpha)).issubset({0, 255}))
        magenta = (
            (result[..., 0] >= 200)
            & (result[..., 1] <= 70)
            & (result[..., 2] >= 200)
            & (alpha > 0)
        )
        self.assertFalse(magenta.any())
        # The fringe pixel was either keyed out or despilled away from magenta.
        self.assertNotEqual(tuple(result[4, 4, :3]), (240, 90, 240))

    def test_alpha_is_binarised(self):
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((2, 2, 3, 3), fill=(255, 255, 255, 100))
        ImageDraw.Draw(image).rectangle((5, 5, 6, 6), fill=(255, 255, 255, 220))
        result = np.array(pp.remove_bg_and_despill(image))
        self.assertTrue(set(np.unique(result[..., 3])).issubset({0, 255}))
        self.assertEqual(result[2, 2, 3], 0)  # below threshold
        self.assertEqual(result[5, 5, 3], 255)  # above threshold


class NoiseAndOutlineTests(unittest.TestCase):
    def test_isolated_edge_noise_is_removed(self):
        image = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((5, 5, 12, 12), fill=PURPLE + (255,))
        image.putpixel((0, 0), (255, 0, 0, 255))
        image.putpixel((19, 10), (0, 255, 0, 255))

        result = np.array(pp.clean_orphan_pixels(image))
        self.assertEqual(result[0, 0, 3], 0)
        self.assertEqual(result[10, 19, 3], 0)
        self.assertEqual(result[8, 8, 3], 255)
        self.assertEqual(tuple(result[8, 8, :3]), PURPLE)

    def test_add_outline_ring_on_transparent_margin(self):
        image = Image.new("RGBA", (7, 7), (0, 0, 0, 0))
        image.putpixel((3, 3), PURPLE + (255,))
        result = np.array(pp.add_1px_outline(image, (0, 0, 0)))
        self.assertEqual(tuple(result[3, 3, :3]), PURPLE)
        for y, x in ((2, 3), (4, 3), (3, 2), (3, 4)):
            self.assertEqual(tuple(result[y, x]), (0, 0, 0, 255))
        self.assertEqual(result[0, 0, 3], 0)


if __name__ == "__main__":
    unittest.main()
