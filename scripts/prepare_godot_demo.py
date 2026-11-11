#!/usr/bin/env python3
"""Reproducible asset copy script for Godot demo.
Copies required paperdoll assets from input/paperdoll and output/paperdoll to godot/assets/paperdoll.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT / "input" / "paperdoll"
OUTPUT_DIR = ROOT / "output" / "paperdoll"
DEST_DIR = ROOT / "godot" / "assets" / "paperdoll"

ASSETS = [
    # Body sprite sheet + frame descriptors from output
    (OUTPUT_DIR / "char_sheet.png", DEST_DIR / "char_sheet.png"),
    (OUTPUT_DIR / "char_sheet.json", DEST_DIR / "char_sheet.json"),
    (OUTPUT_DIR / "grip_sockets.json", DEST_DIR / "grip_sockets.json"),

    # 2 weapon overlays (png + json) from output
    (OUTPUT_DIR / "weapon_sword_sheet.png", DEST_DIR / "weapon_sword_sheet.png"),
    (OUTPUT_DIR / "weapon_sword_sheet.json", DEST_DIR / "weapon_sword_sheet.json"),
    (OUTPUT_DIR / "weapon_axe_sheet.png", DEST_DIR / "weapon_axe_sheet.png"),
    (OUTPUT_DIR / "weapon_axe_sheet.json", DEST_DIR / "weapon_axe_sheet.json"),

    # 2 weapon metadata JSONs from output
    (OUTPUT_DIR / "weapon_sword_metadata.json", DEST_DIR / "weapon_sword_metadata.json"),
    (OUTPUT_DIR / "weapon_axe_metadata.json", DEST_DIR / "weapon_axe_metadata.json"),

    # 2 clean static weapon PNGs from input
    (INPUT_DIR / "sword_clean.png", DEST_DIR / "sword_clean.png"),
    (INPUT_DIR / "axe_clean.png", DEST_DIR / "axe_clean.png"),
]

def main() -> int:
    missing = [str(src) for src, _ in ASSETS if not src.exists()]
    if missing:
        print("ERROR: Required source assets are missing:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("\nPlease run existing scripts/test_paperdoll.py first to generate paperdoll outputs.", file=sys.stderr)
        return 1

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src, dst in ASSETS:
        shutil.copy2(src, dst)
        print(f"Copied {src.name} -> {dst.relative_to(ROOT)}")
        copied += 1

    print(f"Successfully copied {copied} assets to {DEST_DIR.relative_to(ROOT)}.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
