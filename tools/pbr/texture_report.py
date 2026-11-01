#!/usr/bin/env python3
"""Join the port's texture manifest with an HD texture pack and say what each texture is for.

Step 1 of docs/research/PBR_TEXTURES.md. tools/pbr/disc_texture_scan.py writes the manifest for
every material on the disc (no play needed); the game, run with MP2_TEXTURE_MANIFEST=1, writes the
same records for what it draws. One record per material and texture slot, with the texture's pack
file name and the material's flags, blend, TEV stages and texgens
(platform/include/port_texture_manifest.h).
This reads that, finds each texture in the pack, classifies its role, and writes a CSV:

    python tools/pbr/texture_report.py texture_manifest.jsonl "<Dolphin>/Load/Textures/G2M"
    (Windows: py -3 tools\\pbr\\texture_report.py texture_manifest.jsonl "C:\\...\\G2M")

Roles, from how the material's TEV stages use the texture's texmap (GX: out = d + lerp(a, b, c)):
  lightmap    the first textured stage of a material with the lightmap flag
  reflection  its texture coordinates come from the normal or the position (an environment or
              projected map), not from the model's UVs
  glow        added onto what earlier stages made (d is the previous colour or a register)
  diffuse     multiplied into the colour (lit, tinted or alone): the PBR candidates
  alpha       only its alpha is read (a mask)
  indirect    the material's indirect (warp) texture
  unused      bound but read by no stage
These are heuristics over the raw facts the manifest keeps, so they can change without rebuilding
the game. A texture used several ways lists every role. Python 3.8+, standard library only.
"""

import argparse
import collections
import csv
import json
import pathlib
import re
import sys

# GX enums (extern/aurora/include/dolphin/gx/GXEnum.h).
CC_CPREV, CC_C0, CC_C1, CC_C2 = 0, 2, 4, 6
CC_TEXC, CC_TEXA, CC_RASC, CC_ZERO = 8, 9, 10, 15
CA_TEXA = 4
TEV_ADD = 0
TG_POS, TG_NRM = 0, 1
TEXMAP_NULL = 0xFF
BL_ONE = 1

# CCubeMaterial's state flags (include/Kyoto/Graphics/CCubeMaterial.hpp).
FLAG_ALPHA_TEST = 1 << 5
FLAG_INDIRECT = 1 << 10
FLAG_LIGHTMAP = 1 << 11

# Pack folders whose textures are never surfaces (matched case-insensitively against any folder
# between the pack root and the file).
NON_SURFACE_FOLDERS = ("hud", "fx", "font", "fonts", "text", "ui", "menu", "menus", "effects",
                       "particles")

NAME_RE = re.compile(
    r"^tex1_(\d+)x(\d+)(_m)?_([0-9a-f]{16}|\$)(?:_([0-9a-f]{16}|\$))?_(\d+)(?:_(mip\d+|arb))?\.(dds|png)$",
    re.IGNORECASE)


def parse_name(name):
    """(w, h, texhash, tluthash or None, fmt) for a Dolphin-style replacement name, or None. A
    mip-level file (_mipN) parses to None: it belongs to its base file."""
    m = NAME_RE.match(name)
    if not m or (m.group(7) or "").lower().startswith("mip"):
        return None
    w, h, _mips, tex, tlut, fmt = m.group(1, 2, 3, 4, 5, 6)
    return (int(w), int(h), tex.lower(), tlut.lower() if tlut else None, int(fmt))


class Stage:
    def __init__(self, raw):
        order, color_in, alpha_in, color_op = raw[0], raw[1], raw[2], raw[3]
        self.texcoord = order & 0xFF
        self.texmap = (order >> 8) & 0xFF
        self.color = [(color_in >> s) & 0x1F for s in (0, 5, 10, 15)]  # a, b, c, d
        self.alpha = [(alpha_in >> s) & 0x1F for s in (0, 5, 10, 15)]
        self.color_op = color_op & 0xF

    def reads_color(self):
        return CC_TEXC in self.color or CC_TEXA in self.color

    def reads_alpha(self):
        return CA_TEXA in self.alpha


def classify(record):
    """The role of record["slot"] in its material (see the module docstring)."""
    slot = record["slot"]
    flags = record["flags"]
    stages = [Stage(s) for s in record["stages"]]
    using = [(i, s) for i, s in enumerate(stages) if s.texmap == slot]
    color_uses = [(i, s) for i, s in using if s.reads_color()]
    alpha_uses = [(i, s) for i, s in using if s.reads_alpha()]
    if not color_uses and not alpha_uses:
        return "indirect" if flags & FLAG_INDIRECT else "unused"
    texgens = record.get("texgens", [])
    texcoord = using[0][1].texcoord
    if texcoord < len(texgens) and ((texgens[texcoord] >> 4) & 0x1F) in (TG_POS, TG_NRM):
        return "reflection"
    if not color_uses:
        return "alpha"
    first_textured = next((i for i, s in enumerate(stages)
                           if s.texmap != TEXMAP_NULL and (s.reads_color() or s.reads_alpha())), None)
    index, stage = color_uses[0]
    if flags & FLAG_LIGHTMAP and index == first_textured:
        return "lightmap"
    a, b, c, d = stage.color
    lerp = (a, b, c)
    if stage.color_op == TEV_ADD:
        if d in (CC_TEXC, CC_TEXA) and any(x not in (CC_ZERO,) for x in lerp) and index > 0:
            return "glow"
        if (CC_TEXC in lerp or CC_TEXA in lerp) and d in (CC_CPREV, CC_C0, CC_C1, CC_C2, CC_RASC) \
                and index > 0:
            return "glow"
    if CC_TEXC in stage.color:
        return "diffuse"
    return "alpha"


