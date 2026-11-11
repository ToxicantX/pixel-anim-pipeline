"""Focused, mocked tests for the _GripSocket rig and weapon wrappers.

No Aseprite is launched: run_custom_lua and _require_output are stubbed so the
generated Lua is inspected as text. Legacy generate_character_animations output
is pinned to a byte-exact SHA-256 captured before the refactor.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "mcp_server" / "server.py"

# SHA-256 of the legacy five-layer Lua for the synthetic sprite below, captured
# from the pre-refactor generator. Any visual/layout drift breaks this test.
LEGACY_LUA_SHA256 = "be00605c8871ca6e1e99b54a4c9f50a05cad5219dffafb38841d428f344b71ae"
LEGACY_TARGET = str(ROOT / "output" / "__tmp_gold.aseprite")

LEGACY_LAYERS = ["Shadow", "Legs", "Torso", "Head", "FX"]
SOCKET_LAYERS = ["Shadow", "Legs", "Torso", "Head", "Weapon", "FX", "_GripSocket"]


def layer_names(lua):
    """Layer names in creation order (excludes tag names)."""
    names = []
    for line in lua.splitlines():
        if "s.layers[1]" in line or "newLayer()" in line:
            match = re.search(r'name="([^"]+)"', line)
            if match:
                names.append(match.group(1))
    return names


def socket_entries(lua):
    """Parse the 16 five-value {x,y,r,g,b} socketFrames entries."""
    return [
        (int(a), int(b), int(c), int(d), int(e))
        for a, b, c, d, e in re.findall(r"\{(\d+),(\d+),(\d+),(\d+),(\d+)\}", lua)
    ]


def load_server():
    spec = importlib.util.spec_from_file_location("pixel_anim_server_socket_under_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_sprite():
    """12x12 synthetic hero: opaque rows 2..9, cols 3..8, three band colours."""
    image = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    for y in range(2, 10):
        for x in range(3, 9):
            if y < 5:
                color = (200, 100, 50, 255)
            elif y < 8:
                color = (80, 120, 200, 255)
            else:
                color = (50, 50, 60, 255)
            image.putpixel((x, y), color)
    return image


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.server = load_server()
        self._real_run = self.server.run_custom_lua
        self._real_require = self.server._require_output
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.sprite_path = Path(self.tempdir.name) / "hero.png"
        make_sprite().save(self.sprite_path)

    def tearDown(self):
        self.server.run_custom_lua = self._real_run
        self.server._require_output = self._real_require

    def capture_lua(self):
        captured = {}

        def fake(lua):
            captured["lua"] = lua

        self.server.run_custom_lua = fake
        self.server._require_output = lambda path: None
        return captured


class TorsoPoseTests(ServerTestCase):
    def test_torso_pose_table(self):
        expected = {
            1: (0, 0), 2: (0, 1), 3: (0, 1), 4: (0, 0),
            5: (0, 0), 6: (0, 1), 7: (0, 0), 8: (0, 0), 9: (0, 0), 10: (0, 1),
            11: (-1, 1), 12: (2, 0), 13: (1, 0), 14: (0, 0),
            15: (-2, -1), 16: (-1, 0),
        }
        for frame, pose in expected.items():
            self.assertEqual(self.server._torso_pose(frame), pose, f"frame {frame}")

    def test_grip_offset_table(self):
        expected = {
            1: (0, 0), 2: (0, 0), 3: (0, 0), 4: (0, 0),
            5: (1, 0), 6: (1, 0), 7: (0, 0), 8: (-1, 0), 9: (-1, 0), 10: (0, 0),
            11: (-2, -2), 12: (3, 0), 13: (2, 2), 14: (0, 0),
            15: (-1, -1), 16: (-1, 0),
        }
        for frame, offset in expected.items():
            self.assertEqual(self.server._grip_offset(frame), offset, f"frame {frame}")


class SocketPlanTests(ServerTestCase):
    def test_exact_coordinates_and_colors(self):
        poses, frames = self.server._socket_plan(12, 12, 0, 5, 6)
        expected = [
            (5, 6, 255, 0, 0),    # 1 idle
            (5, 7, 255, 0, 0),    # 2
            (5, 7, 255, 0, 0),    # 3
            (5, 6, 255, 0, 0),    # 4
            (6, 6, 255, 0, 0),    # 5 walk +1
            (6, 7, 255, 0, 0),    # 6 walk +1
            (5, 6, 255, 0, 0),    # 7 walk 0
            (4, 6, 255, 0, 0),    # 8 walk -1
            (4, 6, 255, 0, 0),    # 9 walk -1
            (5, 7, 255, 0, 0),    # 10 walk 0
            (2, 5, 0, 255, 0),    # 11 attack GREEN
            (10, 6, 0, 0, 255),   # 12 attack BLUE
            (8, 8, 255, 255, 0),  # 13 attack YELLOW
            (5, 6, 255, 0, 0),    # 14 attack RED
            (2, 4, 255, 0, 255),  # 15 hit MAGENTA
            (3, 6, 255, 0, 255),  # 16 hit MAGENTA
        ]
        self.assertEqual(frames, expected)

    def test_color_protocol(self):
        _, frames = self.server._socket_plan(12, 12, 0, 5, 6)
        colors = {i + 1: frame[2:] for i, frame in enumerate(frames)}
        red = (255, 0, 0)
        for frame in list(range(1, 11)) + [14]:
            self.assertEqual(colors[frame], red, f"frame {frame} should be RED")
        self.assertEqual(colors[11], (0, 255, 0))
        self.assertEqual(colors[12], (0, 0, 255))
        self.assertEqual(colors[13], (255, 255, 0))
        self.assertEqual(colors[15], (255, 0, 255))
        self.assertEqual(colors[16], (255, 0, 255))

    def test_out_of_canvas_socket_is_rejected(self):
        # Hand at x=1: attack frame 11 socket would be at x=-2.
        with self.assertRaises(ValueError) as ctx:
            self.server._socket_plan(6, 6, 0, 1, 2)
        self.assertIn("outside", str(ctx.exception))


class SocketLuaTests(ServerTestCase):
    def build(self):
        poses, frames = self.server._socket_plan(12, 12, 1, 5, 6)
        return poses, frames, self.server._build_rig_lua(
            12, 12, [[(0, 0, 1, 1, 1, 255)], [(5, 6, 1, 1, 1, 255)], [(5, 8, 1, 1, 1, 255)]],
            1, Path(LEGACY_TARGET), socket_frames=frames, torso_poses=poses)

    def test_layer_order_and_hidden_socket(self):
        _, _, lua = self.build()
        self.assertEqual(layer_names(lua), SOCKET_LAYERS)
        self.assertLess(lua.index('weapon.name="Weapon"'), lua.index('fx.name="FX"'))
        self.assertIn('grip.name="_GripSocket"; grip.isVisible=false', lua)
        self.assertIn("local torsoPose=", lua)
        self.assertIn("local socketFrames=", lua)
        self.assertNotIn("s:newCel(weapon", lua)  # Weapon layer stays empty

    def test_one_opaque_pixel_per_frame(self):
        poses, frames, lua = self.build()
        self.assertEqual(len(frames), 16)
        # One grip draw block inside the frame loop, executed once per frame.
        self.assertEqual(lua.count("local gripImg="), 1)
        self.assertEqual(lua.count("gripImg:drawPixel("), 1)
        self.assertEqual(socket_entries(lua), list(frames))

    def test_legacy_build_has_no_socket(self):
        lua = self.server._build_rig_lua(
            12, 12, [[(0, 0, 1, 1, 1, 255)], [(5, 6, 1, 1, 1, 255)], [(5, 8, 1, 1, 1, 255)]],
            1, Path(LEGACY_TARGET))
        self.assertEqual(layer_names(lua), LEGACY_LAYERS)
        for marker in ("Weapon", "_GripSocket", "torsoPose", "socketFrames"):
            self.assertNotIn(marker, lua)


class HandValidationTests(ServerTestCase):
    def assert_rejected(self, **kwargs):
        self.capture_lua()
        called = {"n": 0}
        original = self.server.run_custom_lua
        self.server.run_custom_lua = lambda lua: called.__setitem__("n", called["n"] + 1)
        out = Path(self.tempdir.name) / "out.aseprite"
        with self.assertRaises(ValueError):
            self.server.generate_character_with_socket(
                str(self.sprite_path), str(out), 4, 7, **kwargs)
        self.assertEqual(called["n"], 0, "run_custom_lua must not run on invalid hand")
        self.server.run_custom_lua = original

    def test_outside_canvas(self):
        self.assert_rejected(hand_x=20, hand_y=6)

    def test_head_band_rejected(self):
        self.assert_rejected(hand_x=5, hand_y=4)

    def test_transparent_rejected(self):
        self.assert_rejected(hand_x=0, hand_y=6)

    def test_non_integer_rejected(self):
        self.assert_rejected(hand_x=5.0, hand_y=6)

    def test_socket_outside_canvas_rejected(self):
        tiny = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        for y in range(1, 6):
            for x in range(1, 7):
                tiny.putpixel((x, y), (90, 90, 90, 255))
        path = Path(self.tempdir.name) / "tiny.png"
        tiny.save(path)
        self.capture_lua()
        with self.assertRaises(ValueError) as ctx:
            self.server.generate_character_with_socket(
                str(path), str(Path(self.tempdir.name) / "o.aseprite"), 2, 3, 2, 3)
        self.assertIn("outside", str(ctx.exception))


class MockedGenerationTests(ServerTestCase):
    def test_socket_generation_embeds_plan(self):
        captured = self.capture_lua()
        out = Path(self.tempdir.name) / "hero_socket.aseprite"
        result = self.server.generate_character_with_socket(
            str(self.sprite_path), str(out), 4, 7, 5, 6)
        self.assertTrue(result.endswith(".aseprite"))
        lua = captured["lua"]
        ground_shift = 1  # 12 - 2 - bottom(9)
        _, expected = self.server._socket_plan(12, 12, ground_shift, 5, 6)
        self.assertEqual(socket_entries(lua), list(expected))
        self.assertEqual(layer_names(lua), SOCKET_LAYERS)


class LegacyUnchangedTests(ServerTestCase):
    def test_legacy_lua_is_byte_identical(self):
        captured = self.capture_lua()
        self.server.generate_character_animations(str(self.sprite_path), LEGACY_TARGET, 4, 7)
        lua = captured["lua"]
        digest = hashlib.sha256(lua.encode("utf-8")).hexdigest()
        self.assertEqual(digest, LEGACY_LUA_SHA256)
        self.assertEqual(layer_names(lua), LEGACY_LAYERS)
        self.assertNotIn("Weapon", lua)
        self.assertNotIn("_GripSocket", lua)


class WeaponWrapperTests(ServerTestCase):
    def test_inspect_delegates_path(self):
        seen = {}

        class FakeModule:
            @staticmethod
            def inspect_weapon_ascii(path):
                seen["path"] = path
                return "ASCII"

        self.server._load_weapon_tools = lambda: FakeModule
        result = self.server.inspect_weapon_ascii(str(self.sprite_path))
        self.assertEqual(result, "ASCII")
        self.assertIsInstance(seen["path"], Path)
        self.assertTrue(seen["path"].is_file())

    def test_equip_delegates_contract(self):
        seen = {}

        class FakeModule:
            @staticmethod
            def equip_and_export_weapon(character_path, weapon_path, output_dir,
                                        weapon_id, pivot_x, pivot_y, *,
                                        run_lua, run_aseprite, lua_string):
                seen.update(
                    character_path=character_path, weapon_path=weapon_path,
                    output_dir=output_dir, weapon_id=weapon_id,
                    pivot_x=pivot_x, pivot_y=pivot_y,
                    run_lua=run_lua, run_aseprite=run_aseprite, lua_string=lua_string)
                return {"ok": True}

        self.server._load_weapon_tools = lambda: FakeModule
        out = Path(self.tempdir.name) / "exports"
        result = self.server.equip_and_export_weapon(
            str(self.sprite_path), str(self.sprite_path), str(out),
            "sword", 3, 4)
        self.assertEqual(result, {"ok": True})
        self.assertIsInstance(seen["character_path"], Path)
        self.assertIsInstance(seen["weapon_path"], Path)
        self.assertIsInstance(seen["output_dir"], Path)
        self.assertEqual(seen["weapon_id"], "sword")
        self.assertEqual((seen["pivot_x"], seen["pivot_y"]), (3, 4))
        self.assertEqual(seen["run_lua"], self.server.run_custom_lua)
        self.assertEqual(seen["run_aseprite"], self.server._run)
        self.assertEqual(seen["lua_string"], self.server._lua_string)


class RegistrationTests(ServerTestCase):
    def test_eight_tools_registered(self):
        expected = {
            "preprocess_gpt_sprite", "inspect_sprite_ascii", "generate_character_animations",
            "generate_character_with_socket", "inspect_weapon_ascii",
            "equip_and_export_weapon", "run_custom_lua", "export_game_assets",
        }
        tools = set(self.server.mcp._tool_manager._tools)
        self.assertEqual(tools, expected)
        for name in expected:
            self.assertTrue(callable(getattr(self.server, name)), name)


if __name__ == "__main__":
    unittest.main()
