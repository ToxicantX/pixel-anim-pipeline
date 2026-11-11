"""Paperdoll weapon placement; native documents are handled only by Aseprite."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
from PIL import Image

ANGLES = {(255, 0, 0): 15, (0, 255, 0): -45, (0, 0, 255): 90,
          (255, 255, 0): 135, (255, 0, 255): -25}
EXPECTED = [15] * 10 + [-45, 90, 135, 15, -25, -25]


def _weapon(path):
    with Image.open(path) as source:
        if source.format != "PNG":
            raise ValueError("Weapon must be a PNG")
        image = source.convert("RGBA")
    image.putalpha(image.getchannel("A").point(lambda a: 255 if a >= 128 else 0))
    if image.getbbox() is None:
        raise ValueError("Weapon has no opaque pixels (alpha >= 128)")
    return image


def _pivot(image):
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("Weapon has no opaque pixels")
    left, top, right, bottom = bbox
    target_y = bottom - max(2, int((bottom - top) * .18))
    rows = [y for y in range(top, bottom)
            if any(image.getpixel((x, y))[3] for x in range(left, right))]
    y = min(rows, key=lambda row: (abs(row - target_y), row))
    xs = [x for x in range(left, right) if image.getpixel((x, y))[3]]
    mean = sum(xs) / len(xs)
    return min(xs, key=lambda x: (abs(x - mean), x)), y


def inspect_weapon_ascii(weapon_png_path: Path) -> str:
    image = _weapon(weapon_png_path)
    px, py = _pivot(image)
    width = len(str(image.height - 1))
    lines = [f"Weapon {image.width}x{image.height}; suggested pivot: ({px}, {py}); pivot_x={px}, pivot_y={py}; @=pivot #=opaque .=transparent"]
    for digit in range(len(str(image.width - 1)) - 1, -1, -1):
        lines.append(" " * (width + 2) + "".join(str(x // 10**digit % 10) for x in range(image.width)))
    for y in range(image.height):
        lines.append(f"{y:>{width}} |" + "".join("@" if (x, y) == (px, py) else
                     "#" if image.getpixel((x, y))[3] else "." for x in range(image.width)))
    return "\n".join(lines)


def _safe_id(value):
    if (not isinstance(value, str) or not value or len(value) > 80
            or any(ord(c) < 32 or ord(c) == 127 or c in '<>:"/\\|?*' for c in value)
            or value.endswith((".", " ")) or value in (".", "..")
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value)):
        raise ValueError("weapon_id must be a safe non-reserved filename stem")
    return value


def _sockets(data):
    if len(data["frames"]) != 16:
        raise ValueError("Character must have exactly 16 frames")
    result = []
    for i, frame in enumerate(data["frames"], 1):
        pixels = frame["markers"]
        if len(pixels) != 1:
            raise ValueError(f"_GripSocket frame {i}: expected exactly 1 opaque pixel, got {len(pixels)}")
        p = pixels[0]
        color = tuple(p[2:])
        if len(color) != 4 or color[3] != 255 or color[:3] not in ANGLES:
            raise ValueError(f"_GripSocket frame {i}: unknown marker RGBA {color}")
        angle = ANGLES[color[:3]]
        if angle != EXPECTED[i - 1]:
            raise ValueError(f"_GripSocket frame {i}: angle {angle}, expected {EXPECTED[i - 1]}")
        if (type(p[0]) is not int or type(p[1]) is not int or
                not 0 <= p[0] < data["width"] or not 0 <= p[1] < data["height"]):
            raise ValueError(f"_GripSocket frame {i}: coordinates must be integer canvas pixels, got {p[:2]}")
        result.append(dict(frame=i, grip_x=p[0], grip_y=p[1], angle_deg=angle))
    return result


def _place(image, size, pivot, grip, angle):
    c, s = round(math.cos(math.radians(angle)), 15), round(math.sin(math.radians(angle)), 15)
    gx, gy = grip
    px, py = pivot
    # Pillow transforms pixel corners; translate centers, not integer corners.
    matrix = (c, s, px + .5 - c * (gx + .5) - s * (gy + .5),
              -s, c, py + .5 + s * (gx + .5) - c * (gy + .5))
    return image.transform(size, Image.Transform.AFFINE, matrix, Image.Resampling.NEAREST)


def _require(path):
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError(f"Aseprite did not produce fresh output: {path.name}")


def equip_and_export_weapon(character_path: Path, weapon_path: Path, output_dir: Path,
                            weapon_id: str, pivot_x: int, pivot_y: int, *,
                            run_lua, run_aseprite, lua_string) -> dict:
    stem = _safe_id(weapon_id)
    image = _weapon(weapon_path)
    if (type(pivot_x) is not int or type(pivot_y) is not int or
            not 0 <= pivot_x < image.width or not 0 <= pivot_y < image.height or
            not image.getpixel((pivot_x, pivot_y))[3]):
        raise ValueError("Pivot must be integer, in bounds, and opaque (alpha >= 128)")
    q = lambda p: lua_string(str(p))
    output_dir.mkdir(parents=True, exist_ok=True)
    names = dict(equipped_ase=f"char_with_{stem}.aseprite", sheet=f"weapon_{stem}_sheet.png",
                 json=f"weapon_{stem}_sheet.json", gif=f"preview_with_{stem}.gif", sockets="grip_sockets.json")
    targets = {k: (output_dir / v).resolve() for k, v in names.items()}
    if character_path.resolve() in targets.values() or weapon_path.resolve() in targets.values():
        raise ValueError("Output would overwrite an input")
    with tempfile.TemporaryDirectory(prefix="paperdoll_", dir=output_dir) as temp:
        tmp = Path(temp)
        manifest = tmp / "manifest.json"
        run_lua(f'''
local s=assert(app.open({q(character_path)}), "Cannot open character")
assert(s.colorMode == ColorMode.RGB, "Character must be RGBA")
local sockets={{}} local weapons={{}}
local function scan(layers)
 for _,l in ipairs(layers) do
  if l.name == "_GripSocket" then table.insert(sockets,l) end
  if l.name == "Weapon" then table.insert(weapons,l) end
  if l.isGroup then scan(l.layers) end
 end
end
scan(s.layers)
assert(#sockets == 1, "Expected exactly one _GripSocket layer")
assert(#weapons <= 1, "Ambiguous duplicate Weapon layers")
assert(not sockets[1].isGroup, "Socket must be an image layer")
assert(not sockets[1].isVisible, "_GripSocket must be hidden")
local frames={{}}
for i,f in ipairs(s.frames) do
 local markers={{}} local cel=sockets[1]:cel(f)
 if cel then
  for it in cel.image:pixels() do
   local p=it() local a=app.pixelColor.rgbaA(p)
   if a > 0 then table.insert(markers,{{it.x+cel.position.x,it.y+cel.position.y,
    app.pixelColor.rgbaR(p),app.pixelColor.rgbaG(p),app.pixelColor.rgbaB(p),a}}) end
  end
 end
 frames[i]={{duration=math.floor(f.duration*1000+0.5),markers=markers}}
end
local tags={{}}
for _,t in ipairs(s.tags) do table.insert(tags,{{name=t.name,from=t.fromFrame.frameNumber-1,to=t.toFrame.frameNumber-1}}) end
local out=assert(io.open({q(manifest)},"w"))
out:write(json.encode({{width=s.width,height=s.height,frames=frames,tags=tags}})) out:close()
s:close()
''')
        _require(manifest)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        sockets = _sockets(data)
        socket_text = json.dumps(sockets, indent=2) + "\n"
        if targets["sockets"].exists() and json.loads(targets["sockets"].read_text(encoding="utf-8")) != sockets:
            raise ValueError("Existing grip_sockets.json belongs to a different character trajectory")
        size = (data["width"], data["height"])
        clips = []
        radius = math.ceil(math.hypot(image.width, image.height)) + 2
        for rec in sockets:
            frame = _place(image, size, (pivot_x, pivot_y), (rec["grip_x"], rec["grip_y"]), rec["angle_deg"])
            frame.save(tmp / f"pose_{rec['frame']}.png")
            full = _place(image, (radius*2+1, radius*2+1), (pivot_x, pivot_y), (radius, radius), rec["angle_deg"])
            total = full.getchannel("A").histogram()[255]
            visible = frame.getchannel("A").histogram()[255]
            clips.append(dict(frame=rec["frame"], clipped_pixels=max(0, total-visible)))
        equipped = tmp / names["equipped_ase"]
        run_lua(f'''
local s=assert(app.open({q(character_path)}))
local found={{}}
local function scan(ls) for _,l in ipairs(ls) do
 if l.name=="Weapon" then table.insert(found,l) end
 if l.isGroup then scan(l.layers) end
end end
scan(s.layers) assert(#found<=1,"Duplicate Weapon layers")
local parent=s local index=#s.layers+1
if #found==1 then
 assert(not found[1].isGroup,"Weapon must be an image layer")
 parent=found[1].parent index=found[1].stackIndex
 s:deleteLayer(found[1])
else
 for _,l in ipairs(s.layers) do
  if l.name=="FX" or l.name=="_GripSocket" then index=math.min(index,l.stackIndex) end
 end
end
local layer=s:newLayer() layer.name="Weapon" layer.isVisible=true
layer.parent=parent layer.stackIndex=index
for i,f in ipairs(s.frames) do
 local im=Image{{fromFile={q(tmp)} .. "/pose_" .. i .. ".png"}}
 s:newCel(layer,f,im,Point(0,0))
end
s:saveAs({q(equipped)}) s:close()
''')
        _require(equipped)
        sheet, meta, gif = (tmp / names[k] for k in ("sheet", "json", "gif"))
        run_aseprite(["--layer", "Weapon", "--list-tags", str(equipped), "--sheet-type", "horizontal",
                      "--sheet", str(sheet), "--format", "json-array", "--data", str(meta)])
        run_aseprite([str(equipped), "--scale", "4", "--save-as", str(gif)])
        for p in (sheet, meta, gif):
            _require(p)
        exported = json.loads(meta.read_text(encoding="utf-8"))
        frames = exported.get("frames", [])
        if not isinstance(frames, list) or len(frames) != 16:
            raise RuntimeError("Weapon sheet must contain all 16 unmerged frames")
        for i, f in enumerate(frames):
            if (f.get("duration") != data["frames"][i]["duration"] or f.get("trimmed") or
                f.get("frame") != dict(x=i*size[0], y=0, w=size[0], h=size[1])):
                raise RuntimeError(f"Invalid untrimmed weapon sheet frame {i+1}")
        if [(t['name'],t['from'],t['to']) for t in exported.get('meta',{}).get('frameTags',[])] != [(t['name'],t['from'],t['to']) for t in data['tags']]:
            raise RuntimeError("Weapon sheet tags changed")
        with Image.open(sheet) as check:
            if check.size != (size[0]*16, size[1]):
                raise RuntimeError("Invalid weapon sheet dimensions")
            for i in range(16):
                with Image.open(tmp / f"pose_{i+1}.png") as pose:
                    if check.convert("RGBA").crop((i*size[0],0,(i+1)*size[0],size[1])).tobytes() != pose.tobytes():
                        raise RuntimeError("Weapon sheet pixels differ from isolated overlay")
        with Image.open(gif) as check:
            if check.format != "GIF" or check.size != (size[0]*4,size[1]*4):
                raise RuntimeError("Invalid composed GIF")
            check.verify()
        # Exclusive creation prevents concurrent different trajectories from overwriting one another.
        try:
            socket_stage = tmp / 'sockets.json'
            socket_stage.write_text(socket_text, encoding='utf-8')
            os.link(socket_stage, targets['sockets'])
        except FileExistsError:
            if json.loads(targets['sockets'].read_text(encoding='utf-8')) != sockets:
                raise ValueError("Concurrent conflicting grip_sockets.json")
        for key in ('equipped_ase','sheet','json','gif'):
            (tmp / names[key]).replace(targets[key])
        metadata = output_dir / f"weapon_{stem}_metadata.json"
        metadata.write_text(json.dumps(dict(canvas=dict(width=size[0],height=size[1]),
            coordinates="zero-based canvas pixels, x right, y down; frames one-based",
            angle="degrees clockwise from north-up; arbitrary rotation is not lossless",
            pivot=dict(x=pivot_x,y=pivot_y), durations_ms=[f['duration'] for f in data['frames']],
            clipping=clips), indent=2)+"\n", encoding="utf-8")
    return {**{k:str(v) for k,v in targets.items()}, 'metadata':str(metadata.resolve()),
            'clipping':clips, 'warnings':["Fixed canvas clips weapon pixels"] if any(c['clipped_pixels'] for c in clips) else []}