def folder_category(rel_path):
    """The first folder under the pack root ("" when the file sits at the root)."""
    return rel_path.parts[0] if len(rel_path.parts) > 1 else ""


def is_surface_folder(rel_path):
    return not any(part.lower() in NON_SURFACE_FOLDERS for part in rel_path.parts[:-1])


def index_pack(pack_root):
    """{key: [relative paths]} for every replacement in the pack, and how many .dds/.png files
    aren't replacement names (extra mip levels, _mipN, belong to their base file and don't count)."""
    index = collections.defaultdict(list)
    skipped = 0
    for path in sorted(pack_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".dds", ".png"):
            continue
        key = parse_name(path.name)
        if key is None:
            m = NAME_RE.match(path.name)
            if not (m and (m.group(7) or "").lower().startswith("mip")):
                skipped += 1
            continue
        index[key].append(path.relative_to(pack_root))
    return index, skipped


def find_in_pack(index, key):
    """(the pack's key, its files) for `key`: exact, else a wildcard ("$") TLUT entry for a
    paletted texture; (None, []) when the pack doesn't replace it."""
    if key in index:
        return key, index[key]
    if key[3] is not None:
        wildcard = key[:3] + ("$", key[4])
        if wildcard in index:
            return wildcard, index[wildcard]
    return None, []


def read_manifest(path):
    records, bad = [], 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1  # a line cut short by a crash or a kill
    return records, bad


def build_report(records, index):
    """One row per texture: its roles, uses, sources and pack file."""
    textures = {}
    for record in records:
        key = parse_name(record["name"])
        if key is None:
            continue
        entry = textures.setdefault(record["name"], {
            "key": key, "roles": collections.Counter(), "sources": set(), "uses": 0,
            "additive": False, "alpha_test": False})
        entry["roles"][classify(record)] += 1
        entry["sources"].add(record.get("src", ""))
        entry["uses"] += 1
        entry["additive"] |= (record.get("blend", 0) >> 16) == BL_ONE
        entry["alpha_test"] |= bool(record.get("flags", 0) & FLAG_ALPHA_TEST)
    rows = []
    for name, e in sorted(textures.items()):
        pack_key, files = find_in_pack(index, e["key"])
        pack_file = files[0] if files else None
        roles = [role for role, _ in e["roles"].most_common()]
        surface = pack_file is None or is_surface_folder(pack_file)
        candidate = roles == ["diffuse"] and not e["additive"] and surface and pack_file is not None
        rows.append({
            "pbr_candidate": "yes" if candidate else "no",
            "roles": "+".join(roles),
            "name": name,
            "pack_file": str(pack_file) if pack_file else "",
            "category": folder_category(pack_file) if pack_file else "",
            "width": e["key"][0], "height": e["key"][1], "format": e["key"][4],
            "uses": e["uses"],
            "additive": "yes" if e["additive"] else "",
            "alpha_test": "yes" if e["alpha_test"] else "",
            "sources": "; ".join(sorted(s for s in e["sources"] if s)[:5]),
            "_pack_key": pack_key,
        })
    return rows


FIELDS = ["pbr_candidate", "roles", "name", "pack_file", "category", "width", "height", "format",
          "uses", "additive", "alpha_test", "sources"]


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def summary(rows, index, bad_lines, skipped_files):
    out = []
    seen_keys = {r["_pack_key"] for r in rows if r["_pack_key"] is not None}
    in_pack = [r for r in rows if r["pack_file"]]
    out.append(f"{len(rows)} textures in the manifest, {len(in_pack)} of them in the pack"
               f" ({len(rows) - len(in_pack)} the pack doesn't replace).")
    roles = collections.Counter(r["roles"] for r in rows)
    out.append("Roles: " + ", ".join(f"{role} {n}" for role, n in roles.most_common()))
    out.append(f"PBR candidates (diffuse only, not additive, not in an HUD/FX folder, in the pack): "
               f"{sum(r['pbr_candidate'] == 'yes' for r in rows)}")
    by_category = collections.Counter(r["category"] or "(root)" for r in in_pack)
    if by_category:
        out.append("Seen, by pack folder: " +
                   ", ".join(f"{c} {n}" for c, n in sorted(by_category.items())))
    unseen = collections.Counter(folder_category(paths[0]) or "(root)"
                                 for key, paths in index.items() if key not in seen_keys)
    if unseen:
        out.append("Pack files no material in the manifest uses: " +
                   ", ".join(f"{c} {n}" for c, n in sorted(unseen.items())))
        out.append("  (Particles, HUD frames and fonts draw most FX, HUD and text textures, and neither "
                   "manifest records them; one recorded in game also lacks the rooms not visited.)")
    if bad_lines:
        out.append(f"{bad_lines} manifest line(s) didn't parse and were skipped.")
    if skipped_files:
        out.append(f"{skipped_files} pack file(s) aren't named tex1_... and were skipped.")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", type=pathlib.Path, help="texture_manifest.jsonl from the port")
    parser.add_argument("pack", type=pathlib.Path, help="the pack folder (e.g. .../Load/Textures/G2M)")
    parser.add_argument("-o", "--out", type=pathlib.Path, default=pathlib.Path("texture_report.csv"),
                        help="the CSV to write (default: texture_report.csv)")
    args = parser.parse_args(argv)
    if not args.manifest.is_file():
        parser.error(f"no manifest at {args.manifest}")
    if not args.pack.is_dir():
        parser.error(f"no pack folder at {args.pack}")
    records, bad = read_manifest(args.manifest)
    index, skipped = index_pack(args.pack)
    rows = build_report(records, index)
    write_csv(rows, args.out)
    print(summary(rows, index, bad, skipped))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
