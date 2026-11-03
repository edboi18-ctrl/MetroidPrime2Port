"""The logic behind tools/mcp/echoes_mcp.py: script templates, the disc, the mods folder, the console.

Standard library only, so tests/mcp_workshop.py runs it without the MCP SDK. Nothing here writes to
the disc image; the only writes are into the mods folder (install_resource / remove_mod_file).

Sources each part follows (read them before changing a layout):
- script object layout: PrimeWorldEditor src/Core/Resource/Factory/CScriptLoader.cpp
  (LoadObjectMP2, LoadStructMP2, ReadProperty), the same layout tools/echoes_disc.py's
  scly_objects reads;
- property names: PWE's templates (retro-script-object-templates, MP2/), where a property ID is
  CRC32(name + type name) with no final XOR (LibCommon CCRC32, NPropertyMap::CalculatePropertyID);
- world layer table: PWE CWorldLoader::LoadPrimeMLVL (after the area list);
- mods folder rules: platform/port_mods.cpp and port_mods_pak.cpp (ParseLooseName, IsDocument);
- console protocol: platform/port_console.cpp and tools/mp2con.py.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import echoes_disc as ed  # noqa: E402


class WorkshopError(Exception):
    """A failure with a message meant for the person (or model) calling the tool."""


# ---------------------------------------------------------------------------- configuration
def _env(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def templates_dir() -> pathlib.Path:
    """PWE's MP2 template folder: ECHOES_TEMPLATES, else a PrimeWorldEditor checkout beside this repo."""
    configured = _env("ECHOES_TEMPLATES")
    candidates = [pathlib.Path(configured)] if configured else []
    for base in (ROOT.parent / "PrimeWorldEditor", ROOT.parent / "primeworldeditor"):
        candidates.append(base / "templates" / "MP2")
    for path in candidates:
        if (path / "Game.xml").is_file():
            return path
        if (path / "MP2" / "Game.xml").is_file():
            return path / "MP2"
    raise WorkshopError(
        "Prime World Editor's script templates were not found. Set ECHOES_TEMPLATES to the "
        "templates\\MP2 folder of a PrimeWorldEditor checkout (run `git submodule update --init "
        "templates` there first), or clone PrimeWorldEditor beside this repository.")


def disc_path() -> Optional[str]:
    return _env("ECHOES_DISC", "MP2_DISC")


def mods_dir() -> pathlib.Path:
    """The port's mods folder, found the way platform/port_mods.cpp Folder() finds it."""
    configured = _env("ECHOES_MODS", "MP2_MODS")
    if configured:
        return pathlib.Path(configured)
    user = _env("MP2_USER_PATH")
    if user:
        return pathlib.Path(user) / "mods"
    exe = _env("ECHOES_PORT_EXE")
    if exe:
        return pathlib.Path(exe).resolve().parent / "user" / "mods"
    raise WorkshopError(
        "The mods folder is not configured. Set ECHOES_MODS to it (on Windows usually the `user\\mods` "
        "folder beside metroid_prime2_port.exe), or set ECHOES_PORT_EXE to the game's executable.")


def console_port() -> int:
    value = _env("ECHOES_CONSOLE_PORT", "MP2_CONSOLE")
    if value and value not in ("0", "1"):
        try:
            return int(value)
        except ValueError:
            pass
    return 4780  # kDefaultPort in platform/port_console.cpp


# ---------------------------------------------------------------------------- templates
# PropEnumToHashableTypeName (PWE src/Core/Resource/Script/Property/IProperty.h): the type text a
# property's ID is hashed with, when it has no archetype. AnimationSet is CAnimationSetProperty's.
HASH_TYPE_NAMES = {
    "Bool": "bool", "Int": "int", "Float": "float", "Choice": "choice", "Enum": "enum",
    "Flags": "Flags", "String": "string", "Vector": "Vector", "Color": "Color", "Asset": "asset",
    "Sound": "sound", "Spline": "spline", "Guid": "guid", "Animation": "animation",
    "Sequence": "sequence", "AnimationSet": "AnimationSet", "Byte": "byte", "Short": "short",
    "Array": "array",
}


def property_id(name: str, type_name: str) -> int:
    """NPropertyMap::CalculatePropertyID: CRC32 of name then type name, LibCommon's (no final XOR)."""
    return zlib.crc32((name + type_name).encode("utf-8")) ^ 0xFFFFFFFF


@dataclass
class Prop:
    id: int
    type: str
    name: str = ""
    name_verified: bool = False   # the name hashes to the ID (or comes from the template itself)
    archetype: Optional[str] = None
    atomic: bool = False
    children: list = field(default_factory=list)
    default: object = None
    values: dict = field(default_factory=dict)      # Choice / Enum: value -> name
    flags: dict = field(default_factory=dict)       # Flags: mask -> name
    type_filter: list = field(default_factory=list)
    item: Optional["Prop"] = None                   # Array item archetype

    def label(self) -> str:
        return self.name or f"Unknown_{self.id:08X}"

    def child(self, pid: int) -> Optional["Prop"]:
        for c in self.children:
            if c.id == pid:
                return c
        return None


def _hex(text: Optional[str]) -> int:
    return int(text, 0) if text else 0


def _parse_default(ptype: str, node: Optional[ET.Element]):
    if node is None:
        return None
    if ptype in ("Vector", "Color"):
        keys = "XYZ" if ptype == "Vector" else "RGBA"
        out = []
        for k in keys:
            sub = node.find(k)
            out.append(float(sub.text) if sub is not None and sub.text else 0.0)
        return out
    text = (node.text or "").strip()
    if ptype == "Bool":
        return text.lower() == "true"
    if ptype == "Float":
        return float(text) if text else 0.0
    if ptype in ("Int", "Short", "Byte", "Choice", "Enum", "Flags", "Sound", "Animation"):
        try:
            return int(text, 0)
        except ValueError:
            return None
    if ptype == "String":
        return text
    return None


