"""Paperdoll acceptance test: socket rig, weapon inspection, weapon equip.

Run from any directory:

    .venv/Scripts/python.exe -B scripts/test_paperdoll.py                # full run
    .venv/Scripts/python.exe -B scripts/test_paperdoll.py --fixture-only

The script draws three deterministic procedural fixtures on pure #FF00FF
(test artifacts, not art deliverables) under input/paperdoll/:

    hero_raw.png   unarmed right-facing hero, closed fist forward at torso height
    sword_raw.png  north-up sword, tip marker on top, grip wrap near the bottom
    axe_raw.png    north-up axe, tip marker on top, grip wrap near the bottom

It then calls the server tools in SOP order and writes only under
output/paperdoll/ (legacy output/test_hero* files are never touched):

    preprocess_gpt_sprite(hero) -> inspect_sprite_ascii -> choose splits + fist
    -> generate_character_with_socket -> export_game_assets(asset_name='char')
    -> for sword, axe: preprocess_gpt_sprite(weapon, 24px) -> inspect_weapon_ascii
       -> equip_and_export_weapon

Coordinates are zero-based canvas pixels, x right, y down. Angles are degrees
clockwise from north-up. The ASCII parser and the native .aseprite parser are
imported from scripts/test_pipeline.py, which this script does not modify.
Any failed check prints a FAIL line and the process exits with status 1.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import math
import re
import struct
import sys
import tempfile
import time
import traceback
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "mcp_server" / "server.py"
LEGACY_PATH = ROOT / "scripts" / "test_pipeline.py"
IN_DIR = ROOT / "input" / "paperdoll"
OUT_DIR = ROOT / "output" / "paperdoll"
HERO_RAW = IN_DIR / "hero_raw.png"
HERO_CLEAN = IN_DIR / "hero_clean.png"
SOCKET_ASE = OUT_DIR / "character_socket.aseprite"
CHAR_SHEET = OUT_DIR / "char_sheet.png"
CHAR_JSON = OUT_DIR / "char_sheet.json"
CHAR_GIF = OUT_DIR / "char_preview.gif"
SOCKETS_JSON = OUT_DIR / "grip_sockets.json"

CANVAS = 32
WEAPON_CANVAS = 24
MAX_COLORS = 16
FRAME_COUNT = 16
GIF_SCALE = 4
EXPECTED_LAYERS = ["Shadow", "Legs", "Torso", "Head", "Weapon", "FX", "_GripSocket"]
BODY_LAYERS = ["Shadow", "Legs", "Torso", "Head", "FX"]
EXPECTED_TOOLS = {"preprocess_gpt_sprite", "inspect_sprite_ascii", "generate_character_animations",
                  "run_custom_lua", "export_game_assets", "generate_character_with_socket",
                  "inspect_weapon_ascii", "equip_and_export_weapon"}

# Socket colour protocol, one entry per 1-based frame.
RED, GREEN, BLUE, YELLOW, MAGENTA_RGB = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255)
PROTOCOL = {RED: 15, GREEN: -45, BLUE: 90, YELLOW: 135, MAGENTA_RGB: -25}
FRAME_COLORS = [RED] * 10 + [GREEN, BLUE, YELLOW, RED, MAGENTA_RGB, MAGENTA_RGB]

FIXTURE_SCALE = 8
FIXTURE_MARGIN = 4
MAGENTA = (255, 0, 255)

# Hero: 15x27 design so the 32px processor box (26x27) recovers it 1:1.
HERO_PALETTE = {
    "O": (27, 27, 42), "H": (107, 58, 30), "S": (242, 192, 138), "E": (40, 36, 64),
    "A": (62, 111, 201), "L": (127, 168, 232), "D": (42, 74, 138), "G": (232, 192, 64),
    "B": (122, 74, 34), "P": (58, 53, 80), "K": (90, 52, 24),
}
HERO = [
    "....OOOOO........",  # 0  head rows 0..7
    "...OHHHHHO.......",
    "..OHHHHHHHO......",
    "..OHHHSSSSO......",
    "..OHHSSSESO......",  # eye on the right: faces right
    "..OHHSSSSSSO.....",
    "...OHSSSSSO......",
    "....OOSSOO.......",
    "...OODAAAO.......",  # 8  torso rows 8..17
    "..ODAALLAAO......",
    "..ODALGGLAOOOOOOO",  # forearm reaches forward
    "..ODALGGLAAAOSSSO",  # fist: skin block at columns 13..15, rows 11..13
    "..ODAALLAADAOSSSO",
    "..ODAAAAAADOOSSSO",
    "..ODDAAAAADO.OOOO",
    "...ODAAAAAO......",
    "...OBBBGBBO......",
    "...OBBBBBBO......",
    "...OPPPPPPPO.....",  # 18 legs rows 18..26
    "...OPPPOPPPO.....",
    "...OPPPOPPPO.....",
    "...OPPPOPPPO.....",
    "...OPPPOPPPO.....",
    "...OKKKOKKKO.....",
    "...OKKKOKKKO.....",
    "...OKKKKOKKKKO...",
    "...OOOOOOOOOOO...",
]
HERO_HEAD_END, HERO_TORSO_END = 7, 17
HERO_FIST_CENTER = (14, 12)  # design coordinates
TORSO_GLYPHS, LEG_GLYPHS = set("ADLGB"), set("PK")

# Weapons: 19 rows tall so the 24px processor box (18x19) recovers them 1:1.
WEAPON_PALETTE = {
    "O": (27, 27, 42), "W": (200, 208, 220), "T": (64, 224, 208), "G": (232, 192, 64),
    "R": (150, 40, 40), "N": (140, 90, 50), "X": (160, 170, 190),
}
SWORD = ["..T..", *[".OWO."] * 11, "GGGGG", *[".ORO."] * 4, ".OGO.", ".OOO."]
AXE = ["....T....", ".OOONOOO.", "OXXXNXXXO", "OXXXNXXXO", "OXXXNXXXO", ".OXXNXXO.",
       "..OONOO..", *["...ONO..."] * 6, *["...ORO..."] * 4, "...OGO...", "...OOO..."]
WEAPONS = {"sword": SWORD, "axe": AXE}
WEAPON_KNOWN_PIVOT = {"sword": (2, 15), "axe": (4, 15)}  # design coordinates, on the R wrap


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_module("pixel_anim_legacy_pipeline_test", LEGACY_PATH)
Checker, parse_ascii, parse_aseprite, opaque_map, same_path = (
    legacy.Checker, legacy.parse_ascii, legacy.parse_aseprite, legacy.opaque_map, legacy.same_path)
EXPECTED_TAGS = legacy.EXPECTED_TAGS  # 0-based, as exported in JSON and parsed natively


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def render(design: list[str], palette: dict) -> bytes:
    if any(len(row) != len(design[0]) for row in design):
        raise ValueError("fixture rows must share one width")
    width, height = len(design[0]) + 2 * FIXTURE_MARGIN, len(design) + 2 * FIXTURE_MARGIN
    image = Image.new("RGB", (width * FIXTURE_SCALE, height * FIXTURE_SCALE), MAGENTA)
    for y, row in enumerate(design):
        for x, ch in enumerate(row):
            if ch != ".":
                ox, oy = (x + FIXTURE_MARGIN) * FIXTURE_SCALE, (y + FIXTURE_MARGIN) * FIXTURE_SCALE
                image.paste(palette[ch], (ox, oy, ox + FIXTURE_SCALE, oy + FIXTURE_SCALE))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def write_fixtures(directory: Path = IN_DIR) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {"hero": directory / "hero_raw.png"}
    blobs = {"hero": render(HERO, HERO_PALETTE)}
    for wid, design in WEAPONS.items():
        paths[wid] = directory / f"{wid}_raw.png"
        blobs[wid] = render(design, WEAPON_PALETTE)
    for key, path in paths.items():
        if path.is_file() and path.read_bytes() == blobs[key]:
            print(f"fixture unchanged: {path}")
        else:
            path.write_bytes(blobs[key])
            print(f"fixture written: {path}")
    return paths


def placement(design: list[str], canvas: int) -> tuple[int, int, int, int]:
    """Design bbox origin and bottom-center paste origin for a 1:1 recovery."""
    cells = [(x, y) for y, row in enumerate(design) for x, ch in enumerate(row) if ch != "."]
    x0, y0 = min(x for x, _ in cells), min(y for _, y in cells)
    w = max(x for x, _ in cells) + 1 - x0
    h = max(y for _, y in cells) + 1 - y0
    return x0, y0, (canvas - w) // 2, canvas - 1 - h


def expected_clean(design: list[str], palette: dict, canvas: int) -> dict:
    x0, y0, px, py = placement(design, canvas)
    return {(px + x - x0, py + y - y0): (*palette[ch], 255)
            for y, row in enumerate(design) for x, ch in enumerate(row) if ch != "."}


def to_canvas(design: list[str], canvas: int, xy: tuple[int, int]) -> tuple[int, int]:
    x0, y0, px, py = placement(design, canvas)
    return px + xy[0] - x0, py + xy[1] - y0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def layer_flags(path: Path) -> dict[str, int]:
    """Layer name -> flags word (bit 0 = visible). The legacy parser omits flags."""
    data = path.read_bytes()
    (frames,) = struct.unpack_from("<H", data, 6)
    flags, offset = {}, 128
    for _ in range(frames):
        frame_bytes, _magic, old_chunks = struct.unpack_from("<IHH", data, offset)
        (new_chunks,) = struct.unpack_from("<I", data, offset + 12)
        pos = offset + 16
        for _ in range(new_chunks or old_chunks):
            size, ctype = struct.unpack_from("<IH", data, pos)
            if ctype == 0x2004:
                (word,) = struct.unpack_from("<H", data, pos + 6)
                (length,) = struct.unpack_from("<H", data, pos + 22)
                flags[data[pos + 24:pos + 24 + length].decode("utf-8")] = word
            pos += size
        offset += frame_bytes
    return flags


def composite(frame: dict, order: list[str], hidden: set[str]) -> dict:
    """Stack visible layers bottom to top; an opaque upper pixel replaces the lower one."""
    out: dict = {}
    for name in order:
        if name in hidden:
            continue
        for xy, rgba in frame.get(name, {}).items():
            if rgba[3] == 255 or xy not in out:
                out[xy] = rgba
    return out


def inverse_place(weapon: dict, size: tuple[int, int], pivot, grip, angle: float,
                  bounded: bool = True) -> dict:
    """Reference nearest-neighbour inverse mapping about pixel centres.

    Destination (x, y) samples source pivot + R^-1 (x - gx, y - gy), where R
    rotates clockwise on a y-down canvas. With bounded=False the destination
    grid grows around the grip so off-canvas samples can be counted.
    """
    c = round(math.cos(math.radians(angle)), 15)
    s = round(math.sin(math.radians(angle)), 15)
    (px, py), (gx, gy) = pivot, grip
    if bounded:
        xs, ys = range(size[0]), range(size[1])
    else:
        r = math.ceil(math.hypot(WEAPON_CANVAS, WEAPON_CANVAS)) + 2
        xs, ys = range(gx - r, gx + r + 1), range(gy - r, gy + r + 1)
    out = {}
    for y in ys:
        for x in xs:
            dx, dy = x - gx, y - gy
            sx = math.floor(px + 0.5 + c * dx + s * dy)
            sy = math.floor(py + 0.5 - s * dx + c * dy)
            if (sx, sy) in weapon:
                out[(x, y)] = weapon[(sx, sy)]
    return out


def torso_offset(rest: dict, frame: dict) -> tuple[int, int] | None:
    """Shift that carries the rest-pose torso mask inside this frame's torso mask."""
    best = None
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            shifted = {(x + dx, y + dy) for x, y in rest}
            if shifted <= frame.keys():
                extra = len(frame.keys() - shifted)
                if best is None or extra < best[0]:
                    best = (extra, (dx, dy))
    return best[1] if best else None


