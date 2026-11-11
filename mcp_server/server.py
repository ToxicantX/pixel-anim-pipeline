"""Local-only sprite automation MCP server. Relative paths use PROJECT_ROOT."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from mcp.server.fastmcp import FastMCP
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
mcp = FastMCP("Pixel-Animator-Pro")
TIMEOUT = 120
TAGS = (("idle", 1, 4), ("walk", 5, 10), ("attack", 11, 14), ("hit", 15, 16))
DURATIONS = [180, 120, 180, 120, 100, 80, 100, 100, 80, 100, 140, 60, 80, 180, 80, 180]
WEAPON_TOOLS_PATH = PROJECT_ROOT / "mcp_server" / "weapon_tools.py"


def _path(value: str, exists: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("Expected a nonempty path without NUL characters")
    path = Path(value).expanduser()
    path = (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if exists and not path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    return path


def _aseprite() -> str:
    configured = os.environ.get("ASEPRITE_PATH")
    if not configured:
        config = PROJECT_ROOT / "mcp_server" / "config.json"
        if config.exists():
            data = json.loads(config.read_text(encoding="utf-8"))
            configured = data.get("aseprite_path")
    if configured:
        path = _path(configured, exists=True)
        return str(path)
    executable = shutil.which("aseprite")
    if not executable:
        raise RuntimeError("Aseprite not found. Set ASEPRITE_PATH or mcp_server/config.json aseprite_path to its executable.")
    return executable


def _run(args: list[str]) -> str:
    command = [_aseprite(), "--batch", *args]
    try:
        result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Aseprite timed out after {TIMEOUT}s; check the script/input and executable.") from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot launch Aseprite: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"Aseprite exited {result.returncode}: {result.stderr.strip()} {result.stdout.strip()}")
    return result.stdout.strip()


def _lua_string(value: str) -> str:
    # JSON quoting handles controls; literal UTF-8 avoids unsupported Lua \u escapes.
    return '"' + "".join(
        "\\" + f"{ord(char):03d}" if ord(char) < 32
        else json.dumps(char, ensure_ascii=False)[1:-1] for char in value
    ) + '"'


def run_custom_lua(lua_script: str) -> str:
    """Execute arbitrary TRUSTED LOCAL Lua with this process's filesystem permissions.

    Not a sandbox: never supply scripts from untrusted users or remote content.
    """
    if not isinstance(lua_script, str) or not lua_script.strip():
        raise ValueError("lua_script must be nonempty trusted local Lua")
    with tempfile.TemporaryDirectory(prefix="pixel_anim_") as temp:
        script = Path(temp) / "script.lua"
        script.write_text(lua_script, encoding="utf-8")
        return _run(["--script", str(script)])


def preprocess_gpt_sprite(input_path: str, output_path: str, canvas_size: int = 32,
                          max_colors: int = 16, add_outline: bool = False,
                          use_endesga_palette: bool = False) -> str:
    """Normalize an image using the project's bottom-center pixel processor."""
    source, target = _path(input_path, True), _path(output_path)
    if type(canvas_size) is not int or not 8 <= canvas_size <= 1024:
        raise ValueError("canvas_size must be an integer from 8 to 1024")
    if type(max_colors) is not int or not 2 <= max_colors <= 256:
        raise ValueError("max_colors must be an integer from 2 to 256")
    target.parent.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("pixel_anim_processor", PROJECT_ROOT / "scripts" / "pixel_processor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.process_sprite(str(source), str(target), canvas_size, max_colors, add_outline, use_endesga_palette)
    _require_output(target)
    return str(target)


def inspect_sprite_ascii(sprite_path: str) -> str:
    """Return dimensions, opaque bounds, color legend and one ASCII cell per pixel."""
    with Image.open(_path(sprite_path, True)) as source:
        image = source.convert("RGBA")
    if image.width > 256 or image.height > 256:
        raise ValueError("ASCII inspection supports images up to 256x256")
    colors = sorted({p for p in image.getdata() if p[3]})
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz@#$%&*+=?"
    if len(colors) > len(alphabet):
        raise ValueError("Too many colors for ASCII inspection; preprocess to <= 64 colors")
    legend = {color: alphabet[i] for i, color in enumerate(colors)}
    rows = ["".join(legend.get(image.getpixel((x, y)), ".") for x in range(image.width)) for y in range(image.height)]
    header = f"Size: {image.width}x{image.height}\nOpaque bounds: {image.getbbox()}\nLegend: . = transparent\n"
    labels = "\n".join(f"{symbol} = #{''.join(f'{v:02X}' for v in color)}" for color, symbol in legend.items())
    digits = max(2, len(str(image.height - 1)))
    prefix = " " * (digits + 3)
    rulers = []
    if image.width >= 100:
        rulers.append(prefix + "".join(str(x // 100 % 10) for x in range(image.width)))
    if image.width >= 10:
        rulers.append(prefix + "".join(str(x // 10 % 10) for x in range(image.width)))
    rulers.append(prefix + "".join(str(x % 10) for x in range(image.width)))
    numbered = [f"{y:0{digits}d} | {row}" for y, row in enumerate(rows)]
    return header + labels + "\n" + "\n".join(rulers + numbered)


def _require_output(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Aseprite/processor did not produce a nonempty output: {path}")


# ---------------------------------------------------------------------------
# Shared rig generator
# ---------------------------------------------------------------------------
# Per-frame torso pose used by the socket rig. The legacy rig keeps the older
# inline lean/bob formula; only the socket rig varies the torso to this table.
_IDLE_TORSO_DY = (0, 1, 1, 0)
_WALK_TORSO_DY = (0, 1, 0, 0, 0, 1)
_ATTACK_TORSO = {11: (-1, 1), 12: (2, 0), 13: (1, 0), 14: (0, 0)}
_HIT_TORSO = {15: (-2, -1), 16: (-1, 0)}
# Grip offsets applied on top of the torso displacement, in canvas pixels.
_WALK_GRIP = ((1, 0), (1, 0), (0, 0), (-1, 0), (-1, 0), (0, 0))  # frames 5..10
_ATTACK_GRIP = {11: (-2, -2), 12: (3, 0), 13: (2, 2), 14: (0, 0)}
_HIT_GRIP = {15: (-1, -1), 16: (-1, 0)}
# Grip socket data-pixel color protocol per frame (annotation angle in comments).
_SOCKET_COLORS = {
    1: (255, 0, 0), 2: (255, 0, 0), 3: (255, 0, 0), 4: (255, 0, 0),
    5: (255, 0, 0), 6: (255, 0, 0), 7: (255, 0, 0), 8: (255, 0, 0),
    9: (255, 0, 0), 10: (255, 0, 0), 11: (0, 255, 0), 12: (0, 0, 255),
    13: (255, 255, 0), 14: (255, 0, 0), 15: (255, 0, 255), 16: (255, 0, 255),
}
_SOCKET_ANGLES = {
    1: 15, 2: 15, 3: 15, 4: 15, 5: 15, 6: 15, 7: 15, 8: 15, 9: 15, 10: 15,
    11: -45, 12: 90, 13: 135, 14: 15, 15: -25, 16: -25,
}


def _torso_pose(frame: int) -> tuple[int, int]:
    """Torso displacement (dx, dy) in canvas pixels for one frame."""
    if 1 <= frame <= 4:
        return (0, _IDLE_TORSO_DY[frame - 1])
    if 5 <= frame <= 10:
        return (0, _WALK_TORSO_DY[frame - 5])
    if 11 <= frame <= 14:
        return _ATTACK_TORSO[frame]
    return _HIT_TORSO[frame]


def _grip_offset(frame: int) -> tuple[int, int]:
    """Grip offset (dx, dy) applied on top of the torso pose for one frame."""
    if 1 <= frame <= 4:
        return (0, 0)
    if 5 <= frame <= 10:
        return _WALK_GRIP[frame - 5]
    if 11 <= frame <= 14:
        return _ATTACK_GRIP[frame]
    return _HIT_GRIP[frame]


def _socket_plan(width: int, height: int, ground_shift: int, hand_x: int, hand_y: int):
    """Return (torso_poses, socket_frames) for all 16 frames.

    Each socket frame is (x, y, r, g, b). A socket that would fall outside the
    canvas is rejected with ValueError rather than silently clamped.
    """
    poses = []
    frames = []
    for frame in range(1, 17):
        dx, dy = _torso_pose(frame)
        poses.append((dx, dy))
        gx, gy = _grip_offset(frame)
        x = hand_x + dx + gx
        y = hand_y + ground_shift + dy + gy
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(
                f"grip socket for frame {frame} falls outside the {width}x{height} canvas at ({x},{y})"
            )
        r, g, b = _SOCKET_COLORS[frame]
        frames.append((x, y, r, g, b))
    return poses, frames


_RIG_BODY = r'''local s=Sprite(W,H,ColorMode.RGB)
local shadow=s.layers[1]; shadow.name="Shadow"
local legs=s:newLayer(); legs.name="Legs"
local torso=s:newLayer(); torso.name="Torso"
local head=s:newLayer(); head.name="Head"
@@LAYER_EXTRA@@local fx=s:newLayer(); fx.name="FX"
@@GRIP_LAYER@@local layers={head,torso,legs}
local torsoTop=H
for _,p in ipairs(parts[2]) do torsoTop=math.min(torsoTop,p[2]) end
local idleHead={0,0,1,1}
local idleTorso={0,1,1,0}
local legMinX,legMaxX=W,0
for _,p in ipairs(parts[3]) do
  legMinX=math.min(legMinX,p[1]); legMaxX=math.max(legMaxX,p[1])
end
local legMiddle=(legMinX+legMaxX)/2
local footBottom={}
for _,p in ipairs(parts[3]) do
  local side=p[1]<=legMiddle and 1 or 2
  footBottom[side]=math.max(footBottom[side] or -1,p[2]+groundShift)
end
local function put(img,x,y,c)
  x=math.max(0,math.min(W-1,x)); y=math.max(0,math.min(H-2,y))
  img:drawPixel(x,y,c)
end
@@TABLES@@for f=1,16 do
  if f>1 then s:newEmptyFrame(f) end
  s.frames[f].duration=durations[f]/1000
  local bob=({0,1,0,0})[(f-1)%4+1]
  local lean=0
  if f>=11 and f<=14 then lean=({-1,1,2,0})[f-10]; bob=0 end
  if f>=15 then lean=-(17-f); bob=0 end
  for part=1,3 do
    local img=Image(W,H,ColorMode.RGB)
    for _,p in ipairs(parts[part]) do
      local x,y=p[1],p[2]+groundShift
      local dx,dy=lean,bob
      if f<=4 then
        if part==1 then dy=idleHead[f] end
@@IDLE_TORSO@@      end
@@TORSO_POSE@@      if part==3 then
        dx=0; dy=0
        if f>=5 and f<=10 then
          local phase=f-5
          local support=phase<3 and 1 or 2
          if not footBottom[support] then support=3-support end
          local side=x<=legMiddle and 1 or 2
          dy=H-2-footBottom[side]
          if side~=support then
            dx=({-1,0,1,1,0,-1})[phase+1]
            dy=dy-1-(phase%3==1 and 1 or 0)
          end
        end
      end
      local c=app.pixelColor.rgba(p[3],p[4],p[5],p[6])
      if f==15 then c=app.pixelColor.rgba(255,255,255,p[6]) end
      put(img,x+dx,y+dy,c)
      -- Bridge only the neck seam opened by the phase-offset torso pose.
      if f<=4 and part==2 and idleHead[f]==0 and idleTorso[f]==1 and p[2]==torsoTop then
        put(img,x,y,c)
      end
    end
    s:newCel(layers[part],f,img,Point(0,0))
  end
  local sh=Image(W,H,ColorMode.RGB)
  for x=math.floor(W/2)-3,math.floor(W/2)+3 do
    sh:drawPixel(x,H-1,app.pixelColor.rgba(25,25,35,90))
  end
  s:newCel(shadow,f,sh,Point(0,0))
  if f==12 or f==13 then
    local effect=Image(W,H,ColorMode.RGB)
    for t=0,math.floor(H/3) do
      local x=W-3-math.floor(t*t/H)
      local y=math.floor(H/4)+t+(f-12)
      put(effect,x,y,app.pixelColor.rgba(255,240,160,255))
      put(effect,x-1,y,app.pixelColor.rgba(255,255,255,255))
    end
    s:newCel(fx,f,effect,Point(0,0))
  end
@@GRIP_DRAW@@end
'''


def _lua_pairs(pairs) -> str:
    return "{" + ",".join("{" + str(a) + "," + str(b) + "}" for a, b in pairs) + "}"


def _build_rig_lua(width: int, height: int, parts, ground_shift: int, target: Path,
                   socket_frames=None, torso_poses=None) -> str:
    """Assemble the Aseprite Lua rig.

    With socket_frames/torso_poses omitted this emits the original five-layer rig
    byte-for-byte. Supplying them emits the seven-layer socket rig
    (Shadow, Legs, Torso, Head, Weapon, FX, _GripSocket).
    """
    socket = socket_frames is not None
    lua = "local W,H=" + str(width) + "," + str(height) + "\n"
    lua += "local parts=" + "{" + ",".join(
        "{" + ",".join("{" + ",".join(map(str, p)) + "}" for p in region) + "}"
        for region in parts) + "}\n"
    lua += "local groundShift=" + str(ground_shift) + "\n"
    lua += "local durations={" + ",".join(map(str, DURATIONS)) + "}\n"
    lua += "\n"

    if socket:
        layer_extra = 'local weapon=s:newLayer(); weapon.name="Weapon"\n'
        grip_layer = 'local grip=s:newLayer(); grip.name="_GripSocket"; grip.isVisible=false\n'
        tables = (
            "local torsoPose=" + _lua_pairs(torso_poses) + "\n"
            "local socketFrames={" + ",".join(
                "{" + ",".join(map(str, frame)) + "}" for frame in socket_frames) + "}\n"
        )
        idle_torso = ""
        torso_pose_line = "      if part==2 then dx=torsoPose[f][1]; dy=torsoPose[f][2] end\n"
        grip_draw = (
            "  local gripImg=Image(W,H,ColorMode.RGB)\n"
            "  local gs=socketFrames[f]\n"
            "  gripImg:drawPixel(gs[1],gs[2],app.pixelColor.rgba(gs[3],gs[4],gs[5],255))\n"
            "  s:newCel(grip,f,gripImg,Point(0,0))\n"
        )
    else:
        layer_extra = ""
        grip_layer = ""
        tables = ""
        idle_torso = "        if part==2 then dy=idleTorso[f] end\n"
        torso_pose_line = ""
        grip_draw = ""

    body = (_RIG_BODY
            .replace("@@LAYER_EXTRA@@", layer_extra)
            .replace("@@GRIP_LAYER@@", grip_layer)
            .replace("@@TABLES@@", tables)
            .replace("@@IDLE_TORSO@@", idle_torso)
            .replace("@@TORSO_POSE@@", torso_pose_line)
            .replace("@@GRIP_DRAW@@", grip_draw))
    lua += body
    for name, first, last in TAGS:
        lua += f"s:newTag({first},{last}).name={_lua_string(name)}\n"
    lua += f"s:saveAs({_lua_string(str(target))})\n"
    return lua


def _split_parts(image: Image.Image, head_end_y: int, torso_end_y: int):
    w, h = image.size
    pixels = []
    for start, end in ((0, head_end_y + 1), (head_end_y + 1, torso_end_y + 1), (torso_end_y + 1, h)):
        region = [(x, y, *image.getpixel((x, y))) for y in range(start, end) for x in range(w) if image.getpixel((x, y))[3]]
        if not region:
            raise ValueError("Head, torso and legs must each contain opaque pixels; adjust split rows")
        pixels.append(region)
    return pixels


def generate_character_animations(base_png_path: str, output_ase_path: str,
                                  head_end_y: int, torso_end_y: int) -> str:
    """Rig a PNG into 16 frames. Split rows are inclusive canvas Y end coordinates."""
    source, target = _path(base_png_path, True), _path(output_ase_path)
    if target.suffix.lower() not in (".ase", ".aseprite"):
        raise ValueError("output_ase_path must end in .ase or .aseprite")
    with Image.open(source) as png:
        if png.format != "PNG":
            raise ValueError("base_png_path must be a PNG")
        image = png.convert("RGBA")
    w, h = image.size
    if not (8 <= w <= 256 and 8 <= h <= 256):
        raise ValueError("Rig canvas must be between 8 and 256 pixels per dimension")
    if type(head_end_y) is not int or type(torso_end_y) is not int or not 0 <= head_end_y < torso_end_y < h - 1:
        raise ValueError("Require 0 <= head_end_y < torso_end_y < image height - 1 (inclusive ends)")
    pixels = _split_parts(image, head_end_y, torso_end_y)
    # Embed canvas coordinates, avoiding cropped PNG cel offsets and indexed-mode ambiguity.
    bottom = max(p[1] for region in pixels for p in region)
    top = min(p[1] for region in pixels for p in region)
    if bottom - top > h - 3:
        raise ValueError("Sprite needs a transparent top margin to ground feet at H-2")
    target.parent.mkdir(parents=True, exist_ok=True)
    lua = _build_rig_lua(w, h, pixels, h - 2 - bottom, target)
    run_custom_lua(lua)
    _require_output(target)
    return str(target)


def generate_character_with_socket(base_png_path: str, output_ase_path: str,
                                   head_end_y: int, torso_end_y: int,
                                   hand_x: int, hand_y: int) -> str:
    """Rig a PNG into 16 frames with a hidden one-pixel _GripSocket data layer.

    Layer order bottom-to-top: Shadow, Legs, Torso, Head, Weapon (empty), FX,
    _GripSocket (hidden). Every frame carries exactly one opaque pixel in
    _GripSocket whose canvas position is the base hand pixel posed by that
    frame's torso displacement plus the frame's grip offset. Its color encodes
    the weapon angle: RED 15deg (frames 1-10, 14), GREEN -45 (11), BLUE 90 (12),
    YELLOW 135 (13), MAGENTA -25 (15-16).
    """
    source, target = _path(base_png_path, True), _path(output_ase_path)
    if target.suffix.lower() not in (".ase", ".aseprite"):
        raise ValueError("output_ase_path must end in .ase or .aseprite")
    with Image.open(source) as png:
        if png.format != "PNG":
            raise ValueError("base_png_path must be a PNG")
        image = png.convert("RGBA")
    w, h = image.size
    if not (8 <= w <= 256 and 8 <= h <= 256):
        raise ValueError("Rig canvas must be between 8 and 256 pixels per dimension")
    if type(head_end_y) is not int or type(torso_end_y) is not int or not 0 <= head_end_y < torso_end_y < h - 1:
        raise ValueError("Require 0 <= head_end_y < torso_end_y < image height - 1 (inclusive ends)")
    if type(hand_x) is not int or type(hand_y) is not int:
        raise ValueError("hand_x and hand_y must be integers")
    if not (0 <= hand_x < w and 0 <= hand_y < h):
        raise ValueError("base hand must lie inside the canvas")
    if not (head_end_y < hand_y <= torso_end_y):
        raise ValueError("base hand must lie in the torso band (head_end_y < hand_y <= torso_end_y)")
    if not image.getpixel((hand_x, hand_y))[3]:
        raise ValueError("base hand must be an opaque pixel, not transparent")
    pixels = _split_parts(image, head_end_y, torso_end_y)
    bottom = max(p[1] for region in pixels for p in region)
    top = min(p[1] for region in pixels for p in region)
    if bottom - top > h - 3:
        raise ValueError("Sprite needs a transparent top margin to ground feet at H-2")
    ground_shift = h - 2 - bottom
    torso_poses, socket_frames = _socket_plan(w, h, ground_shift, hand_x, hand_y)
    target.parent.mkdir(parents=True, exist_ok=True)
    lua = _build_rig_lua(w, h, pixels, ground_shift, target,
                         socket_frames=socket_frames, torso_poses=torso_poses)
    run_custom_lua(lua)
    _require_output(target)
    return str(target)


def _load_weapon_tools():
    """Load mcp_server/weapon_tools.py by file path (no cyclic server import)."""
    if not WEAPON_TOOLS_PATH.is_file():
        raise RuntimeError(f"weapon_tools module is unavailable: {WEAPON_TOOLS_PATH}")
    name = "pixel_anim_weapon_tools"
    spec = importlib.util.spec_from_file_location(name, WEAPON_TOOLS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load weapon_tools from {WEAPON_TOOLS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def inspect_weapon_ascii(weapon_png_path: str) -> str:
    """Return an ASCII dump of a weapon PNG via the weapon_tools module."""
    weapon_path = _path(weapon_png_path, exists=True)
    return _load_weapon_tools().inspect_weapon_ascii(weapon_path)


def equip_and_export_weapon(character_ase_path: str, weapon_png_path: str, output_dir: str,
                            weapon_id: str, pivot_x: int, pivot_y: int) -> dict:
    """Equip a weapon onto a character and export it via the weapon_tools module."""
    character_path = _path(character_ase_path, exists=True)
    weapon_path = _path(weapon_png_path, exists=True)
    directory = _path(output_dir)
    if not isinstance(weapon_id, str) or not weapon_id.strip():
        raise ValueError("weapon_id must be a nonempty string")
    if type(pivot_x) is not int or type(pivot_y) is not int:
        raise ValueError("pivot_x and pivot_y must be integers")
    module = _load_weapon_tools()
    return module.equip_and_export_weapon(
        character_path, weapon_path, directory, weapon_id, pivot_x, pivot_y,
        run_lua=run_custom_lua, run_aseprite=_run, lua_string=_lua_string)


def export_game_assets(ase_path: str, output_dir: str, asset_name: str) -> dict[str, str]:
    """Export checked animated GIF, PNG sheet and JSON with tags/layers."""
    source, directory = _path(ase_path, True), _path(output_dir)
    if not asset_name or asset_name in (".", "..") or any(c in asset_name for c in '/\\<>:"|?*\0'):
        raise ValueError("asset_name must be a safe filename stem")
    directory.mkdir(parents=True, exist_ok=True)
    gif = directory / f"{asset_name}_preview.gif"
    sheet = directory / f"{asset_name}_sheet.png"
    data = directory / f"{asset_name}_sheet.json"
    _run([str(source), "--scale", "4", "--save-as", str(gif)])
    _require_output(gif)
    _run(["--list-tags", "--list-layers", str(source), "--sheet-type", "horizontal",
          "--sheet", str(sheet), "--format", "json-array", "--data", str(data)])
    for path in (sheet, data):
        _require_output(path)
    metadata = json.loads(data.read_text(encoding="utf-8"))
    if not metadata.get("frames") or not metadata.get("meta", {}).get("frameTags") or not metadata.get("meta", {}).get("layers"):
        raise RuntimeError("Export JSON is missing frames, frameTags or layers")
    with Image.open(gif) as img:
        img.verify()
    with Image.open(sheet) as img:
        img.verify()
    return {"gif": str(gif), "sheet": str(sheet), "json": str(data)}


# Register explicitly while retaining directly callable public functions.
for _tool in (preprocess_gpt_sprite, inspect_sprite_ascii, generate_character_animations,
              generate_character_with_socket, inspect_weapon_ascii, equip_and_export_weapon,
              run_custom_lua, export_game_assets):
    mcp.tool()(_tool)

if __name__ == "__main__":
    mcp.run()