class Templates:
    """PWE's Echoes script templates (Game.xml and the files it lists), loaded on first use."""

    def __init__(self, folder: Optional[pathlib.Path] = None):
        self.folder = pathlib.Path(folder) if folder else templates_dir()
        game = ET.parse(self.folder / "Game.xml").getroot()
        self.objects = self._map(game.find("ScriptObjects"), "Path")        # FourCC -> path
        self.archetype_paths = self._map(game.find("PropertyArchetypes"), "Path")
        self.states = self._map(game.find("States"))
        self.messages = self._map(game.find("Messages"))
        self._archetypes: dict = {}
        self._object_cache: dict = {}
        self.names_by_fourcc: dict = {}
        for fourcc, path in self.objects.items():
            if path.startswith("Script/"):
                self.names_by_fourcc[fourcc] = pathlib.PurePosixPath(path).stem
        self._names = self._load_name_map()

    @staticmethod
    def _map(node: Optional[ET.Element], attr: Optional[str] = None) -> dict:
        out = {}
        if node is None:
            return out
        for element in node.findall("Element"):
            key = element.findtext("Key")
            value = element.find("Value")
            if key is None or value is None:
                continue
            out[key] = value.get(attr) if attr else (value.text or "")
        return out

    def _load_name_map(self) -> dict:
        """PropertyMap.xml: ID -> [(type name, name)]. It sits one level above the game folders."""
        path = self.folder.parent / "PropertyMap.xml"
        names: dict = {}
        if not path.is_file():
            return names
        for element in ET.parse(path).getroot().iter("Element"):
            key = element.find("Key")
            value = element.find("Value")
            if key is None or value is None or key.get("ID") is None:
                continue
            names.setdefault(int(key.get("ID"), 0), []).append((key.get("Type", ""), value.get("Name", "")))
        return names

    # -- name resolution
    def _resolve_name(self, prop: Prop, own_name: Optional[str]):
        if own_name:
            prop.name, prop.name_verified = own_name, True
            return
        candidates = [t for t in (prop.archetype, HASH_TYPE_NAMES.get(prop.type)) if t]
        entries = self._names.get(prop.id, [])
        for type_name in candidates:
            for entry_type, name in entries:
                if entry_type == type_name and name:
                    prop.name, prop.name_verified = name, property_id(name, type_name) == prop.id
                    return
        if len(entries) == 1 and entries[0][1]:
            prop.name = entries[0][1]
            prop.name_verified = property_id(prop.name, entries[0][0]) == prop.id

    # -- loading
    def archetype(self, name: str) -> Prop:
        if name not in self._archetypes:
            path = self.archetype_paths.get(name)
            if path is None:
                raise WorkshopError(f"No property archetype named {name!r} in the templates.")
            root = ET.parse(self.folder / path).getroot().find("PropertyArchetype")
            prop = self._build(root, pid=0, type_override=root.get("Type"))
            prop.name, prop.name_verified = name, True
            self._archetypes[name] = prop
        return self._archetypes[name]

    def _copy(self, prop: Prop) -> Prop:
        clone = Prop(**{k: getattr(prop, k) for k in prop.__dataclass_fields__})
        clone.children = [self._copy(c) for c in prop.children]
        clone.values, clone.flags = dict(prop.values), dict(prop.flags)
        return clone

    def _build(self, node: ET.Element, pid: Optional[int] = None, type_override: Optional[str] = None) -> Prop:
        ptype = type_override or node.get("Type", "")
        prop = Prop(id=_hex(node.get("ID")) if pid is None else pid, type=ptype, archetype=node.get("Archetype"))
        if prop.archetype:
            base = self._copy(self.archetype(prop.archetype))
            prop.children, prop.atomic = base.children, base.atomic
            prop.values, prop.flags, prop.default = base.values, base.flags, base.default
        prop.atomic = prop.atomic or (node.findtext("Atomic") or "").strip().lower() == "true"
        overrides = node.find("SubProperties")
        if overrides is not None:
            for sub in overrides.findall("Element"):
                sid = _hex(sub.get("ID"))
                existing = prop.child(sid) if prop.archetype else None
                if existing is not None:
                    # An archetype's child re-listed here overrides its default (and maybe its name).
                    default = _parse_default(existing.type, sub.find("DefaultValue"))
                    if default is not None:
                        existing.default = default
                    if sub.findtext("Name"):
                        existing.name = sub.findtext("Name")
                else:
                    prop.children.append(self._build(sub))
        default = _parse_default(ptype, node.find("DefaultValue"))
        if default is not None:
            prop.default = default
        values = node.find("Values")
        if values is not None:
            prop.values = {_hex(v.get("ID")): v.get("Name", "") for v in values.findall("Element")}
        flags = node.find("Flags")
        if flags is not None:
            prop.flags = {_hex(f.get("Mask")): f.get("Name", "") for f in flags.findall("Element")}
        type_filter = node.find("TypeFilter")
        if type_filter is not None:
            prop.type_filter = [(e.text or "").strip() for e in type_filter.findall("Element")]
        item = node.find("ItemArchetype")
        if item is not None:
            prop.item = self._build(item)
        own_name = node.findtext("Name")
        if pid is None:
            self._resolve_name(prop, own_name)
        elif own_name:
            prop.name = own_name
        return prop

    def resolve_object(self, name_or_fourcc: str) -> tuple:
        """(FourCC, display name) of a script object type given either, case-insensitively."""
        text = name_or_fourcc.strip()
        for fourcc, name in self.names_by_fourcc.items():
            if text.upper() == fourcc or text.lower() == name.lower():
                return fourcc, name
        return None, None

    def object(self, fourcc: str) -> Prop:
        if fourcc not in self._object_cache:
            path = self.objects.get(fourcc)
            if path is None or not path.startswith("Script/"):
                raise WorkshopError(f"No script object template for {fourcc!r}.")
            root = ET.parse(self.folder / path).getroot()
            prop = self._build(root.find("Properties"), pid=0xFFFFFFFF, type_override="Struct")
            prop.name = self.names_by_fourcc[fourcc]
            prop.name_verified = True
            self._object_cache[fourcc] = prop
        return self._object_cache[fourcc]

    def search(self, text: str = "") -> list:
        """[(FourCC, name)] of the script object types whose name or FourCC contains `text`."""
        needle = text.strip().lower()
        out = [(cc, name) for cc, name in self.names_by_fourcc.items()
               if not needle or needle in name.lower() or needle in cc.lower()]
        return sorted(out, key=lambda item: item[1].lower())

    def state_name(self, fourcc: str) -> str:
        return self.states.get(fourcc, fourcc)

    def message_name(self, fourcc: str) -> str:
        return self.messages.get(fourcc, fourcc)