def gif_frames(path: Path) -> tuple[tuple[int, int], list[dict], list]:
    frames, durations = [], []
    with Image.open(path) as gif:
        size = gif.size
        for index in range(getattr(gif, "n_frames", 1)):
            gif.seek(index)
            rgba = gif.convert("RGBA")
            durations.append(gif.info.get("duration"))
            cells = {}
            for y in range(size[1] // GIF_SCALE):
                for x in range(size[0] // GIF_SCALE):
                    p = rgba.getpixel((x * GIF_SCALE + 1, y * GIF_SCALE + 1))
                    if p[3]:
                        cells[(x, y)] = p
            frames.append(cells)
    return size, frames, durations


def sheet_frames(c, sheet: Path, meta_path: Path, native: dict, label: str) -> list[dict]:
    doc = json.loads(meta_path.read_text(encoding="utf-8"))
    frames = doc.get("frames")
    if isinstance(frames, dict):
        frames = list(frames.values())
    c.check(isinstance(frames, list) and len(frames) == FRAME_COUNT, f"{label}: JSON frame count != {FRAME_COUNT}")
    tags = [(t.get("name"), t.get("from"), t.get("to")) for t in doc.get("meta", {}).get("frameTags", [])]
    c.check(tags == EXPECTED_TAGS, f"{label}: frameTags {tags} != {EXPECTED_TAGS}")
    with Image.open(sheet) as source:
        image = source.convert("RGBA")
    out, durations = [], []
    for i, entry in enumerate(frames or []):
        rect, src = entry.get("frame", {}), entry.get("sourceSize", {})
        x, y, w, h = (rect.get(k) for k in ("x", "y", "w", "h"))
        c.check((w, h) == (CANVAS, CANVAS) and (src.get("w"), src.get("h")) == (CANVAS, CANVAS)
                and not entry.get("trimmed"), f"{label}: frame {i} is not an untrimmed {CANVAS}x{CANVAS} cell")
        durations.append(entry.get("duration"))
        out.append(opaque_map(image.crop((x, y, x + w, y + h))) if None not in (x, y, w, h) else {})
    c.check(durations == native["durations"], f"{label}: durations {durations} != body {native['durations']}")
    return out


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def check_tool_surface(c, server) -> None:
    for name in EXPECTED_TOOLS:
        c.check(callable(getattr(server, name, None)), f"server does not expose callable {name}()")
    registered = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    c.check(registered == EXPECTED_TOOLS, f"registered MCP tools {sorted(registered)} != {sorted(EXPECTED_TOOLS)}")
    print(f"registered tools ({len(registered)}): {sorted(registered)}")


def choose_rig(c, ascii_text: str) -> tuple[int, int, int, int]:
    """Read the dump: splits from colour bands, hand from the forward skin block."""
    width, height, legend, grid = parse_ascii(ascii_text)
    c.check((width, height) == (CANVAS, CANVAS), f"ASCII size {width}x{height}")
    with Image.open(HERO_CLEAN) as source:
        png = opaque_map(source)
    rebuilt = {(x, y): (*legend[ch], 255) for y, row in enumerate(grid) for x, ch in enumerate(row) if ch in legend}
    c.check(rebuilt == png, "ASCII dump does not reproduce hero_clean.png")
    c.check(png == expected_clean(HERO, HERO_PALETTE, CANVAS), "hero_clean.png is not the 1:1 fixture recovery")
    glyph = {g: next((k for k, v in HERO_PALETTE.items() if v == rgb), None) for g, rgb in legend.items()}
    rows = {name: sorted({y for y, row in enumerate(grid) for ch in row if glyph.get(ch) in group})
            for name, group in (("torso", TORSO_GLYPHS), ("legs", LEG_GLYPHS))}
    head_end_y, torso_end_y = rows["torso"][0] - 1, rows["legs"][0] - 1
    # The fist is the skin inside the torso band; its centre is the hand pixel.
    skin = [(x, y) for y, row in enumerate(grid) for x, ch in enumerate(row)
            if glyph.get(ch) == "S" and head_end_y < y <= torso_end_y]
    if not c.check(bool(skin), "no skin (fist) pixels inside the torso band"):
        raise ValueError("fist not found in the ASCII dump")
    hand_x = round(sum(x for x, _ in skin) / len(skin))
    hand_y = round(sum(y for _, y in skin) / len(skin))
    known = (to_canvas(HERO, CANVAS, (0, HERO_HEAD_END))[1], to_canvas(HERO, CANVAS, (0, HERO_TORSO_END))[1],
             *to_canvas(HERO, CANVAS, HERO_FIST_CENTER))
    chosen = (head_end_y, torso_end_y, hand_x, hand_y)
    c.check(chosen == known, f"ASCII-derived rig {chosen} != fixture layout {known}")
    c.check(png.get((hand_x, hand_y), (0, 0, 0, 0))[:3] == HERO_PALETTE["S"], "hand coordinate is not on the fist")
    print(f"splits (inclusive): head y <= {head_end_y}, torso y <= {torso_end_y}; hand ({hand_x}, {hand_y})")
    return chosen


def check_socket_char(c, server, rig) -> dict:
    head_end_y, torso_end_y, hand_x, hand_y = rig
    ase = parse_aseprite(SOCKET_ASE)
    c.check((ase["frames"], ase["width"], ase["height"]) == (FRAME_COUNT, CANVAS, CANVAS),
            f"socket char {ase['frames']} frames {ase['width']}x{ase['height']}")
    c.check(ase["layers"] == EXPECTED_LAYERS, f"socket char layers {ase['layers']} != {EXPECTED_LAYERS}")
    c.check(ase["tags"] == EXPECTED_TAGS, f"socket char tags {ase['tags']} != {EXPECTED_TAGS}")
    c.check(ase["durations"] == list(server.DURATIONS), f"durations {ase['durations']} != {server.DURATIONS}")
    flags = layer_flags(SOCKET_ASE)
    c.check(flags.get("_GripSocket", 1) & 1 == 0, "_GripSocket layer is visible")
    c.check(all(flags.get(n, 0) & 1 for n in EXPECTED_LAYERS if n != "_GripSocket"), f"a body layer is hidden: {flags}")
    if ase["layers"] != EXPECTED_LAYERS or len(ase["images"]) != FRAME_COUNT:
        return ase

    # Head, legs, shadow and FX keep the legacy rig; only the torso pose table differs.
    with tempfile.TemporaryDirectory(prefix="paperdoll_legacy_") as temp:
        legacy_ase = Path(temp) / "legacy.aseprite"
        server.generate_character_animations(str(HERO_CLEAN), str(legacy_ase), head_end_y, torso_end_y)
        old = parse_aseprite(legacy_ase)
    c.check(old["layers"] == ["Shadow", "Legs", "Torso", "Head", "FX"], f"legacy rig layers changed: {old['layers']}")
    for f in range(FRAME_COUNT):
        for name in ("Shadow", "Legs", "Head", "FX"):
            c.check(ase["images"][f][name] == old["images"][f][name], f"frame {f + 1}: {name} differs from legacy rig")
        c.check(not ase["images"][f]["Weapon"], f"frame {f + 1}: base Weapon layer is not empty")
        c.check(bool(ase["images"][f]["Torso"]), f"frame {f + 1}: Torso layer is empty")

    # The socket rides the torso actually drawn in each frame, plus the documented grip offset.
    grip_offset = getattr(server, "_grip_offset", lambda frame: (0, 0))
    rest = ase["images"][0]["Torso"]
    trail = []
    for f in range(FRAME_COUNT):
        marker = ase["images"][f]["_GripSocket"]
        if not c.check(len(marker) == 1, f"frame {f + 1}: _GripSocket has {len(marker)} pixels, expected 1"):
            continue
        (xy, rgba), = marker.items()
        trail.append(xy)
        c.check(rgba == (*FRAME_COLORS[f], 255), f"frame {f + 1}: marker {rgba} != {(*FRAME_COLORS[f], 255)}")
        offset = torso_offset(rest, ase["images"][f]["Torso"])
        if c.check(offset is not None, f"frame {f + 1}: cannot infer torso offset"):
            gx, gy = grip_offset(f + 1)
            expected = (hand_x + offset[0] + gx, hand_y + offset[1] + gy)
            c.check(xy == expected, f"frame {f + 1}: socket {xy} != hand + torso {offset} + grip {(gx, gy)} = {expected}")
    print(f"socket per frame: {trail}")
    return ase


def check_char_export(c, exported, native: dict) -> None:
    for key, path in (("gif", CHAR_GIF), ("sheet", CHAR_SHEET), ("json", CHAR_JSON)):
        c.check(isinstance(exported, dict) and same_path(exported.get(key, ""), path), f"char export [{key}] != {path}")
    cells = sheet_frames(c, CHAR_SHEET, CHAR_JSON, native, "char sheet")
    for f, cell in enumerate(cells):
        body = composite(native["images"][f], native["layers"], {"_GripSocket"})
        c.check({k: v for k, v in cell.items() if k[1] < CANVAS - 1} == {k: v for k, v in body.items() if k[1] < CANVAS - 1},
                f"char sheet frame {f + 1} != visible body layers (marker leaked?)")


def socket_reference(native: dict) -> list[dict]:
    out = []
    for f, frame in enumerate(native["images"]):
        (xy, rgba), = frame["_GripSocket"].items()
        out.append({"frame": f + 1, "grip_x": xy[0], "grip_y": xy[1], "angle_deg": PROTOCOL[rgba[:3]]})
    return out


def weapon_step(c, server, wid: str, socket_digest: str, sockets: list, native: dict) -> dict | None:
    raw, clean = IN_DIR / f"{wid}_raw.png", IN_DIR / f"{wid}_clean.png"
    print(f"\n== weapon {wid}: preprocess_gpt_sprite ({WEAPON_CANVAS}px) ==")
    result = server.preprocess_gpt_sprite(str(raw), str(clean), canvas_size=WEAPON_CANVAS, max_colors=MAX_COLORS)
    c.check(same_path(result, clean), f"{wid}: preprocess returned {result!r}")
    with Image.open(clean) as source:
        c.check(source.size == (WEAPON_CANVAS, WEAPON_CANVAS), f"{wid}: clean size {source.size}")
        weapon = opaque_map(source)
    c.check(weapon == expected_clean(WEAPONS[wid], WEAPON_PALETTE, WEAPON_CANVAS), f"{wid}: clean is not the 1:1 recovery")

    print(f"== weapon {wid}: inspect_weapon_ascii ==")
    text = server.inspect_weapon_ascii(str(clean))
    print(text)
    px = re.search(r"pivot_x\s*=\s*(-?\d+)", text or "")
    py = re.search(r"pivot_y\s*=\s*(-?\d+)", text or "")
    if not c.check(bool(px and py), f"{wid}: inspect_weapon_ascii output lacks pivot_x=/pivot_y="):
        return None
    pivot = (int(px.group(1)), int(py.group(1)))
    c.check(pivot in weapon, f"{wid}: suggested pivot {pivot} is not on an opaque pixel")
    rows = {int(m.group(1)): m.group(2) for m in re.finditer(r"^\s*(\d+) \| ?(\S+)\s*$", text, re.M)}
    mask = {(x, y) for y, row in rows.items() for x, ch in enumerate(row) if ch != "."}
    c.check(mask == set(weapon), f"{wid}: ASCII rows do not reproduce the clean weapon mask")
    # Manual verification step of the SOP: keep the heuristic only when it sits on the grip wrap.
    if weapon.get(pivot, (0, 0, 0, 0))[:3] != WEAPON_PALETTE["R"]:
        known = to_canvas(WEAPONS[wid], WEAPON_CANVAS, WEAPON_KNOWN_PIVOT[wid])
        print(f"NOTE: {wid}: suggested pivot {pivot} is off the grip wrap; using verified grip pixel {known}")
        pivot = known
    print(f"{wid}: pivot used {pivot}")

    print(f"== weapon {wid}: equip_and_export_weapon ==")
    clean_digest = digest(clean)
    res = server.equip_and_export_weapon(str(SOCKET_ASE), str(clean), str(OUT_DIR), wid, pivot[0], pivot[1])
    if not c.check(isinstance(res, dict), f"{wid}: equip returned {type(res).__name__}, expected dict"):
        return None
    names = {"equipped_ase": f"char_with_{wid}.aseprite", "sheet": f"weapon_{wid}_sheet.png",
             "json": f"weapon_{wid}_sheet.json", "gif": f"preview_with_{wid}.gif", "sockets": "grip_sockets.json"}
    for key, name in names.items():
        c.check(same_path(res.get(key, ""), OUT_DIR / name), f"{wid}: result[{key!r}] = {res.get(key)!r}, expected {name}")
    c.check(digest(SOCKET_ASE) == socket_digest, f"{wid}: equip modified character_socket.aseprite")
    c.check(digest(clean) == clean_digest, f"{wid}: equip modified the clean weapon PNG")

    eq_path = OUT_DIR / names["equipped_ase"]
    eq = parse_aseprite(eq_path)
    c.check(eq["layers"] == EXPECTED_LAYERS, f"{wid}: equipped layers {eq['layers']} != {EXPECTED_LAYERS}")
    c.check(eq["tags"] == EXPECTED_TAGS and eq["durations"] == native["durations"], f"{wid}: equipped tags/durations changed")
    flags = layer_flags(eq_path)
    c.check(flags.get("_GripSocket", 1) & 1 == 0 and flags.get("Weapon", 0) & 1, f"{wid}: equipped visibility {flags}")
    written = json.loads(SOCKETS_JSON.read_text(encoding="utf-8"))
    c.check(written == sockets, f"{wid}: grip_sockets.json != socket layer protocol; got {written[:1]}...")

    clipped_total, overlay = 0, []
    clipping = {r.get("frame"): r.get("clipped_pixels") for r in (res.get("clipping") or []) if isinstance(r, dict)}
    for f in range(FRAME_COUNT):
        grip, angle = (sockets[f]["grip_x"], sockets[f]["grip_y"]), sockets[f]["angle_deg"]
        layer = eq["images"][f].get("Weapon", {})
        ref = inverse_place(weapon, (CANVAS, CANVAS), pivot, grip, angle)
        full = inverse_place(weapon, (CANVAS, CANVAS), pivot, grip, angle, bounded=False)
        overlay.append(layer)
        c.check(bool(layer), f"{wid}: frame {f + 1} Weapon layer empty")
        c.check(layer == ref, f"{wid}: frame {f + 1} Weapon layer != inverse-sampled reference "
                              f"({len(set(layer) ^ set(ref))} px differ)")
        c.check(layer.get(grip) == weapon[pivot], f"{wid}: frame {f + 1} grip pixel is not the pivot colour")
        clipped_total += len(full) - len(ref)
        c.check(clipping.get(f + 1) == len(full) - len(ref),
                f"{wid}: frame {f + 1} clipping report {clipping.get(f + 1)} != {len(full) - len(ref)} clipped px")
        for name in BODY_LAYERS + ["_GripSocket"]:
            c.check(eq["images"][f].get(name) == native["images"][f][name], f"{wid}: frame {f + 1} {name} changed")
    c.check(bool(res.get("warnings")) == (clipped_total > 0),
            f"{wid}: warnings {res.get('warnings')} do not match {clipped_total} clipped px")
    print(f"{wid}: clipped px per frame {[clipping.get(f + 1) for f in range(FRAME_COUNT)]}")

    # +90 deg (frame 12, BLUE) points right of the grip.
    f12 = 11
    grip = (sockets[f12]["grip_x"], sockets[f12]["grip_y"])
    tip_src = next(xy for xy, p in weapon.items() if p[:3] == WEAPON_PALETTE["T"])
    tip = (grip[0] + pivot[1] - tip_src[1], grip[1] + tip_src[0] - pivot[0])
    c.check(sockets[f12]["angle_deg"] == 90 and tip[0] > grip[0] and abs(tip[1] - grip[1]) <= 1,
            f"{wid}: +90 tip reference {tip} not right of grip {grip}")
    visible_tip = [xy for xy, p in overlay[f12].items() if p[:3] == WEAPON_PALETTE["T"]]
    c.check(all(x > grip[0] for x, _ in visible_tip), f"{wid}: surviving +90 tip pixels {visible_tip} not right of grip")
    right = sum(1 for x, _ in overlay[f12] if x > grip[0])
    c.check(right > len(overlay[f12]) - right, f"{wid}: +90 overlay mass is not right of grip")
    print(f"{wid}: +90 grip {grip}, tip reference {tip}, surviving tip pixels {visible_tip}")

    cells = sheet_frames(c, OUT_DIR / names["sheet"], OUT_DIR / names["json"], native, f"{wid} sheet")
    for f, cell in enumerate(cells):
        c.check(cell == overlay[f], f"{wid}: sheet frame {f + 1} != Weapon layer (body or marker leaked)")

    size, frames, durations = gif_frames(OUT_DIR / names["gif"])
    c.check(size == (CANVAS * GIF_SCALE, CANVAS * GIF_SCALE), f"{wid}: GIF size {size}")
    hidden = {n for n, fl in flags.items() if not fl & 1}
    # Row H-1 holds only the semi-transparent shadow, which GIF cannot carry; compare rows above it.
    expect = [frozenset(xy for xy in composite(eq["images"][f], eq["layers"], hidden) if xy[1] < CANVAS - 1)
              for f in range(FRAME_COUNT)]
    got = [frozenset(xy for xy in fr if xy[1] < CANVAS - 1) for fr in frames]
    if len(got) != FRAME_COUNT:  # identical adjacent composites may legitimately merge
        merged, merged_durations = [], []
        for f, mask in enumerate(expect):
            if merged and merged[-1] == mask:
                merged_durations[-1] += eq["durations"][f]
            else:
                merged.append(mask)
                merged_durations.append(eq["durations"][f])
        expect, expect_durations = merged, merged_durations
    else:
        expect_durations = eq["durations"]
    c.check(got == expect, f"{wid}: GIF masks != composed visible layers ({len(got)} vs {len(expect)} frames)")
    c.check(durations == expect_durations, f"{wid}: GIF durations {durations} != {expect_durations}")
    if len(frames) == FRAME_COUNT:
        for f, fr in enumerate(frames):
            grip = (sockets[f]["grip_x"], sockets[f]["grip_y"])
            c.check(fr.get(grip, (0, 0, 0, 0))[:3] != FRAME_COLORS[f], f"{wid}: GIF frame {f + 1} shows the socket marker")
    return {"overlay": overlay}


def run() -> int:
    c = Checker()
    started = time.time()
    write_fixtures()
    server = load_module("pixel_anim_server_paperdoll", SERVER_PATH)
    c.section("tool surface", check_tool_surface, server)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if SOCKETS_JSON.is_file():
        SOCKETS_JSON.unlink()  # dedicated output; equip refuses a stale trajectory from an older rig

    print("\n== 1. preprocess_gpt_sprite (hero) ==")
    result = server.preprocess_gpt_sprite(str(HERO_RAW), str(HERO_CLEAN), canvas_size=CANVAS, max_colors=MAX_COLORS)
    c.check(same_path(result, HERO_CLEAN), f"preprocess returned {result!r}")

    print("\n== 2. inspect_sprite_ascii ==")
    ascii_text = server.inspect_sprite_ascii(str(HERO_CLEAN))
    print(ascii_text)
    rig = c.section("choose splits and hand from ASCII", choose_rig, ascii_text)
    if rig is None:
        return legacy.report(c)

    print("\n== 3. generate_character_with_socket ==")
    result = server.generate_character_with_socket(str(HERO_CLEAN), str(SOCKET_ASE), *rig)
    c.check(same_path(result, SOCKET_ASE), f"generate returned {result!r}")
    native = c.section("socket character", check_socket_char, server, rig)
    if not native or native["layers"] != EXPECTED_LAYERS:
        return legacy.report(c)
    try:
        sockets = socket_reference(native)
    except (ValueError, KeyError) as exc:
        c.check(False, f"socket layer does not follow the protocol: {exc}")
        return legacy.report(c)

    print("\n== 4. export_game_assets (char) ==")
    exported = server.export_game_assets(str(SOCKET_ASE), str(OUT_DIR), "char")
    c.section("char sheet", check_char_export, exported, native)

    socket_digest = digest(SOCKET_ASE)
    results = {wid: c.section(f"weapon {wid}", weapon_step, server, wid, socket_digest, sockets, native)
               for wid in WEAPONS}
    if all(results.values()):
        c.check(results["sword"]["overlay"] != results["axe"]["overlay"], "sword and axe overlays are identical")
        c.check(digest(OUT_DIR / "weapon_sword_sheet.png") != digest(OUT_DIR / "weapon_axe_sheet.png"),
                "sword and axe sheets are identical")
    outputs = [SOCKET_ASE, CHAR_SHEET, CHAR_JSON, CHAR_GIF, SOCKETS_JSON]
    for wid in WEAPONS:
        outputs += [OUT_DIR / f"char_with_{wid}.aseprite", OUT_DIR / f"weapon_{wid}_sheet.png",
                    OUT_DIR / f"weapon_{wid}_sheet.json", OUT_DIR / f"preview_with_{wid}.gif"]
    for path in outputs:
        c.check(path.is_file() and path.stat().st_mtime >= started - 2, f"missing or stale output: {path}")
    return legacy.report(c)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture-only", action="store_true", help="only (re)write input/paperdoll/*_raw.png")
    args = parser.parse_args(argv)
    try:
        if args.fixture_only:
            write_fixtures()
            return 0
        return run()
    except Exception:
        traceback.print_exc()
        print("PIPELINE TEST FAILED: unhandled error (see traceback)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
