"""Full pipeline acceptance test for the pixel-anim pipeline.

Run from any directory:

    .venv/Scripts/python.exe scripts/test_pipeline.py            # full pipeline
    .venv/Scripts/python.exe scripts/test_pipeline.py --fixture-only

The script draws a deterministic, right-facing test hero on a pure #FF00FF
background (a procedural fixture, not an art deliverable), then calls the
importable server tools in pipeline order:

    preprocess_gpt_sprite -> inspect_sprite_ascii
    -> generate_character_animations -> export_game_assets

The head/torso/legs split rows are chosen by reading the ASCII dump returned by
inspect_sprite_ascii, classifying its legend colours into anatomical bands
(hair/skin, armor/belt, pants/boots) and cross-checking the result against the
fixture's known layout. Split rows use the server's inclusive semantics:
head is y <= head_end_y, torso is head_end_y < y <= torso_end_y, legs is
y > torso_end_y.

Outputs are verified from the files themselves: the clean PNG, the native
.aseprite binary (parsed here, chunk by chunk), the sheet PNG/JSON and the GIF.
Any failed check prints FAIL lines and the process exits with status 1.
The script overwrites only its own fixture and output paths; it deletes nothing.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import struct
import sys
import time
import traceback
import zlib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "mcp_server" / "server.py"
FIXTURE = ROOT / "input" / "test_hero.png"
CLEAN = ROOT / "input" / "test_hero_clean.png"
OUTPUT_DIR = ROOT / "output"
ASSET_NAME = "test_hero"
ASE = OUTPUT_DIR / "test_hero.aseprite"
SHEET = OUTPUT_DIR / "test_hero_sheet.png"
SHEET_JSON = OUTPUT_DIR / "test_hero_sheet.json"
GIF = OUTPUT_DIR / "test_hero_preview.gif"

CANVAS = 32
BASELINE = CANVAS - 2  # final opaque feet row
GIF_SCALE = 4
MAX_COLORS = 16
FRAME_COUNT = 16
EXPECTED_TAGS = [("idle", 0, 3), ("walk", 4, 9), ("attack", 10, 13), ("hit", 14, 15)]
EXPECTED_LAYERS = ["Shadow", "Legs", "Torso", "Head", "FX"]  # bottom to top

# ---------------------------------------------------------------------------
# Fixture: one glyph per logical pixel, scaled by FIXTURE_SCALE onto magenta.
# 27 rows tall so the processor's 27px content box maps it back 1:1
# (rows 4..30 of the 32px canvas). Band rows are part of the known layout.
# ---------------------------------------------------------------------------
FIXTURE_SCALE = 8
FIXTURE_MARGIN = 4  # logical pixels of magenta around the design
MAGENTA = (255, 0, 255)
PALETTE = {
    "O": (27, 27, 42),     # outline
    "H": (107, 58, 30),    # hair
    "S": (242, 192, 138),  # skin
    "E": (40, 36, 64),     # eye
    "A": (62, 111, 201),   # armor
    "L": (127, 168, 232),  # armor light
    "D": (42, 74, 138),    # armor dark
    "G": (232, 192, 64),   # gold trim
    "B": (122, 74, 34),    # belt
    "P": (58, 53, 80),     # pants
    "K": (90, 52, 24),     # boots
}
BAND_OF = {
    "H": "head", "S": "head", "E": "head",
    "A": "torso", "L": "torso", "D": "torso", "G": "torso", "B": "torso",
    "P": "legs", "K": "legs",
    "O": None,  # outline belongs to every band
}
DESIGN = [
    "....OOOOO.....",  # 0  head band: rows 0..7
    "...OHHHHHO....",
    "..OHHHHHHHO...",
    "..OHHHSSSSO...",
    "..OHHSSSESO...",  # eye on the right: the hero faces right
    "..OHHSSSSSSO..",
    "...OHSSSSSO...",
    "....OOSSOO....",
    "...OODAAAO....",  # 8  torso band: rows 8..17 (armor, belt)
    "..ODAALLAAO...",
    "..ODALGGLAO...",
    "..ODALGGLAAO..",
    "..ODAALLAADO..",
    "..ODAAAAAADO..",
    "..ODDAAAAADO..",
    "...ODAAAAAO...",
    "...OBBBGBBO...",
    "...OBBBBBBO...",
    "...OPPPPPPPO..",  # 18 legs band: rows 18..26 (pants, boots)
    "...OPPPOPPPO..",
    "...OPPPOPPPO..",
    "...OPPPOPPPO..",
    "...OPPPOPPPO..",
    "...OKKKOKKKO..",
    "...OKKKOKKKO..",
    "...OKKKKOKKKKO",
    "...OOOOOOOOOOO",
]
DESIGN_HEAD_END = 7    # last design row of the head band (inclusive)
DESIGN_TORSO_END = 17  # last design row of the torso band (inclusive)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, condition: bool, message: str) -> bool:
        if condition:
            self.passed += 1
        else:
            self.failures.append(message)
            print(f"FAIL: {message}")
        return bool(condition)

    def section(self, name: str, func, *args):
        print(f"\n== {name} ==")
        try:
            return func(self, *args)
        except Exception as exc:  # report and keep checking independent sections
            self.failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"FAIL: {name} raised")
            traceback.print_exc()
            return None


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
def design_bbox() -> tuple[int, int, int, int]:
    cells = [(x, y) for y, row in enumerate(DESIGN) for x, ch in enumerate(row) if ch != "."]
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def render_fixture() -> bytes:
    width = len(DESIGN[0]) + 2 * FIXTURE_MARGIN
    height = len(DESIGN) + 2 * FIXTURE_MARGIN
    image = Image.new("RGB", (width * FIXTURE_SCALE, height * FIXTURE_SCALE), MAGENTA)
    pixels = image.load()
    for y, row in enumerate(DESIGN):
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            color = PALETTE[ch]
            ox = (x + FIXTURE_MARGIN) * FIXTURE_SCALE
            oy = (y + FIXTURE_MARGIN) * FIXTURE_SCALE
            for dy in range(FIXTURE_SCALE):
                for dx in range(FIXTURE_SCALE):
                    pixels[ox + dx, oy + dy] = color
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def write_fixture() -> None:
    if any(len(row) != len(DESIGN[0]) for row in DESIGN):
        raise ValueError("fixture DESIGN rows must share one width")
    data = render_fixture()
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    if FIXTURE.is_file() and FIXTURE.read_bytes() == data:
        print(f"fixture unchanged: {FIXTURE}")
    else:
        FIXTURE.write_bytes(data)
        print(f"fixture written: {FIXTURE}")


def expected_clean() -> dict[tuple[int, int], tuple[int, int, int, int]]:
    """Clean image expected from an exact 1:1 recovery, bottom-center at BASELINE."""
    x0, y0, x1, y1 = design_bbox()
    w, h = x1 - x0, y1 - y0
    paste_x, paste_y = (CANVAS - w) // 2, BASELINE + 1 - h
    result = {}
    for y in range(y0, y1):
        for x in range(x0, x1):
            ch = DESIGN[y][x]
            if ch != ".":
                result[(paste_x + x - x0, paste_y + y - y0)] = (*PALETTE[ch], 255)
    return result


# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------
def load_server():
    if not SERVER_PATH.is_file():
        raise FileNotFoundError(f"server module missing: {SERVER_PATH}")
    spec = importlib.util.spec_from_file_location("pixel_anim_server_under_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("preprocess_gpt_sprite", "inspect_sprite_ascii",
                 "generate_character_animations", "export_game_assets"):
        if not callable(getattr(module, name, None)):
            raise AttributeError(f"server does not expose callable {name}()")
    return module


def same_path(a, b: Path) -> bool:
    return Path(str(a)).resolve() == b.resolve()


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
def nearest_glyph(rgb, tolerance: float = 48.0):
    best, best_d = None, None
    for glyph, ref in PALETTE.items():
        d = sum((a - b) ** 2 for a, b in zip(rgb, ref)) ** 0.5
        if best_d is None or d < best_d:
            best, best_d = glyph, d
    return best if best_d is not None and best_d <= tolerance else None


def is_magenta_like(rgb) -> bool:
    r, g, b = rgb[:3]
    return r >= 165 and b >= 165 and g <= 150 and min(r, b) - g >= 40


def opaque_map(image: Image.Image) -> dict[tuple[int, int], tuple[int, int, int, int]]:
    rgba = image.convert("RGBA")
    w = rgba.width
    return {(i % w, i // w): p for i, p in enumerate(rgba.getdata()) if p[3]}


# ---------------------------------------------------------------------------
# Clean PNG and ASCII
# ---------------------------------------------------------------------------
def check_clean(c: Checker) -> None:
    with Image.open(CLEAN) as source:
        c.check(source.size == (CANVAS, CANVAS), f"clean size {source.size} != {(CANVAS, CANVAS)}")
        rgba = source.convert("RGBA")
    alphas = {p[3] for p in rgba.getdata()}
    c.check(alphas <= {0, 255}, f"clean alpha not binary: {sorted(alphas)}")
    opaque = opaque_map(rgba)
    c.check(bool(opaque), "clean image is blank")
    ys = [y for _, y in opaque]
    c.check(max(ys) == BASELINE, f"clean bottom opaque row {max(ys)} != {BASELINE}")
    colors = {p[:3] for p in opaque.values()}
    c.check(len(colors) <= MAX_COLORS, f"clean uses {len(colors)} colours > {MAX_COLORS}")
    c.check(not any(is_magenta_like(p) for p in colors), "magenta remains in clean image")
    unknown = sorted(p for p in colors if nearest_glyph(p) is None)
    c.check(not unknown, f"clean colours not near the fixture palette: {unknown}")
    expected = expected_clean()
    diff = set(opaque) ^ set(expected)
    c.check(not diff, f"clean silhouette differs from the 1:1 fixture recovery at {len(diff)} px, e.g. {sorted(diff)[:5]}")
    wrong = [xy for xy in set(opaque) & set(expected) if opaque[xy] != expected[xy]]
    c.check(not wrong, f"clean colours differ from fixture at {len(wrong)} px, e.g. {sorted(wrong)[:5]}")
    eye = [x for (x, y), p in opaque.items() if nearest_glyph(p[:3]) == "E"]
    head = [x for (x, y), p in opaque.items() if BAND_OF.get(nearest_glyph(p[:3])) == "head"]
    c.check(bool(eye) and sum(eye) / len(eye) > sum(head) / len(head), "hero does not face right (eye not right of head centre)")
    print(f"clean: {len(opaque)} opaque px, {len(colors)} colours, rows {min(ys)}..{max(ys)}")


def parse_ascii(text: str):
    size = re.search(r"Size:\s*(\d+)x(\d+)", text)
    if not size:
        raise ValueError("ASCII dump has no 'Size: WxH' header")
    width, height = int(size.group(1)), int(size.group(2))
    legend = {}
    # Legend entries are #RRGGBB or #RRGGBBAA; only opaque colours are listed.
    for match in re.finditer(r"^(\S) = #([0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)\s*$", text, re.M):
        value = match.group(2)
        legend[match.group(1)] = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    rows = {}
    for match in re.finditer(r"^(\d+) \| (.*)$", text, re.M):
        rows[int(match.group(1))] = match.group(2).rstrip("\r")
    grid = [rows.get(y, "") for y in range(height)]
    if any(len(row) != width for row in grid):
        raise ValueError("ASCII dump rows do not match the declared width/height")
    return width, height, legend, grid


def choose_splits(c: Checker, ascii_text: str) -> tuple[int, int]:
    """Read the ASCII dump and pick inclusive anatomical split rows."""
    width, height, legend, grid = parse_ascii(ascii_text)
    c.check((width, height) == (CANVAS, CANVAS), f"ASCII size {width}x{height} != {CANVAS}x{CANVAS}")
    unknown = sorted(g for g in {ch for row in grid for ch in row} if g != "." and g not in legend)
    c.check(not unknown, f"ASCII glyphs missing from legend: {unknown}")

    # The ASCII must describe the clean PNG exactly.
    with Image.open(CLEAN) as source:
        png = opaque_map(source)
    from_ascii = {(x, y): (*legend[ch], 255) for y, row in enumerate(grid)
                  for x, ch in enumerate(row) if ch in legend}
    c.check(from_ascii == png, "ASCII dump does not reproduce the clean PNG pixels")

    bands: dict[str, list[int]] = {"head": [], "torso": [], "legs": []}
    for y, row in enumerate(grid):
        for ch in row:
            if ch in legend:
                band = BAND_OF.get(nearest_glyph(legend[ch]))
                if band:
                    bands[band].append(y)
    for name, rows in bands.items():
        if not rows:
            raise ValueError(f"no {name}-band colours found in the ASCII legend")
    print("band rows from ASCII legend:",
          ", ".join(f"{k} {min(v)}..{max(v)}" for k, v in bands.items()))
    head_end_y = min(bands["torso"]) - 1
    torso_end_y = min(bands["legs"]) - 1
    c.check(max(bands["head"]) <= head_end_y, "skin/hair rows overlap the armor band")
    c.check(max(bands["torso"]) <= torso_end_y, "armor/belt rows overlap the pants band")

    # Cross-check against the known fixture layout.
    top = min(y for y, row in enumerate(grid) if any(ch in legend for ch in row))
    known = (top + DESIGN_HEAD_END - design_bbox()[1], top + DESIGN_TORSO_END - design_bbox()[1])
    c.check((head_end_y, torso_end_y) == known,
            f"ASCII-derived splits {(head_end_y, torso_end_y)} != fixture layout {known}")
    print(f"selected splits (inclusive): head y<= {head_end_y}; torso {head_end_y} < y <= {torso_end_y}; legs y > {torso_end_y}")
    return head_end_y, torso_end_y


# ---------------------------------------------------------------------------
# Native .aseprite parser (format: aseprite/docs/ase-file-specs.md)
# ---------------------------------------------------------------------------
def _string(data: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<H", data, offset)
    start = offset + 2
    return data[start:start + length].decode("utf-8"), start + length


def parse_aseprite(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 128:
        raise ValueError("file shorter than the 128-byte header")
    file_size, magic, frames, width, height, depth = struct.unpack_from("<IHHHHH", data, 0)
    if magic != 0xA5E0:
        raise ValueError(f"bad .aseprite magic 0x{magic:04X}")
    if file_size != len(data):
        raise ValueError(f"header size {file_size} != file size {len(data)}")
    transparent_index = data[28]
    layers: list[str] = []
    tags: list[tuple[str, int, int]] = []
    durations: list[int] = []
    palette: dict[int, tuple[int, int, int, int]] = {}
    cels: list[dict[int, dict]] = []  # frame -> layer index -> cel
    offset = 128
    for frame in range(frames):
        frame_bytes, frame_magic, old_chunks, duration = struct.unpack_from("<IHHH", data, offset)
        (new_chunks,) = struct.unpack_from("<I", data, offset + 12)
        if frame_magic != 0xF1FA:
            raise ValueError(f"bad frame magic in frame {frame}")
        durations.append(duration)
        chunk_count = new_chunks or old_chunks
        frame_cels: dict[int, dict] = {}
        pos = offset + 16
        for _ in range(chunk_count):
            size, ctype = struct.unpack_from("<IH", data, pos)
            body = pos + 6
            if ctype == 0x2004:  # layer
                name, _ = _string(data, body + 16)
                layers.append(name)
            elif ctype == 0x2018:  # tags
                (count,) = struct.unpack_from("<H", data, body)
                p = body + 10
                for _ in range(count):
                    first, last = struct.unpack_from("<HH", data, p)
                    name, p = _string(data, p + 17)
                    tags.append((name, first, last))
            elif ctype == 0x2019:  # palette
                _, first, last = struct.unpack_from("<III", data, body)
                p = body + 20
                for index in range(first, last + 1):
                    (flags,) = struct.unpack_from("<H", data, p)
                    palette[index] = tuple(data[p + 2:p + 6])
                    p += 6
                    if flags & 1:
                        _, p = _string(data, p)
            elif ctype == 0x2005:  # cel
                layer_index, x, y, opacity, cel_type = struct.unpack_from("<HhhBH", data, body)
                p = body + 16
                cel = {"x": x, "y": y, "opacity": opacity, "type": cel_type}
                if cel_type == 1:
                    (cel["link"],) = struct.unpack_from("<H", data, p)
                elif cel_type in (0, 2):
                    w, h = struct.unpack_from("<HH", data, p)
                    raw = data[p + 4:pos + size]
                    cel.update(w=w, h=h, pixels=raw if cel_type == 0 else zlib.decompress(raw))
                frame_cels[layer_index] = cel
            pos += size
        cels.append(frame_cels)
        offset += frame_bytes
    bpp = {32: 4, 16: 2, 8: 1}.get(depth)
    if bpp is None:
        raise ValueError(f"unsupported colour depth {depth}")

    def decode(cel: dict) -> dict[tuple[int, int], tuple[int, int, int, int]]:
        out = {}
        raw = cel["pixels"]
        for i in range(cel["w"] * cel["h"]):
            chunk = raw[i * bpp:(i + 1) * bpp]
            if bpp == 4:
                rgba = tuple(chunk)
            elif bpp == 2:
                rgba = (chunk[0], chunk[0], chunk[0], chunk[1])
            else:
                rgba = (0, 0, 0, 0) if chunk[0] == transparent_index else palette.get(chunk[0], (0, 0, 0, 0))
            if rgba[3]:
                out[(cel["x"] + i % cel["w"], cel["y"] + i // cel["w"])] = rgba
        return out

    images: list[dict[str, dict]] = []
    for frame in range(frames):
        per_layer = {}
        for layer_index, name in enumerate(layers):
            cel = cels[frame].get(layer_index)
            if cel and cel["type"] == 1:
                cel = cels[cel["link"]].get(layer_index)
            per_layer[name] = decode(cel) if cel and "pixels" in cel else {}
        images.append(per_layer)
    return {"frames": frames, "width": width, "height": height, "depth": depth,
            "layers": layers, "tags": tags, "durations": durations, "images": images}


def check_native(c: Checker, splits: tuple[int, int]) -> dict:
    ase = parse_aseprite(ASE)
    c.check(ase["frames"] == FRAME_COUNT, f"native frames {ase['frames']} != {FRAME_COUNT}")
    c.check((ase["width"], ase["height"]) == (CANVAS, CANVAS), f"native size {ase['width']}x{ase['height']}")
    c.check(ase["layers"] == EXPECTED_LAYERS, f"native layers {ase['layers']} != {EXPECTED_LAYERS}")
    c.check(ase["tags"] == EXPECTED_TAGS, f"native tags {ase['tags']} != {EXPECTED_TAGS}")
    c.check(all(d > 0 for d in ase["durations"]), f"native durations not positive: {ase['durations']}")
    print(f"native: {ase['frames']} frames, depth {ase['depth']}, layers {ase['layers']}")
    print(f"native tags: {ase['tags']}")
    print(f"native durations (ms): {ase['durations']}")
    images = ase["images"]
    if len(images) != FRAME_COUNT or ase["layers"] != EXPECTED_LAYERS:
        return ase

    for f, frame in enumerate(images):
        for name in ("Legs", "Torso", "Head"):
            c.check(bool(frame[name]), f"frame {f}: layer {name} is empty")
        for name in ("Legs", "Torso", "Head", "FX"):
            low = [y for _, y in frame[name] if y > BASELINE]
            c.check(not low, f"frame {f}: layer {name} draws below baseline row {BASELINE}")

    # Inclusive split semantics on the rest pose (frame 0).
    head_end_y, torso_end_y = splits
    rest = images[0]
    for name, lo, hi in (("Head", 0, head_end_y), ("Torso", head_end_y + 1, torso_end_y),
                         ("Legs", torso_end_y + 1, BASELINE)):
        rows = [y for _, y in rest[name]]
        if rows:
            c.check(lo <= min(rows) and max(rows) <= hi,
                    f"frame 0 {name} rows {min(rows)}..{max(rows)} outside inclusive band {lo}..{hi}")
    composite = {**rest["Legs"], **rest["Torso"], **rest["Head"]}
    with Image.open(CLEAN) as source:
        clean = opaque_map(source)
    c.check(composite == clean, "frame 0 Head+Torso+Legs does not reproduce the clean base")

    # Idle: feet planted on the baseline, same leg pixels, upper body moves.
    idle_legs = [images[f]["Legs"] for f in range(0, 4)]
    for f, legs in enumerate(idle_legs):
        bottom = max((y for _, y in legs), default=-1)
        c.check(bottom == BASELINE, f"idle frame {f}: feet bottom row {bottom} != H-2 ({BASELINE})")
    c.check(all(legs == idle_legs[0] for legs in idle_legs), "idle legs move between frames (feet not planted)")
    contact = sorted(x for x, y in idle_legs[0] if y == BASELINE)
    print(f"idle feet baseline row {BASELINE}, contact columns {contact[0]}..{contact[-1]}" if contact else "idle: no contact row")
    upper = {tuple(sorted({**images[f]["Torso"], **images[f]["Head"]}.items())) for f in range(4)}
    c.check(len(upper) >= 2, "idle upper body never changes (no breathing pose)")

    # Walk: one support foot on the baseline every frame, legs change.
    walk = range(4, 10)
    for f in walk:
        bottom = max((y for _, y in images[f]["Legs"]), default=-1)
        c.check(bottom == BASELINE, f"walk frame {f}: no support foot on baseline (bottom {bottom})")
    c.check(len({tuple(sorted(images[f]["Legs"].items())) for f in walk}) >= 2, "walk legs never change")

    c.check(any(images[f]["FX"] for f in range(10, 14)), "no FX pixels in any attack frame")
    return ase


# ---------------------------------------------------------------------------
# Sheet PNG + JSON
# ---------------------------------------------------------------------------
def check_sheet(c: Checker, native: dict | None) -> None:
    meta_doc = json.loads(SHEET_JSON.read_text(encoding="utf-8"))
    frames = meta_doc.get("frames")
    if isinstance(frames, dict):
        frames = list(frames.values())
    c.check(isinstance(frames, list) and len(frames) == FRAME_COUNT,
            f"JSON frame count {len(frames) if isinstance(frames, list) else frames!r} != {FRAME_COUNT}")
    meta = meta_doc.get("meta", {})
    tags = [(t.get("name"), t.get("from"), t.get("to")) for t in meta.get("frameTags", [])]
    c.check(tags == EXPECTED_TAGS, f"JSON frameTags {tags} != {EXPECTED_TAGS}")
    layers = [layer.get("name") for layer in meta.get("layers", [])]
    c.check(layers == EXPECTED_LAYERS, f"JSON layers {layers} != {EXPECTED_LAYERS}")
    with Image.open(SHEET) as source:
        sheet = source.convert("RGBA")
    size = meta.get("size", {})
    c.check((size.get("w"), size.get("h")) == sheet.size, f"JSON meta.size {size} != sheet {sheet.size}")
    c.check(sheet.getbbox() is not None, "sheet PNG is blank")
    print(f"sheet: {sheet.size[0]}x{sheet.size[1]}, tags {tags}")
    durations = []
    for i, entry in enumerate(frames or []):
        rect = entry.get("frame", {})
        x, y, w, h = (rect.get(k) for k in ("x", "y", "w", "h"))
        source_size = entry.get("sourceSize", {})
        c.check((w, h) == (CANVAS, CANVAS), f"JSON frame {i} rect {w}x{h} != {CANVAS}x{CANVAS}")
        c.check((source_size.get("w"), source_size.get("h")) == (CANVAS, CANVAS), f"JSON frame {i} sourceSize {source_size}")
        if None in (x, y, w, h) or x < 0 or y < 0 or x + w > sheet.width or y + h > sheet.height:
            c.check(False, f"JSON frame {i} rect {rect} outside the sheet")
            continue
        c.check(sheet.crop((x, y, x + w, y + h)).getbbox() is not None, f"sheet frame {i} is blank")
        durations.append(entry.get("duration"))
    c.check(all(isinstance(d, int) and d > 0 for d in durations), f"JSON durations invalid: {durations}")
    if native:
        c.check(durations == native["durations"], f"JSON durations {durations} != native {native['durations']}")


# ---------------------------------------------------------------------------
# GIF preview
# ---------------------------------------------------------------------------
def check_gif(c: Checker, native: dict | None) -> None:
    frames, durations = [], []
    with Image.open(GIF) as gif:
        size = gif.size
        count = getattr(gif, "n_frames", 1)
        for index in range(count):
            gif.seek(index)
            frames.append(gif.convert("RGBA").tobytes())
            durations.append(gif.info.get("duration"))
    expected = (CANVAS * GIF_SCALE, CANVAS * GIF_SCALE)
    c.check(size == expected, f"GIF size {size} != {expected}")
    c.check(count == FRAME_COUNT, f"GIF frames {count} != {FRAME_COUNT} (identical adjacent frames get merged)")
    print(f"gif: {size[0]}x{size[1]}, {count} frames, durations {durations}")
    for i in range(1, len(frames)):
        c.check(frames[i] != frames[i - 1], f"GIF frames {i - 1} and {i} are identical poses")
    if count == FRAME_COUNT:
        for name, first, last in EXPECTED_TAGS:
            c.check(len(set(frames[first:last + 1])) >= 2, f"GIF tag {name} shows a single pose")
        if native:
            c.check(durations == native["durations"], f"GIF durations {durations} != native {native['durations']}")


# ---------------------------------------------------------------------------
def check_fresh(c: Checker, started: float) -> None:
    for path in (CLEAN, ASE, SHEET, SHEET_JSON, GIF):
        ok = path.is_file() and path.stat().st_size > 0
        c.check(ok, f"missing or empty output: {path}")
        if ok:
            c.check(path.stat().st_mtime >= started - 2, f"stale output not rewritten by this run: {path}")


def run_pipeline() -> int:
    c = Checker()
    started = time.time()
    write_fixture()
    server = load_server()

    print("\n== 1. preprocess_gpt_sprite ==")
    result = server.preprocess_gpt_sprite(str(FIXTURE), str(CLEAN), canvas_size=CANVAS, max_colors=MAX_COLORS)
    c.check(same_path(result, CLEAN), f"preprocess returned {result!r}, expected {CLEAN}")
    c.section("clean base checks", check_clean)

    print("\n== 2. inspect_sprite_ascii ==")
    ascii_text = server.inspect_sprite_ascii(str(CLEAN))
    print(ascii_text)
    splits = c.section("choose anatomical splits from ASCII", choose_splits, ascii_text)
    if splits is None:
        return report(c)

    print("\n== 3. generate_character_animations ==")
    result = server.generate_character_animations(str(CLEAN), str(ASE), splits[0], splits[1])
    c.check(same_path(result, ASE), f"generate returned {result!r}, expected {ASE}")

    print("\n== 4. export_game_assets ==")
    exported = server.export_game_assets(str(ASE), str(OUTPUT_DIR), ASSET_NAME)
    for key, path in (("gif", GIF), ("sheet", SHEET), ("json", SHEET_JSON)):
        c.check(isinstance(exported, dict) and same_path(exported.get(key, ""), path),
                f"export result[{key!r}] = {exported.get(key) if isinstance(exported, dict) else exported!r}, expected {path}")

    c.section("output freshness", check_fresh, started)
    native = c.section("native .aseprite", check_native, splits)
    c.section("sheet PNG + JSON", check_sheet, native)
    c.section("GIF preview", check_gif, native)
    return report(c)


def report(c: Checker) -> int:
    print(f"\n{c.passed} checks passed, {len(c.failures)} failed")
    if c.failures:
        print("PIPELINE TEST FAILED:")
        for failure in c.failures:
            print(f"  - {failure}")
        return 1
    print("PIPELINE TEST PASSED")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture-only", action="store_true",
                        help="only (re)write input/test_hero.png and exit")
    args = parser.parse_args(argv)
    try:
        if args.fixture_only:
            write_fixture()
            return 0
        return run_pipeline()
    except Exception:
        traceback.print_exc()
        print("PIPELINE TEST FAILED: unhandled error (see traceback)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