def describe_template(prop: Prop, max_depth: int = 2, depth: int = 0, lines: Optional[list] = None) -> list:
    """Markdown lines describing a property tree, expanding structs down to `max_depth`."""
    lines = [] if lines is None else lines
    for child in prop.children:
        indent = "  " * depth
        kind = child.type + (f": {child.archetype}" if child.archetype else "")
        name = child.label() + ("" if child.name_verified or not child.name else " (name unverified)")
        extra = []
        if child.default is not None and child.type != "Struct":
            extra.append(f"default {child.default!r}")
        if child.type_filter:
            extra.append("asset type " + "/".join(child.type_filter))
        line = f"{indent}- **{name}** ({kind}) `0x{child.id:08X}`" + (f" - {', '.join(extra)}" if extra else "")
        lines.append(line)
        if child.values:
            shown = ", ".join(f"{k}={v}" for k, v in list(child.values.items())[:24])
            more = f", ... ({len(child.values)} in all)" if len(child.values) > 24 else ""
            lines.append(f"{indent}  - values: {shown}{more}")
        if child.flags:
            lines.append(f"{indent}  - flags: " + ", ".join(f"0x{k:X}={v}" for k, v in child.flags.items()))
        if child.type == "Struct" and child.children:
            if depth + 1 < max_depth:
                describe_template(child, max_depth, depth + 1, lines)
            else:
                lines.append(f"{indent}  - ({len(child.children)} fields; ask about "
                             f"`{child.archetype or child.label()}` to expand)")
    return lines


# ---------------------------------------------------------------------------- script data
def _f(value: float) -> float:
    return round(value, 4)


class PropertyDecoder:
    """Turns the property bytes of one script object into named values, following the template.

    Non-atomic structs give each child's size, so an unknown or mistyped property is skipped exactly
    (LoadStructMP2 seeks to the next property the same way)."""

    def __init__(self, templates: Templates):
        self.templates = templates

    def value(self, prop: Prop, buf: bytes, pos: int, size: Optional[int]):
        t = prop.type
        if t == "Struct":
            return self.struct(prop, buf, pos)[0]
        if t == "Bool":
            return buf[pos] != 0
        if t == "Byte":
            return struct.unpack_from(">b", buf, pos)[0]
        if t == "Short":
            return struct.unpack_from(">h", buf, pos)[0]
        if t in ("Int", "Choice", "Enum", "Sound"):
            return struct.unpack_from(">i", buf, pos)[0]
        if t in ("Flags", "Animation", "Asset"):
            return struct.unpack_from(">I", buf, pos)[0]
        if t == "Float":
            return _f(struct.unpack_from(">f", buf, pos)[0])
        if t == "Vector":
            return [_f(v) for v in struct.unpack_from(">3f", buf, pos)]
        if t == "Color":
            return [_f(v) for v in struct.unpack_from(">4f", buf, pos)]
        if t == "String":
            end = buf.index(b"\0", pos)
            return buf[pos:end].decode("latin1")
        if t == "AnimationSet":
            ancs, character, anim = struct.unpack_from(">3I", buf, pos)
            return {"ancs": f"0x{ancs:08X}", "character": character, "anim": anim}
        if t == "Array" and prop.item is not None:
            (count,) = struct.unpack_from(">i", buf, pos)
            items, p = [], pos + 4
            for _ in range(max(0, count)):
                item, p = self.struct(prop.item, buf, p) if prop.item.type == "Struct" else (None, p)
                items.append(item)
            return items
        return f"<{size if size is not None else '?'} bytes>"

    _FIXED = {"Bool": 1, "Byte": 1, "Short": 2, "Int": 4, "Choice": 4, "Enum": 4, "Sound": 4, "Flags": 4,
              "Animation": 4, "Asset": 4, "Float": 4, "Vector": 12, "Color": 16, "AnimationSet": 12}

    def struct(self, prop: Prop, buf: bytes, pos: int):
        """(list of {name, id, type, value, default}, position after the struct)."""
        out = []
        if prop.atomic:
            for child in prop.children:
                out.append(self._entry(child, self.value(child, buf, pos, None)))
                pos += self._FIXED.get(child.type, 0)
            return out, pos
        (count,) = struct.unpack_from(">H", buf, pos)
        pos += 2
        for _ in range(count):
            pid, size = struct.unpack_from(">IH", buf, pos)
            pos += 6
            child = prop.child(pid)
            if child is None:
                out.append({"name": f"Unknown_{pid:08X}", "id": f"0x{pid:08X}", "type": "?",
                            "value": f"<{size} bytes>", "default": None})
            else:
                try:
                    value = self.value(child, buf, pos, size)
                except (struct.error, ValueError, IndexError):
                    value = f"<{size} bytes, unreadable as {child.type}>"
                out.append(self._entry(child, value))
            pos += size
        return out, pos

    def _entry(self, prop: Prop, value):
        entry = {"name": prop.label(), "id": f"0x{prop.id:08X}", "type": prop.type, "value": value,
                 "default": prop.default}
        if prop.type in ("Choice", "Enum") and isinstance(value, int) and value in prop.values:
            entry["value_name"] = prop.values[value]
        if prop.type == "Flags" and isinstance(value, int) and prop.flags:
            entry["value_name"] = [n for m, n in prop.flags.items() if value & m] or None
        if prop.type == "Asset" and isinstance(value, int):
            entry["value"] = "none" if value == 0xFFFFFFFF else f"0x{value:08X}"
        return entry


def is_default(entry: dict) -> bool:
    value, default = entry.get("value"), entry.get("default")
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return all(is_default(e) for e in value)
    if default is None:
        return value in (None, "none", "", 0, False) and entry.get("type") not in ("Float", "Vector", "Color")
    if isinstance(default, list) and isinstance(value, list) and len(default) == len(value):
        return all(abs(a - b) < 1e-4 for a, b in zip(value, default))
    if isinstance(default, float) and isinstance(value, (int, float)):
        return abs(value - default) < 1e-4
    return value == default


def prune_defaults(entries: list) -> list:
    """Only the values that differ from the template defaults, recursively."""
    out = []
    for entry in entries:
        value = entry.get("value")
        if isinstance(value, list) and value and isinstance(value[0], dict):
            kept = prune_defaults(value)
            if kept:
                out.append(dict(entry, value=kept))
        elif not is_default(entry):
            out.append(entry)
    return out


def find_value(entries: list, *path: int):
    """The value at a property ID path (e.g. EditorProperties, Transform) in decoded entries."""
    for entry in entries:
        if int(entry["id"], 16) == path[0]:
            return entry["value"] if len(path) == 1 else find_value(entry["value"], *path[1:])
    return None


@dataclass
class ScriptObject:
    fourcc: str
    editor_id: int
    links: list            # [(state, message, target editor id)]
    properties: bytes      # from the root struct's u16 child count on

    @property
    def layer(self) -> int:
        return self.editor_id >> 26


