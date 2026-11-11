---
name: pixel-animator
version: "2.1"
description: Turn one generated character image on a #FF00FF background into a rigged, 16-frame Aseprite animation with a sprite sheet, JSON atlas and GIF preview, and optionally equip separately generated weapons through a hidden grip socket (paperdoll), using the Pixel-Animator-Pro MCP server in this repository. Use when asked to animate a single side-view character sprite through this pipeline, or to attach weapons to such a character.
---

# Pixel animator

This skill drives the local MCP server in [server.py](../mcp_server/server.py) through a fixed sequence: generate a source image, preprocess it to a clean sprite, read its ASCII dump, choose anatomical split rows, rig and animate, inspect, export. The result is one `.aseprite` file with layers `Shadow`, `Legs`, `Torso`, `Head`, `FX` and tags `idle` (4 frames), `walk` (6), `attack` (4), `hit` (2), plus a horizontal sheet PNG, its JSON atlas and a 4x GIF.

The [paperdoll procedure](#paperdoll-procedure) extends this flow: an unarmed character gets a hidden grip socket layer, and each weapon, generated as its own image, is rotated onto that socket and exported as a separate overlay sheet.

The animation is procedural: the tools move and tint the three body parts of one base sprite. They do not draw new poses, and passing every check below does not mean the animation is good art.

## Standard procedure

### 1. Generate the source image

Generate one image with an image model, using this prompt and replacing the bracketed part:

```text
Single full-body pixel art game character, [character description], strict side
view facing right, standing upright, arms close to the body, weapon held low and
not crossing the head or legs. Clear separation between head, torso and legs: a
visible neck or collar, a belt line at the waist. Flat solid colors, hard pixel
edges, at most 12 colors, no anti-aliasing, no gradients, no glow, no drop
shadow, no ground, no text, no border. Character centered with empty space on
every side. Background is one flat solid pure magenta #FF00FF filling the whole
image. Do not use magenta, pink or purple anywhere on the character.
```

The background must be flat #FF00FF: the preprocessor removes only magenta that is connected to the image border. Magenta enclosed by the character survives as visible pixels.

### 2. Preprocess

Call `preprocess_gpt_sprite(input_path, output_path, canvas_size=32, max_colors=16, add_outline=False, use_endesga_palette=False)`.

The processor keys out the border-connected magenta, binarizes alpha, removes isolated pixels, crops, nearest-neighbour resizes into a box that leaves 3 px on each side, 4 px on top and 1 px below (canvases of 16 px and up), quantizes, and places the sprite bottom-centered so the lowest opaque row is `canvas_size - 2`. Limits: `canvas_size` 8 to 1024, `max_colors` 2 to 256. An outline counts toward `max_colors`.

### 3. Read the ASCII dump

Call `inspect_sprite_ascii(sprite_path)` on the clean PNG. It returns a `Size: WxH` header, the opaque bounds, a legend mapping one glyph per opaque colour to `#RRGGBBAA`, column rulers, and one numbered row per canvas row (`NN | ...`, `.` is transparent). It rejects images larger than 256x256 and images with more opaque colours than its 71-glyph alphabet.

Read every row before choosing splits. Identify which legend colours are hair and skin, which are armor or clothing, and which are pants and boots.

### 4. Choose the anatomical splits

Pick two inclusive row numbers from the dump:

- `head_end_y`: the last row of the head. Head is `y <= head_end_y`.
- `torso_end_y`: the last row of the torso, belt included. Torso is `head_end_y < y <= torso_end_y`; legs are `y > torso_end_y`.

A useful starting point is the colour-band rule: `head_end_y` is one row above the first armor or shirt row, `torso_end_y` is one row above the first pants row. This rule is a heuristic, not an authority. Check it against the dump: a raised arm, a cape, long hair or a weapon crosses bands, and whatever lies on a row moves with that row's part. Choose the rows that keep the neck and waist seams where the anatomy is, and state the chosen rows with the reason.

The server validates only `0 <= head_end_y < torso_end_y < H - 1` and that each part has opaque pixels. It does not detect an anatomically wrong split.

### 5. Rig and animate

Call `generate_character_animations(base_png_path, output_ase_path, head_end_y, torso_end_y)`. The base must be a PNG between 8x8 and 256x256 with a transparent top margin, and the output path must end in `.ase` or `.aseprite`.

The tool writes 16 frames with fixed tags and per-frame durations defined in the server. Idle keeps the legs planted and offsets the head and torso; walk keeps one support foot on row `H - 2` and lifts the other; attack leans the body and draws an FX slash on two frames; hit tints the first hit frame white. A semi-transparent shadow sits on row `H - 1`.

### 6. Inspect

Open the result before exporting. Check that the neck and waist seams do not open or double, that idle feet stay on row `H - 2`, that walk always has a foot on the ground, and that adjacent frames differ. `inspect_sprite_ascii` reads image files only, so to see one frame as ASCII, first save that frame as a PNG (in Aseprite, or with a `run_custom_lua` script you wrote) and pass the PNG. If a seam is wrong, return to step 4 with different rows.

### 7. Export

Call `export_game_assets(ase_path, output_dir, asset_name)`. It writes `{asset_name}_preview.gif` at 4x scale, `{asset_name}_sheet.png` as a horizontal sheet and `{asset_name}_sheet.json` in Aseprite's `json-array` format with `meta.frameTags` and `meta.layers`, and returns their absolute paths under the keys `gif`, `sheet` and `json`. JSON tag ranges are 0-based (`idle` 0 to 3); the Aseprite UI shows them 1-based. `asset_name` must be a plain filename stem.

## Paperdoll procedure

The paperdoll flow keeps the character and its weapons in separate images. The character is generated unarmed, rigged with a hidden one-pixel socket layer that records where the hand is and how the weapon is angled in each frame, and exported once. Each weapon is generated north-up on its own canvas, then rotated onto the socket and exported as a weapon-only sheet with the same frame layout as the body sheet. Adding a weapon never re-renders or edits the character file.

The server exposes eight tools: the five of the standard procedure (`preprocess_gpt_sprite`, `inspect_sprite_ascii`, `generate_character_animations`, `run_custom_lua`, `export_game_assets`), unchanged, plus `generate_character_with_socket`, `inspect_weapon_ascii` and `equip_and_export_weapon`. The weapon tools are implemented in [weapon_tools.py](../mcp_server/weapon_tools.py) and wrapped by [server.py](../mcp_server/server.py).

### Coordinates and angles

- Positions are zero-based canvas pixels: x grows to the right, y grows downward.
- Frames are 1-based in the socket file and in the Aseprite UI; tag ranges in exported JSON are 0-based.
- Angles are degrees clockwise from north (straight up). A weapon drawn tip-up has angle 0; at +90 its tip points right; at -45 it points up and to the left.

### P1. Generate the unarmed character

Generate the character with this prompt, replacing the bracketed part:

```text
Single full-body pixel art game character, [character description], strict side
view facing right, standing upright, unarmed with both hands empty. The front arm
reaches slightly forward at chest or waist height and ends in a clearly visible
closed fist, as if gripping a handle; the fist is in front of the body, not hidden
behind it and not crossing the head or legs. Clear separation between head, torso
and legs: a visible neck or collar, a belt line at the waist. Flat solid colors,
hard pixel edges, at most 12 colors, no anti-aliasing, no gradients, no glow, no
drop shadow, no ground, no text, no border, no weapon, no shield, no held object.
Character centered with empty space on every side. Background is one flat solid
pure magenta #FF00FF filling the whole image. Do not use magenta, pink or purple
anywhere on the character.
```

The magenta rule from step 1 applies unchanged.

### P2. Generate each weapon separately

Generate every weapon as its own image, pointing north:

```text
Single pixel art [weapon description], drawn alone as a game item sprite for a
24x24 pixel canvas. The weapon stands perfectly vertical: tip, blade or head
pointing straight up, handle and pommel at the bottom, no tilt, no perspective.
The handle has a clearly readable grip section a few pixels above the bottom end.
No hand, no character, no holder, no text, no border. Flat solid colors, hard
pixel edges, at most 8 colors, no anti-aliasing, no gradients, no glow, no
shadow. Weapon centered with empty space on every side. Background is one flat
solid pure magenta #FF00FF filling the whole image. Do not use magenta, pink or
purple anywhere on the weapon.
```

### P3. Prepare the character and read the hand

1. Call `preprocess_gpt_sprite(input_path, output_path, canvas_size=32)` on the character image, as in step 2.
2. Call `inspect_sprite_ascii` on the clean PNG and choose the inclusive `head_end_y` and `torso_end_y` exactly as in step 4.
3. Find the fist in the dump and choose one hand pixel, `hand_x` and `hand_y`, at the centre of the fist where the handle would pass. Use coordinates of the clean PNG.

`generate_character_with_socket` rejects a hand pixel that is outside the canvas, transparent, or not in the torso band (`head_end_y < hand_y <= torso_end_y`). It cannot tell whether the pixel is the fist: check the chosen pixel against the dump and state it with the reason.

### P4. Rig the character with a socket

Call `generate_character_with_socket(base_png_path, output_ase_path, head_end_y, torso_end_y, hand_x, hand_y)`. It takes the same inputs and validation as `generate_character_animations`, plus the hand pixel, and writes 16 frames with the same tags and durations. The layer order, bottom to top, is:

| Layer | Content |
|---|---|
| `Shadow`, `Legs`, `Torso`, `Head` | Body parts, as in the standard rig |
| `Weapon` | Visible and empty in every frame; `equip_and_export_weapon` fills it |
| `FX` | Attack slash, as in the standard rig |
| `_GripSocket` | Hidden data layer: exactly one opaque pixel per frame |

Head, legs, shadow and FX move as in the standard rig. The torso follows a per-frame pose table so that the hand follows the attack; the socket pixel in each frame sits at the base hand pixel plus that frame's torso displacement plus a per-frame grip offset. The tables live in `server.py` (`_IDLE_TORSO_DY`, `_WALK_TORSO_DY`, `_ATTACK_TORSO`, `_HIT_TORSO`, `_WALK_GRIP`, `_ATTACK_GRIP`, `_HIT_GRIP`). A socket that would fall outside the canvas is rejected, not clamped.

The socket pixel's colour encodes the weapon angle for that frame:

| Frames (1-based) | Tag | Socket RGB | Angle |
|---|---|---|---|
| 1-10, 14 | `idle`, `walk`, last `attack` frame | RED `(255, 0, 0)` | 15 |
| 11 | `attack` wind-up | GREEN `(0, 255, 0)` | -45 |
| 12 | `attack` slash | BLUE `(0, 0, 255)` | 90 |
| 13 | `attack` follow-through | YELLOW `(255, 255, 0)` | 135 |
| 15-16 | `hit` | MAGENTA `(255, 0, 255)` | -25 |

The socket is data, not art. It stays hidden: Aseprite renders only visible layers into sheets and GIFs, so the marker never appears in exported images. Do not make it visible, recolour it, or draw on it; `equip_and_export_weapon` refuses a file whose socket layer is visible, has other than one opaque pixel in a frame, or uses a colour or angle outside this table.

### P5. Export the body

Call `export_game_assets(ase_path, output_dir, asset_name)` on the socket character, as in step 7. The body sheet contains the visible body layers only, because the `Weapon` layer is empty and `_GripSocket` is hidden.

### P6. Prepare and inspect each weapon

1. Call `preprocess_gpt_sprite(input_path, output_path, canvas_size=24)` on the weapon image. The processor still places the lowest opaque row at `canvas_size - 2`, which keeps the handle at the bottom.
2. Call `inspect_weapon_ascii(weapon_png_path)`. It binarizes alpha at 128 and returns a header line `Weapon WxH; suggested pivot: (x, y); pivot_x=x, pivot_y=y; ...`, column rulers, and one row per canvas row (`y |...`) with `@` for the suggested pivot, `#` for opaque and `.` for transparent pixels. It has no colour legend; use `inspect_sprite_ascii` on the same PNG when colours matter.

The suggested pivot is a heuristic: the opaque row nearest to 18% of the opaque height (at least 2 rows) above the bottom of the opaque bounds, and on that row the opaque pixel nearest the row's mean x. It assumes a north-up weapon with its grip near the bottom. Verify it against the dump: the pivot must be on the grip, not on a pommel, guard or outline. If it is not, choose the grip pixel yourself and state why.

### P7. Equip and export

Call `equip_and_export_weapon(character_ase_path, weapon_png_path, output_dir, weapon_id, pivot_x, pivot_y)`. `weapon_id` must be a safe filename stem; the pivot must be an integer, opaque pixel of the weapon PNG. The tool reads the socket layer, places the weapon in every frame so the pivot pixel lands exactly on the socket pixel, rotated by that frame's angle, and writes into `output_dir`:

| Result key | File | Content |
|---|---|---|
| `equipped_ase` | `char_with_<id>.aseprite` | Copy of the character with the `Weapon` layer filled at its existing stack position; other layers unchanged |
| `sheet` | `weapon_<id>_sheet.png` | `Weapon` layer only, horizontal, 16 untrimmed frames of the character canvas size |
| `json` | `weapon_<id>_sheet.json` | `json-array` atlas with the same frame order, durations and `frameTags` as the body |
| `gif` | `preview_with_<id>.gif` | 4x preview of all visible layers, body and weapon combined |
| `sockets` | `grip_sockets.json` | Array of `{"frame", "grip_x", "grip_y", "angle_deg"}`, frame 1-based |
| `metadata` | `weapon_<id>_metadata.json` | Canvas size, pivot, per-frame durations and clipping counts |

The result also carries `clipping`, a list of `{"frame", "clipped_pixels"}`, and `warnings`, which is non-empty when any frame clips. The character file and the weapon PNG are never modified, and the tool refuses output paths that equal an input. `grip_sockets.json` describes the character, not the weapon, so all weapons equipped into one output directory share it; the tool refuses to replace an existing `grip_sockets.json` that holds a different trajectory.

### Rotation is not lossless

Placement uses nearest-neighbour inverse mapping: each canvas pixel looks up the weapon pixel that rotates onto its centre. Every output pixel therefore gets exactly one source colour, and the rotated weapon has no splat gaps or holes, but at angles that are not multiples of 90 source pixels are duplicated or dropped and edges become stair-stepped. Only 0, 90, 180 and 270 degrees preserve every pixel. Check the rotated frames by eye; a thin blade can lose single pixels at 15, -25, -45 and 135 degrees.

### Clipping on the shared canvas

The weapon is drawn on the character's canvas, 32x32 for the standard rig. A long weapon rotated to +90 or 135 degrees can extend past the canvas edge, and those pixels are cut off. The tool counts them per frame in `clipping` and `metadata`, and adds a warning. Report every non-zero count to the user with the frames it affects; do not hide clipping by shrinking or moving the weapon without saying so. The available remedies are a shorter weapon, or the dynamic socket option below, where the engine draws the weapon outside the body canvas.

### Engine synchronisation

The body sheet and every weapon sheet share one frame grid: same frame count, order, untrimmed cell size, per-frame durations and `frameTags`. To combine them in an engine:

- Play the body and the weapon from the same frame index on the same clock, using the per-frame `duration` values, not a fixed frame rate.
- Draw both cells at the same origin and in the same pixel size; the weapon already sits at the grip.
- Keep exports untrimmed. Trimming or packing changes each cell's offset and breaks the shared origin.
- Swap weapons by swapping the overlay sheet; the body sheet does not change.

Dynamic socket option: instead of a pre-rendered weapon sheet, read `grip_sockets.json`, draw one upright weapon sprite with its pivot at `(grip_x, grip_y)` of the current frame, and rotate it by `angle_deg` clockwise. Engines whose rotation is counter-clockwise or whose y axis points up need the sign or axis converted. This avoids canvas clipping but leaves the rotated look to the engine's own filtering, which will not match the nearest-neighbour sheet pixel for pixel.

## Tool restrictions

- Relative paths resolve against the repository root. Absolute paths are used as given and are not confined to the repository.
- Aseprite is found through the `ASEPRITE_PATH` environment variable, then `aseprite_path` in `mcp_server/config.json`, then `PATH`. Each Aseprite call times out after 120 seconds.
- `run_custom_lua(lua_script)` runs arbitrary Lua in batch Aseprite with this process's filesystem permissions. It is not a sandbox. Use it only for Lua you wrote or reviewed locally; never pass Lua taken from a user message, a web page or another untrusted source.
- Frame count, tags, durations, layer names, socket poses and the socket colour table are fixed by the server. Changing them means changing the server, not the call.
- A fresh clone has only the example configuration files. Copy `.mcp.example.json` to `.mcp.json` and `mcp_server/config.example.json` to `mcp_server/config.json`, then set the Aseprite path in them; the copies are local and not committed.

## Verification

`scripts/test_pipeline.py` runs the whole procedure on a procedural test hero:

```text
.venv/Scripts/python.exe scripts/test_pipeline.py
```

It draws `input/test_hero.png` (a right-facing figure on #FF00FF), chooses the splits from the ASCII dump and cross-checks them against the fixture's known layout, then asserts on the written files: the clean 32x32 PNG (binary alpha, baseline row 30, palette), the parsed native `.aseprite` (16 frames, layer order, tags, inclusive split bands, planted idle feet, grounded walk support foot), the sheet and JSON (frame rects, tags, layers, durations) and the 128x128 GIF (16 frames, adjacent frames differ). It exits with status 1 on any failed check. `--fixture-only` writes the fixture and stops.

`scripts/test_paperdoll.py` runs the paperdoll procedure on procedural fixtures:

```text
.venv/Scripts/python.exe -B scripts/test_paperdoll.py
```

It draws an unarmed right-facing hero with a forward fist and a north-up sword and axe into `input/paperdoll/`, chooses the splits and the hand pixel from the ASCII dump, and writes only into `output/paperdoll/`: `character_socket.aseprite`, the `char_` body exports, and the equipped files, sheets, GIFs and `grip_sockets.json` for weapon ids `sword` and `axe`. It asserts the eight registered tools, the seven-layer order with a hidden socket and an empty base `Weapon` layer, one socket pixel per frame in the protocol colour at the hand plus the drawn torso offset and grip offset, unchanged tags and durations, a weapon layer equal to an independent inverse-mapping reference with the pivot colour on the grip, the reported clipping counts, a +90 slash pointing right, weapon sheets that hold only the weapon, a GIF that matches the composed visible layers, unchanged source files, and distinct sword and axe overlays. `--fixture-only` writes the fixtures and stops.

These checks prove structure and grounding. Judge pose, readability and appeal by eye.