def parse_layer(section: bytes) -> list:
    """Every object of one SCLY layer section, links kept (LoadObjectMP2's layout)."""
    if section[:4] != b"SCLY":
        raise WorkshopError("not a script layer section")
    (count,) = struct.unpack_from(">I", section, 10)
    pos, out = 14, []
    for _ in range(count):
        fourcc = section[pos:pos + 4].decode("latin1")
        (length,) = struct.unpack_from(">H", section, pos + 4)
        body = section[pos + 6:pos + 6 + length]
        editor_id, link_count = struct.unpack_from(">IH", body, 0)
        links = []
        for i in range(link_count):
            state, message, target = struct.unpack_from(">III", body, 6 + 12 * i)
            links.append((ed.fcc(state), ed.fcc(message), target))
        # Skip the root struct's own ID and size (LoadObjectMP2: Seek(0x6, SEEK_CUR)).
        out.append(ScriptObject(fourcc, editor_id, links, body[6 + 12 * link_count + 6:]))
        pos += 6 + length
    return out


EDITOR_PROPERTIES = 0x255A4580   # EditorProperties: name INAM, transform XFRM, active ACTV
NAME, TRANSFORM, ACTIVE = 0x494E414D, 0x5846524D, 0x41435456


# ---------------------------------------------------------------------------- room collision
# EMaterialTypes (include/Collision/CMaterialList.hpp): the bit of each collision material flag.
MATERIAL_BITS = {
    0: "NoStepLogic", 1: "Stone", 2: "Metal", 3: "Grass", 4: "Ice", 5: "Pillar", 6: "MetalGrating",
    7: "Phazon", 8: "Dirt", 9: "Lava", 10: "LavaStone", 11: "Snow", 12: "MudSlow", 13: "HalfPipe",
    14: "Mud", 15: "Glass", 16: "Shield", 17: "Sand", 18: "Unknown18", 19: "Unknown19",
    20: "ProjectilePassthrough", 21: "CameraPassthrough", 22: "Wood", 23: "Organic",
    24: "FlippedTriangle/NoEdgeCollision", 25: "Unknown25", 26: "SeeThrough", 27: "ScanPassthrough",
    28: "AIPassthrough", 29: "Ceiling", 30: "Wall", 31: "Floor", 48: "AIBlock", 59: "Solid",
}
FLIPPED = 0x01000000  # kFlippedTriangle (src/WorldFormat/CCollisionPrimitiveData.cpp)
COLLISION_SECTION = 20  # MREA header word: CGameArea::GetSectionIndex(3), PWE's mCollisionBlockNum


def mrea_section(mrea: bytes, index: int) -> bytes:
    """Section `index` of a version-0x19 MREA, inflating only the block that holds it (the block
    walk of echoes_disc.mrea_generated)."""
    h = struct.unpack_from(">32I", mrea, 0)
    if h[0] != 0xDEADBEEF or h[1] < 24:
        raise WorkshopError("not a version 0x18+ MREA")
    nsec, nblk = h[16], h[28]
    pos = 0x80
    sizes = struct.unpack_from(">%dI" % nsec, mrea, pos)
    pos += (nsec * 4 + 31) & ~31
    blocks = [struct.unpack_from(">4I", mrea, pos + 16 * i) for i in range(nblk)]
    pos += (nblk * 16 + 31) & ~31
    si = 0
    for buf, dl, cl, sc in blocks:
        rd = ((cl or buf) + 31) & ~31
        if si <= index < si + sc:
            raw = mrea[pos:pos + rd]
            data = ed.inflate_chunks(raw[rd - cl:], dl) if cl else raw[:dl]
            off = sum(sizes[si:index])
            return data[off:off + sizes[index]]
        pos += rd
        si += sc
    raise WorkshopError(f"MREA has no section {index}")


def parse_area_collision(section: bytes) -> dict:
    """An MREA collision section, read as CGameArea::PostConstructArea and
    CAreaOctTree::MakeFromMemory read it (src/MetroidPrime/CGameArea.cpp, src/WorldFormat/CAreaOctTree.cpp)."""
    (size,) = struct.unpack_from(">I", section, 4)  # first byte skipped, aligned to a word: the size
    b = section[8:8 + size]
    r = ed.Rd(b)
    r.u32()
    r.u32()
    bounds = struct.unpack_from(">6f", b, r.p)
    r.skip(24)
    root_type, tree_size = r.u32(), r.u32()
    tree_start = r.p
    leaves, branches, depth = [], 0, 0

    def walk(at, node_type, level):
        nonlocal branches, depth
        depth = max(depth, level)
        if node_type == 2:
            (count,) = struct.unpack_from(">H", b, at + 24)
            leaves.append((struct.unpack_from(">6f", b, at), struct.unpack_from(">%dH" % count, b, at + 26)))
        elif node_type == 1:
            branches += 1
            (types,) = struct.unpack_from(">H", b, at)
            offsets = struct.unpack_from(">8I", b, at + 4)
            for i in range(8):
                child = types >> (2 * i) & 3
                if child:
                    walk(at + 36 + offsets[i], child, level + 1)

    walk(tree_start, root_type, 0)
    r.p = tree_start + tree_size
    material_count = r.u32()
    materials = [struct.unpack_from(">Q", b, r.p + 8 * i)[0] for i in range(material_count)]
    r.skip(8 * material_count)
    arrays = []
    for _ in range(3):
        n = r.u32()
        arrays.append(b[r.p:r.p + n])
        r.skip(n)
    edge_count = r.u32()
    edges = [struct.unpack_from(">HH", b, r.p + 4 * i) for i in range(edge_count)]
    r.skip(4 * edge_count)
    index_count = r.u32() // 3 * 3
    surface = struct.unpack_from(">%dH" % index_count, b, r.p)
    r.skip(2 * index_count)
    r.u32()
    neighbors = struct.unpack_from(">%dH" % index_count, b, r.p)
    r.skip(2 * index_count)
    vertex_count = r.u32()
    vertices = [struct.unpack_from(">3f", b, r.p + 12 * i) for i in range(vertex_count)]
    r.skip(12 * vertex_count)
    return {"bounds": bounds, "leaves": leaves, "branches": branches, "depth": depth, "tree_bytes": tree_size,
            "materials": materials, "vertex_materials": arrays[0], "edge_materials": arrays[1],
            "triangle_materials": arrays[2], "edges": edges, "surface": surface, "neighbors": neighbors,
            "vertices": vertices, "leftover": len(b) - r.p}


def material_names(flags: int) -> list:
    return [MATERIAL_BITS.get(bit, f"bit{bit}") for bit in range(64) if flags >> bit & 1]


def collision_report(c: dict) -> dict:
    """What a room's collision holds, and whether it follows the rules tools/mcp and PWE's
    CCollisionCooker write by (docs/MCP_WORKSHOP.md)."""
    tris = len(c["surface"]) // 3
    mats, edges, surface = c["materials"], c["edges"], c["surface"]

    def corners(t):  # CCollisionPrimitiveData::GetTriangle
        e0, e1 = edges[surface[3 * t]], edges[surface[3 * t + 1]]
        flipped = mats[c["triangle_materials"][t]] & FLIPPED
        return (e0[1], e0[0], e1[0]) if flipped else (e0[0], e0[1], e1[1])

    slot_order = neighbor_ok = neighbor_set = flipped = 0
    for t in range(tris):
        v = corners(t)
        flipped += bool(mats[c["triangle_materials"][t]] & FLIPPED)
        if all({*edges[surface[3 * t + i]]} == {v[i], v[(i + 1) % 3]} for i in range(3)):
            slot_order += 1
        for i in range(3):
            n = c["neighbors"][3 * t + i]
            if n != 0xFFFF:
                neighbor_set += 1
                neighbor_ok += n < tris and surface[3 * t + i] in surface[3 * n:3 * n + 3]
    reached = {t for _, ts in c["leaves"] for t in ts}
    per_leaf = [len(ts) for _, ts in c["leaves"]] or [0]
    uses = {}
    for m in c["triangle_materials"]:
        uses[m] = uses.get(m, 0) + 1
    return {
        "triangles": tris, "vertices": len(c["vertices"]), "edges": len(edges),
        "materials": [{"index": i, "flags": f"0x{f:016X}", "names": material_names(f), "triangles": uses.get(i, 0)}
                      for i, f in enumerate(mats)],
        "flipped_triangles": flipped,
        "any_material_has_solid_bit": any(f >> 59 & 1 for f in mats),
        "edge_or_vertex_material_with_bit24": sum(bool(mats[m] & FLIPPED) for m in c["edge_materials"] + c["vertex_materials"]),
        "edge_slots_follow_corners": f"{slot_order}/{tris}",
        "neighbours_share_their_edge": f"{neighbor_ok}/{neighbor_set} (and {3 * tris - neighbor_set} with none)",
        "octree": {"leaves": len(c["leaves"]), "branches": c["branches"], "depth": c["depth"],
                   "triangles_per_leaf_max": max(per_leaf), "triangles_per_leaf_mean": round(sum(per_leaf) / len(per_leaf), 1),
                   "tree_bytes": c["tree_bytes"], "triangles_reached": f"{len(reached)}/{tris}"},
        "bytes_left_over": c["leftover"],
    }


# ---------------------------------------------------------------------------- room geometry
def mrea_sections(mrea: bytes) -> list:
    """Every section of a version-0x19 MREA, in order (stored and compressed blocks)."""
    h = struct.unpack_from(">32I", mrea, 0)
    if h[0] != 0xDEADBEEF or h[1] < 24:
        raise WorkshopError("not a version 0x18+ MREA")
    nsec, nblk = h[16], h[28]
    pos = 0x80
    sizes = struct.unpack_from(">%dI" % nsec, mrea, pos)
    pos += (nsec * 4 + 31) & ~31
    blocks = [struct.unpack_from(">4I", mrea, pos + 16 * i) for i in range(nblk)]
    pos += (nblk * 16 + 31) & ~31
    out, si = [], 0
    for buf, dl, cl, sc in blocks:
        rd = ((cl or buf) + 31) & ~31
        raw = mrea[pos:pos + rd]
        data = ed.inflate_chunks(raw[rd - cl:], dl) if cl else raw[:dl]
        off = 0
        for i in range(si, si + sc):
            out.append(data[off:off + sizes[i]])
            off += sizes[i]
        pos += rd
        si += sc
    return out


def _counter(values) -> dict:
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:8])


def geometry_report(mrea: bytes) -> dict:
    """A room's world geometry walked as CGameArea::PostConstructArea and FillInStaticGeometry walk
    it (src/MetroidPrime/CGameArea.cpp), with the facts PrimeWorldEditor's CWorldGeometryCooker
    relies on, so retail rooms can confirm or correct them."""
    h = struct.unpack_from(">32I", mrea, 0)
    secs = mrea_sections(mrea)
    model_count, layer_count = h[14], h[15]
    s = h[17]
    u32 = lambda b, at: struct.unpack_from(">I", b, at)[0]  # noqa: E731

    # Materials (CMetroidModelInstance::GetMaterialByIndex, CCubeMaterial::SetCurrent).
    mats = secs[s]
    tex = u32(mats, 0)
    at = 4 + 4 * tex
    mat_count = u32(mats, at)
    ends = struct.unpack_from(">%dI" % mat_count, mats, at + 4)
    base = at + 4 + 4 * mat_count
    materials, start = [], base
    for e in ends:
        flags, tc = u32(mats, start), u32(mats, start + 4)
        materials.append((flags, u32(mats, start + 8 + 4 * tc)))
        start = base + e
    s += 1

    surfaces, models, formats, prims = [], [], [], []
    record_first, record_area, banks = [], [], []
    group_sizes, consistency = [], [0, 0]
    for m in range(model_count):
        flags = u32(secs[s], 0)
        n = u32(secs[s + 6], 0)
        models.append({"short_normals": bool(flags & 1), "flags": flags, "surfaces": n})
        rec = secs[s + 7 + n]
        grp = secs[s + 8 + n]
        g_count = struct.unpack_from(">H", grp, 0)[0]
        g_table = struct.unpack_from(">%dH" % g_count, grp, 2)
        g_members = {}
        for g in range(g_count):
            lo = 0 if g == 0 else g_table[g - 1]
            hi = g_table[g]
            idx = struct.unpack_from(">%dH" % (hi - lo), grp, 2 + 2 * g_count + 2 * lo)
            group_sizes.append(hi - lo)
            for i in idx:
                g_members.setdefault(i, []).append(g)
        for i in range(n):
            sec = secs[s + 7 + i]
            mat = u32(sec, 0xC)
            dl_size = u32(sec, 0x10) & 0x7FFFFFFF
            extra = u32(sec, 0x1C)
            mb, sb = struct.unpack_from(">hh", sec, 0x2C)
            banks.append((mb, sb))
            first, area = struct.unpack_from(">HH", rec, 2 + 4 * i)
            record_first.append((first == 0, first == i))
            record_area.append((m, i, area))
            surfaces.append({"material": mat, "extra": extra})
            flags_m, desc = materials[mat] if mat < len(materials) else (0, 0)
            # The display list (CCubeModel's walk: primitive & 0xF8, a u16 count).
            p = (0x4F + extra) & ~31
            end = p + dl_size
            stride = sum({3: 2, 2: 1}.get(desc >> (2 * a) & 3, 0) for a in range(11)) + bin(desc >> 24).count("1")
            direct = any(desc >> (2 * a) & 3 == 1 for a in range(11))
            while p < end and p < len(sec) and not direct:
                op = sec[p]
                if op & 0xF8 == 0:
                    break
                count = struct.unpack_from(">H", sec, p + 1)[0]
                prims.append(op & 0xF8)
                packed = bool(flags_m & (1 << 13))
                want = (2 if packed else 1) if flags & 1 else 0
                formats.append("as expected" if op & 7 == want else f"format {op & 7} for {'short' if flags & 1 else 'float'} normals, packed={packed}")
                p += 3 + count * stride
        s += 9 + n

    octree_ok = h[25] != 0xFFFFFFFF
    arot = secs[s]
    area_secs = secs[s + 1]
    ambient = secs[s + 2]
    a_count = u32(area_secs, 0)
    areas = [struct.unpack_from(">6f4h", area_secs, 4 + 32 * k) for k in range(a_count)]
    for m, i, area in record_area:
        if area + 1 < len(areas):
            _, _, _, _, _, _, am, ag, _, _ = areas[area + 1]
            consistency[0] += 1
            consistency[1] += am == m
    nodes = u32(arot, 16)
    words = (u32(arot, 12) + 31) // 32
    table = 64 + 4 * u32(arot, 8) * words
    entries = table + 4 * nodes
    node_flags = [arot[entries + u32(arot, table + 4 * i) + 3] for i in range(nodes)]
    node_x2 = [arot[entries + u32(arot, table + 4 * i) + 2] for i in range(nodes)]
    ids = u32(ambient, 0)
    idx_count = u32(ambient, 4 + 4 * ids)
    after = s + 3
    layer_magic = [secs[after + i][:4] for i in range(layer_count)]
    return {
        "world_models": model_count,
        "short_normal_models": sum(m["short_normals"] for m in models),
        "model_flags": _counter(m["flags"] for m in models),
        "surfaces": len(surfaces),
        "materials": mat_count,
        "materials_with_packed_lightmap_uvs": sum(bool(f & (1 << 13)) for f, _ in materials),
        "materials_with_matrix_index_bits": sum(bool(d >> 24) for _, d in materials),
        "materials_with_clr1_or_tex7": sum(bool(d >> 6 & 3 or d >> 22 & 3) for _, d in materials),
        "materials_with_direct_attributes": sum(any(d >> (2 * a) & 3 == 1 for a in range(11)) for _, d in materials),
        "material_flags": {f"0x{k:X}": v for k, v in _counter(f for f, _ in materials).items()},
        "surface_extra_sizes": _counter(x["extra"] for x in surfaces),
        "surface_matrix_and_shadow_banks": {str(k): v for k, v in _counter(banks).items()},
        "display_list_primitives": {f"0x{k:02X}": v for k, v in _counter(prims).items()},
        "display_list_vertex_formats": _counter(formats),
        "record_first_halfword": {"zero": sum(z for z, _ in record_first),
                                  "equals_surface_index": sum(e for _, e in record_first),
                                  "neither": sum(not z and not e for z, e in record_first)},
        "surfaces_per_group": _counter(group_sizes),
        "record_area_surface_belongs_to_its_model": f"{consistency[1]}/{consistency[0]}",
        "area_surfaces": a_count,
        "area_surface_0": {"model": areas[0][6], "group": areas[0][7]} if areas else None,
        "area_surface_last_two_shorts": {str(k): v for k, v in _counter((a[8], a[9]) for a in areas).items()},
        "render_octree": {"header_index_not_minus_1": octree_ok, "words_0_1": [f"0x{u32(arot, 0):08X}", u32(arot, 4)],
                          "bitmaps": u32(arot, 8), "mesh_count": u32(arot, 12), "nodes": nodes,
                          "node_flags": _counter(node_flags), "node_byte_2": _counter(node_x2)},
        "ambient_light_ids": ids, "ambient_light_indices": idx_count,
        "walk_reaches_script_layers": all(m == b"SCLY" for m in layer_magic) and secs[after + layer_count][:4] == b"SCGN",
    }


# ---------------------------------------------------------------------------- disc
def parse_layer_table(mlvl: bytes, end: int, area_count: int) -> list:
    """Per area, [(layer name, active by default)], from the MLVL past its area list.

    CWorldLoader::LoadPrimeMLVL (Echoes): MapWorld ID, 5 bytes, the area count again, per area a
    u32 layer count and u64 active flags, the layer name count, then each area's layer names."""
    r = ed.Rd(mlvl, end)
    r.u32()
    r.skip(5)
    r.u32()
    counts = []
    for _ in range(area_count):
        n = r.u32()
        flags = struct.unpack_from(">Q", mlvl, r.p)[0]
        r.skip(8)
        counts.append((n, flags))
    r.u32()
    out = []
    for n, flags in counts:
        out.append([(r.cstr(), bool(flags >> i & 1)) for i in range(n)])
    return out


class Disc:
    """Read-only access to the user's disc image through tools/echoes_disc.py."""

    def __init__(self, path: Optional[str] = None):
        path = path or disc_path()
        if not path:
            raise WorkshopError("No disc image is configured. Set ECHOES_DISC to your Echoes .iso, .gcm or "
                                ".ciso (RVZ/WIA/GCZ need converting to ISO in Dolphin first).")
        if not os.path.isfile(path):
            raise WorkshopError(f"The disc image {path!r} does not exist.")
        if path.lower().endswith((".rvz", ".wia", ".gcz", ".wbfs")):
            raise WorkshopError("This reader takes .iso, .gcm or .ciso; convert the image to ISO in Dolphin "
                                "(right-click the game > Convert File...).")
        self.path = path
        self.iso = ed.Iso(path)
        self.paks = ed.load_paks(self.iso)
        self.owner: dict = {}
        self.by_id: dict = {}
        for pak in self.paks:
            for rid, (rtype, *_rest) in pak.res.items():
                self.owner.setdefault((rtype, rid), pak)
                self.by_id.setdefault(rid, set()).add((rtype, pak.path))
        self._worlds: Optional[list] = None
        self._areas: dict = {}

    def close(self):
        """Release the image (Windows will not delete or move a file that is still open)."""
        self.iso.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, rtype: str, rid: int) -> bytes:
        pak = self.owner.get((rtype, rid))
        if pak is None:
            raise WorkshopError(f"{rtype} 0x{rid:08X} is in no PAK on this disc.")
        return pak.get(rid)

    def string(self, strg_id: int) -> Optional[str]:
        try:
            strings = ed.strg_strings(self.get("STRG", strg_id))
        except Exception:
            return None
        for text in strings:
            text = re.sub(r"&[^;&]*;", "", text).strip()
            if text:
                return text.split("\n")[0].strip()
        return None

    def worlds(self) -> list:
        if self._worlds is None:
            worlds = []
            for pak in self.paks:
                for resource_name, (rtype, mlvl_id) in pak.names.items():
                    if rtype != "MLVL":
                        continue
                    try:
                        data = pak.get(mlvl_id)
                        parsed = ed.parse_mlvl(data)
                    except Exception:
                        continue  # multiplayer / demo worlds use other layouts
                    try:
                        layers = parse_layer_table(data, parsed["end"], len(parsed["areas"]))
                    except Exception:
                        layers = [[] for _ in parsed["areas"]]
                    for area, area_layers in zip(parsed["areas"], layers):
                        area["layers"] = area_layers
                    worlds.append({"name": self.string(parsed["strg"]) or resource_name,
                                   "resource_name": resource_name, "mlvl": mlvl_id, "pak": pak.path,
                                   "areas": parsed["areas"]})
            self._worlds = worlds
        return self._worlds

    def world(self, text: str) -> dict:
        needle = text.strip().lower().removeprefix("0x")
        for world in self.worlds():
            if needle in (f"{world['mlvl']:08x}", world["name"].lower(), world["resource_name"].lower()):
                return world
        matches = [w for w in self.worlds() if needle in w["name"].lower() or needle in w["resource_name"].lower()]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(w["name"] for w in self.worlds())
        raise WorkshopError(f"No single world matches {text!r}. Worlds on this disc: {names}.")

    def area_name(self, area: dict) -> str:
        return self.string(area["strg"]) or area["iname"] or f"area {area['index']}"

    def area(self, world: dict, text: str) -> dict:
        needle = text.strip().lower().removeprefix("0x")
        areas = world["areas"]
        if needle.isdigit() and int(needle) < len(areas):
            return areas[int(needle)]
        for area in areas:
            if needle in (f"{area['mrea']:08x}", (area["iname"] or "").lower(), self.area_name(area).lower()):
                return area
        matches = [a for a in areas if needle in (a["iname"] or "").lower() or needle in self.area_name(a).lower()]
        if len(matches) == 1:
            return matches[0]
        raise WorkshopError(f"No single area of {world['name']} matches {text!r} ({len(matches)} partial matches); "
                            "use echoes_disc_list_areas to see them.")

    def collision(self, area: dict) -> dict:
        """The room's collision (parse_area_collision of its MREA's collision section)."""
        mrea = self.get("MREA", area["mrea"])
        return parse_area_collision(mrea_section(mrea, struct.unpack_from(">32I", mrea, 0)[COLLISION_SECTION]))

    def layers(self, area: dict) -> list:
        """[[ScriptObject]] per script layer of an area (decompressing only its script blocks)."""
        key = area["mrea"]
        if key not in self._areas:
            sections = ed.mrea_layers(self.get("MREA", area["mrea"]))
            self._areas[key] = [parse_layer(section) for section in sections]
        return self._areas[key]


# ---------------------------------------------------------------------------- mods folder
DOCUMENT_SUFFIXES = (".txt", ".md", ".json", ".png", ".jpg", ".ini")  # IsDocument in port_mods.cpp
LOOSE_NAME = re.compile(r"^([0-9A-Fa-f]{8})\.([A-Za-z0-9]{4})$")      # ParseLooseName


def parse_loose_name(file_name: str):
    """(TYPE, id) for a `<8 hex digits>.<TYPE>` name, else None."""
    match = LOOSE_NAME.match(file_name)
    return (match.group(2).upper(), int(match.group(1), 16)) if match else None


def _visible_files(folder: pathlib.Path) -> list:
    """Every file under a mod, skipping names that start with '.', in the port's sorted order."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in filenames:
            if not name.startswith("."):
                out.append(pathlib.Path(dirpath) / name)
    return sorted(out)


def scan_mods(folder: pathlib.Path, disc: Optional[Disc] = None) -> list:
    """Each mod folder with what the port will do with each of its files."""
    mods = []
    if not folder.is_dir():
        return mods
    disc_files = {p.lower(): p for p in disc.iso.files} if disc else None
    for mod_dir in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")):
        files = []
        for path in _visible_files(mod_dir):
            relative = path.relative_to(mod_dir).as_posix()
            size = path.stat().st_size
            loose = parse_loose_name(path.name)
            entry = {"file": relative, "size": size}
            if loose:
                rtype, rid = loose
                entry.update(kind="resource", type=rtype, id=f"0x{rid:08X}")
                if size == 0:
                    entry["status"] = "ignored: empty"
                elif disc is not None:
                    paks = sorted(p for t, p in disc.by_id.get(rid, ()) if t == rtype)
                    entry["status"] = (f"replaces it in {len(paks)} PAK(s)" if paks else
                                       "ignored: no PAK holds this type and ID (the port can only replace)")
                    entry["paks"] = paks
                else:
                    entry["status"] = "replaces the resource if a PAK holds it (no disc set to check)"
            elif path.name.lower().endswith(DOCUMENT_SUFFIXES) or path.name.lower() == "license":
                entry.update(kind="document", status="ignored (documentation)")
            elif disc_files is not None:
                if relative.lower() in disc_files:
                    entry.update(kind="disc-file", status=f"replaces disc file {disc_files[relative.lower()]}")
                elif relative.lower().endswith(".thp"):
                    entry.update(kind="disc-file", status="added as a new movie")
                else:
                    entry.update(kind="other", status="ignored: not a disc file and not <ID>.<TYPE>")
            else:
                entry.update(kind="disc-file?", status="replaces the disc file at this path, if there is one")
            files.append(entry)
        mods.append({"mod": mod_dir.name, "files": files})
    return mods


def _check_mod_name(mod: str) -> str:
    mod = mod.strip()
    if not mod or mod.startswith(".") or any(c in mod for c in '/\\:*?"<>|') or mod in (".", ".."):
        raise WorkshopError("A mod name is one folder name: no slashes, no leading '.', none of :*?\"<>|.")
    return mod


def install_resource(source: str, mod: str, asset_id: Optional[str] = None, rtype: Optional[str] = None,
                     disc: Optional[Disc] = None, force: bool = False, folder: Optional[pathlib.Path] = None) -> dict:
    """Copy a cooked resource into <mods>/<mod>/<ID>.<TYPE>, checking the port will use it."""
    src = pathlib.Path(source)
    if not src.is_file():
        raise WorkshopError(f"{source!r} is not a file.")
    if src.stat().st_size == 0:
        raise WorkshopError("The file is empty; the port ignores empty resources.")
    parsed = parse_loose_name(src.name)
    if asset_id is not None or rtype is not None:
        if asset_id is None or rtype is None:
            raise WorkshopError("Give both asset_id and type, or neither (then the file must be named <ID>.<TYPE>).")
        try:
            rid = int(asset_id, 16)
        except ValueError:
            raise WorkshopError(f"asset_id {asset_id!r} is not hexadecimal.") from None
        if not 0 <= rid <= 0xFFFFFFFF or not re.fullmatch(r"[A-Za-z0-9]{4}", rtype):
            raise WorkshopError("asset_id must be 8 hex digits and type four letters or digits (e.g. CMDL).")
        parsed = (rtype.upper(), rid)
    if parsed is None:
        raise WorkshopError("The file is not named <8 hex digits>.<TYPE>; pass asset_id and type.")
    rtype, rid = parsed
    notes = []
    if disc is not None:
        paks = sorted(p for t, p in disc.by_id.get(rid, ()) if t == rtype)
        if not paks:
            other = sorted({t for t, _ in disc.by_id.get(rid, ())})
            why = (f"0x{rid:08X} is a {'/'.join(other)} on the disc, not a {rtype}" if other else
                   f"no PAK holds {rtype} 0x{rid:08X}")
            if not force:
                raise WorkshopError(f"Not installed: {why}. The port only replaces resources a PAK already holds "
                                    "(platform/port_mods.cpp: 'is in no PAK; ignored').")
            notes.append(f"installed anyway (force): {why}, so the port will ignore it")
        else:
            notes.append(f"replaces {rtype} 0x{rid:08X} in {', '.join(paks)}")
    else:
        notes.append("no disc configured, so it was not checked that a PAK holds this resource")
    target_dir = (folder or mods_dir()) / _check_mod_name(mod)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{rid:08X}.{rtype}"
    replaced = target.exists()
    shutil.copyfile(src, target)
    return {"installed": str(target), "replaced_existing_file": replaced, "notes": notes}


def remove_mod_file(mod: str, relative: str, folder: Optional[pathlib.Path] = None) -> str:
    base = ((folder or mods_dir()) / _check_mod_name(mod)).resolve()
    target = (base / relative).resolve()
    if base not in target.parents or not target.is_file():
        raise WorkshopError(f"{relative!r} is not a file inside mod {mod!r}.")
    target.unlink()
    return str(target)


# ---------------------------------------------------------------------------- console and launch
def _replies(data: bytes) -> int:
    return sum(1 for line in data.split(b"\n") if line.startswith((b"=> ok", b"=> err:")))


def console(commands: list, port: Optional[int] = None, timeout: float = 10.0) -> str:
    """Send lines to the running game's console; returns every reply (as tools/mp2con.py does)."""
    lines = [c.strip() for c in commands if c.strip()]
    if not lines:
        raise WorkshopError("No command given.")
    expected = sum(line.count(";") + 1 for line in lines)  # `a; b` answers once per part
    port = port or console_port()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall("".join(line + "\n" for line in lines).encode())
            data = b""
            while _replies(data) < expected:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except ConnectionRefusedError:
        hint = ("The Windows build does not have the console yet (platform/port_console.cpp prints "
                "'MP2_CONSOLE is not supported on Windows'); echoes_game_launch still works there."
                if sys.platform == "win32" else
                "Start the game with MP2_CONSOLE=1 (or use echoes_game_launch, which sets it).")
        raise WorkshopError(f"Nothing is listening on 127.0.0.1:{port}. {hint}") from None
    except (TimeoutError, socket.timeout):
        raise WorkshopError(f"The game did not answer within {timeout:.0f} s. Its console serves one client at a "
                            "time: close any other console client (tools/mp2con.py, console.py).") from None
    return data.decode(errors="replace")


def launch_game(world: Optional[str] = None, area: Optional[str] = None, disc: Optional[Disc] = None,
                exe: Optional[str] = None, extra_env: Optional[dict] = None) -> dict:
    """Start the port, booting straight into a world and area when given (docs/RUNNING.md)."""
    exe = exe or _env("ECHOES_PORT_EXE")
    if not exe or not os.path.isfile(exe):
        raise WorkshopError("Set ECHOES_PORT_EXE to the game's executable (metroid_prime2_port.exe on Windows).")
    env = dict(os.environ)
    image = disc.path if disc else disc_path()
    if image:
        env["MP2_DISC"] = image
    request = None
    if world:
        if disc is None:
            raise WorkshopError("Booting into a room needs ECHOES_DISC, to turn names into asset IDs.")
        w = disc.world(world)
        a = disc.area(w, area) if area else w["areas"][0]
        # One request carries both (docs/RUNNING.md: a separate MP2_TELEPORT is served before the world
        # loads), and the area must be an ID or MLVL name, never a slot index.
        request = f"{w['mlvl']:08x},{a['mrea']:08x}"
        env["MP2_WORLD"] = request
        env["MP2_SKIP_FRONTEND"] = "1"
        env.setdefault("MP2_NEW_GAME", "1")
    if sys.platform != "win32":
        env.setdefault("MP2_CONSOLE", str(console_port()))
    env.update(extra_env or {})
    kwargs = {"cwd": str(pathlib.Path(exe).resolve().parent), "env": env,
              "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([exe], **kwargs)
    return {"pid": process.pid, "boot_request": request,
            "console": None if sys.platform == "win32" else console_port()}


def to_json(value) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False)
