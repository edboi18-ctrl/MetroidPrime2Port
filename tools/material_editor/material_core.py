"""The material editor's workings, without the window (tools/material_editor/mp2_material_editor.py).

- Where things are: the port's user folder and its settings.cfg (the disc image's path, the mods
  switch and the mods turned off), the mods folder and the user texture pack, as platform/main.cpp,
  platform/port_mods.cpp and platform/port_textures.cpp find them.
- The disc: worlds and their areas (MLVL), every CMDL, each area's world-geometry material set and
  each model's material sets, read through tools/echoes_disc.py and tools/pbr/disc_texture_scan.py.
- A material's key, as the game computes it (platform/include/port_material_overrides.h): FNV-1a 64 of
  its bytes, then the set's texture id for each texture index it lists (FFFFFFFF past the list).
- Texture thumbnails: GX texture decoding (I4, I8, IA4, IA8, RGB565, RGB5A3, RGBA8, C4, C8, C14X2,
  CMPR) of a TXTR's mip nearest a size, written as PNG.
- materials.json: the overrides the game reads from each enabled mod folder, read and written here.

Standard library only. Docs: docs/MATERIAL_EDITOR.md.
"""

import json
import os
import struct
import sys
import threading
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "pbr")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import echoes_disc as ed  # noqa: E402
import disc_texture_scan as ds  # noqa: E402

APP_FOLDER_WINDOWS = "Metroid Prime 2 Echoes"  # platform/main.cpp kAppFolder (no ':' on Windows)
APP_FOLDER = "Metroid Prime 2: Echoes"
DEFAULT_MOD = "Material Editor"
OVERRIDES_FILE = "materials.json"

# ------------------------------------------------------------------------------------ the fields
# (name in materials.json, label, kind, low, high, the port's value when left out, what it does)
FIELDS = [
    ("pbr", "PBR maps", "bool", None, None, None,
     "Whether the material uses the PBR maps your texture pack has for its texture (normal and "
     "occlusion/roughness/metal). Left out: used when the pack has them."),
    ("pbr_strength", "PBR strength", "float", 0.0, 1.0, 1.0,
     "How much of the PBR lighting the material takes: 0 is the game's own lighting, 1 all PBR."),
    ("roughness", "Roughness", "float", 0.0, 4.0, 1.0,
     "Times the roughness map: under 1 glossier, sharper highlights; over 1 duller."),
    ("metal", "Metal", "float", 0.0, 4.0, 1.0,
     "Times the metal map: higher reflects more like metal, tinted by the texture."),
    ("normal", "Normal map", "float", 0.0, 4.0, 1.0,
     "How strongly the normal map tilts the surface: 0 flat, 1 as made, higher bumpier."),
    ("occlusion", "Occlusion", "float", 0.0, 1.0, 1.0,
     "How much the occlusion map darkens creases: 0 none, 1 as made."),
    ("highlight", "Highlights", "float", 0.0, 4.0, 1.0,
     "Times the PBR highlights (over the Video > Quality PBR highlight setting)."),
    ("luminance", "Luminance", "float", 0.0, 8.0, 1.0,
     "How bright the material's light-emitting texels are (its glow layer: lights, screens, Phazon), "
     "times the game's Luminance setting (Video > Quality). 0 turns the glow off; materials without a "
     "glow layer (the list marks those with one *glows*) don't change."),
    ("reflection", "Live reflection", "bool", None, None, None,
     "For materials with a reflection (environment map): on reflects the room as it is (the live "
     "probe, Video > Quality > Live reflections on); off keeps the painted reflection."),
    ("reflection_strength", "Reflection strength", "float", 0.0, 4.0, 1.0,
     "How bright the live reflection is."),
    ("reflection_blur", "Reflection blur", "float", 0.0, 100.0, 0.0,
     "0 a sharp mirror; higher a softer, rougher reflection (percent)."),
]
FIELD_NAMES = [f[0] for f in FIELDS]


def clamp_field(name, value):
    for fname, _label, kind, lo, hi, _default, _help in FIELDS:
        if fname == name:
            if kind == "bool":
                return bool(value)
            v = float(value)
            return lo if v != v else max(lo, min(hi, v))  # NaN -> low
    raise KeyError(name)


# ------------------------------------------------------------------------------------- paths
def per_user_folder():
    """The per-user folder the port uses when its own folder can't hold user/ (PerUserFolder)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return os.path.join(base, APP_FOLDER_WINDOWS) if base else None
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        return os.path.join(home, "Library", "Application Support", APP_FOLDER) if home else None
    data = os.environ.get("XDG_DATA_HOME")
    if data and data.startswith("/"):
        return os.path.join(data, APP_FOLDER)
    home = os.environ.get("HOME")
    return os.path.join(home, ".local", "share", APP_FOLDER) if home else None


def find_user_folder(game_folder):
    """The port's user folder for a game in `game_folder` (DesktopUserFolder): MP2_USER_PATH, else
    user/ beside the game unless only the per-user folder has a settings.cfg (an older install), else
    the per-user folder. None when there is none."""
    env = os.environ.get("MP2_USER_PATH")
    if env:
        return env
    portable = os.path.join(game_folder, "user") if game_folder else None
    per_user = per_user_folder()
    here = portable is not None and os.path.isfile(os.path.join(portable, "settings.cfg"))
    legacy = per_user is not None and os.path.isfile(os.path.join(per_user, "settings.cfg"))
    if portable is not None and (here or not legacy) and os.path.isdir(portable):
        return portable
    if per_user is not None and os.path.isdir(per_user):
        return per_user
    return portable if portable is not None and os.path.isdir(portable) else None


def read_settings(user_folder):
    """settings.cfg's key=value lines (as the port's LoadSettings reads them); {} without one."""
    values = {}
    if not user_folder:
        return values
    try:
        with open(os.path.join(user_folder, "settings.cfg"), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
    except OSError:
        pass
    return values


def setting_bool(value, default):
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "off", "no")


def mods_folder(user_folder):
    """MP2_MODS, else <user>/mods (PortMods::Folder)."""
    env = os.environ.get("MP2_MODS")
    if env:
        return env
    return os.path.join(user_folder, "mods") if user_folder else None


def user_texture_folder(user_folder):
    """MP2_USER_TEXTURES, else <user>/user_textures (port_textures.h)."""
    env = os.environ.get("MP2_USER_TEXTURES")
    if env:
        return env
    return os.path.join(user_folder, "user_textures") if user_folder else None


DISC_EXTENSIONS = (".iso", ".gcm", ".ciso")


def find_disc(settings, game_folder):
    """The disc image: settings.cfg's disc_path, else MP2_DISC, else an image beside the game."""
    for path in (settings.get("disc_path"), os.environ.get("MP2_DISC")):
        if path and os.path.isfile(path):
            return path
    if game_folder and os.path.isdir(game_folder):
        for name in sorted(os.listdir(game_folder)):
            if name.lower().endswith(DISC_EXTENSIONS + (".rvz", ".wia", ".gcz")):
                return os.path.join(game_folder, name)
    return None


def disc_problem(path):
    """Why the image at `path` can't be read here (RVZ/WIA/GCZ need converting), or None."""
    return ds.disc_problem(path)


# ----------------------------------------------------------------------------------- the key
FNV_OFFSET, FNV_PRIME, MASK64 = 0xCBF29CE484222325, 0x100000001B3, 0xFFFFFFFFFFFFFFFF


def _fnv(h, data):
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & MASK64
    return h


def material_key(material, texture_ids):
    """port_material_overrides.h's MaterialKey: `material` its bytes, `texture_ids` the set's ids."""
    h = _fnv(FNV_OFFSET, material)
    if len(material) < 8:
        return h
    (count,) = struct.unpack_from(">I", material, 4)
    for i in range(min(count, 32)):
        if 8 + 4 * i + 4 > len(material):
            break
        (index,) = struct.unpack_from(">I", material, 8 + 4 * i)
        word = struct.pack(">I", texture_ids[index]) if index < len(texture_ids) else b"\xff\xff\xff\xff"
        h = _fnv(h, word)
    return h


def key_text(key):
    return "%016x" % key


# ------------------------------------------------------------------------------------ materials
GX_TG_NRM = 1
# GXEnum.h: GXTevColorArg, GXTexGenSrc, GXTevOp, GXTevRegID
CC_CPREV, CC_TEXC, CC_RASC, CC_RASA, CC_ZERO = 0, 8, 10, 11, 15
GX_TG_TEX0, GX_TG_TEX7 = 4, 11
GX_TEV_ADD, GX_TEVPREV = 0, 0
NO_TEX = 0xFF  # GX_TEXMAP_NULL, GX_TEXCOORD_NULL


def _color_args(color_in):
    return color_in & 0x1F, (color_in >> 5) & 0x1F, (color_in >> 10) & 0x1F, (color_in >> 15) & 0x1F


def glow_stages(stages, texgens):
    """The indices of a material's glow stages: those the game's Luminance scales (Aurora's
    port_emissive_stages, extern/aurora/lib/gx/shader_info.cpp, is the same rule over the GX state).
    `stages` as disc_texture_scan.parse_material gives them ([order, colour in, alpha in, colour op,
    ...]), `texgens` its texgen words. A glow stage (GX: out = d + lerp(a, b, c)), after the first:
      - adds its texture onto what came before: d is the previous colour and the texture is in the
        lerp (as a plain term, or as the factor c of a lerp from 0), or d is the texture and the
        previous colour is in the lerp (tools/pbr/texture_report.py's glow, as the disc has it:
        CPREV + TEXC after the lit diffuse);
      - is not lit: neither the lit colour nor its alpha (RASC, RASA) is in the lerp, and no later
        stage multiplies the previous colour by them;
      - takes its texture's coordinates from the model's UVs (not an environment or projected map),
        and writes the previous colour with an add."""
    out = []
    for i in range(1, len(stages)):
        order, color_in, color_op = stages[i][0], stages[i][1], stages[i][3]
        texmap, texcoord = (order >> 8) & 0xFF, order & 0xFF
        if texmap == NO_TEX or texcoord == NO_TEX or texcoord >= len(texgens):
            continue
        if not GX_TG_TEX0 <= (texgens[texcoord] >> 4) & 0x1F <= GX_TG_TEX7:
            continue
        if (color_op & 0xF) != GX_TEV_ADD or (color_op >> 9) & 3 != GX_TEVPREV:
            continue
        a, b, c, d = _color_args(color_in)
        if CC_RASC in (a, b, c) or CC_RASA in (a, b, c):
            continue
        if d == CC_CPREV:
            glow = a == CC_ZERO if c == CC_TEXC else CC_TEXC in (a, b)
        elif d == CC_TEXC:
            glow = CC_CPREV in (a, b)
        else:
            glow = False
        if not glow:
            continue
        lit_later = False
        for later in stages[i + 1:]:
            la, lb, lc, _ld = _color_args(later[1])
            if CC_CPREV in (la, lb, lc) and (CC_RASC in (la, lb, lc) or CC_RASA in (la, lb, lc)):
                lit_later = True
                break
        if not lit_later:
            out.append(i)
    return out


class Material:
    """One material of a set: its index, key, texture ids (in its slot order) and what it does."""

    def __init__(self, index, data, set_ids, set_index=0):
        self.index = index
        self.set_index = set_index
        self.data = data
        self.key = material_key(data, set_ids)
        self.textures = []
        self.flags = self.blend = 0
        self.stages = []
        self.texgens = []
        self.problem = None
        try:
            parsed = ds.parse_material(data)
            self.flags = parsed["flags"]
            self.blend = parsed["blend"]
            self.stages = parsed["stages"]
            self.texgens = parsed["texgens"]
            self.textures = [set_ids[i] if i < len(set_ids) else None for i in parsed["textures"]]
        except (struct.error, ValueError) as e:
            self.problem = str(e)

    @property
    def key_text(self):
        return key_text(self.key)

    @property
    def reflective(self):
        """A texgen from the normal: an environment map, which Live reflection can replace."""
        return any(((t >> 4) & 0x1F) == GX_TG_NRM for t in self.texgens)

    @property
    def glows(self):
        """A glow layer (light-emitting texels), which Luminance scales."""
        return bool(glow_stages(self.stages, self.texgens))

    def summary(self):
        parts = ["%d texture(s)" % len(self.textures), "%d stage(s)" % len(self.stages)]
        if self.reflective:
            parts.append("reflective")
        if self.glows:
            parts.append("glows")
        if self.problem:
            parts.append("unreadable: " + self.problem)
        return ", ".join(parts)


class Library:
    """The disc's worlds, areas and models, and their materials (read when asked for)."""

    def __init__(self, disc_path, log=lambda msg: None):
        problem = disc_problem(disc_path)
        if problem:
            raise ValueError(problem)
        self.iso = ed.Iso(disc_path)
        # One reader at a time: the window reads thumbnails while the preview loads in the background,
        # and every read is a seek and a read of the one file.
        lock = threading.RLock()
        unlocked_read = self.iso.read

        def read(*args, **kwargs):
            with lock:
                return unlocked_read(*args, **kwargs)

        self.iso.read = read
        self.paks = ed.load_paks(self.iso)
        self.scanner = ds.Scanner(self.paks, log)
        self.where = self.scanner.where
        self.names = {}  # resource id -> a PAK's name for it
        for pak in self.paks:
            for name, (_t, rid) in pak.names.items():
                self.names.setdefault(rid, name)
        self.worlds = self._worlds()
        self.models = sorted(rid for rid, (_p, t) in self.where.items() if t == "CMDL")
        self._sets = {}
        self._txtr = {}

    def close(self):
        try:
            self.iso.f.close()
        except (AttributeError, OSError):
            pass

    def _worlds(self):
        worlds = []
        for rid, (pak, rtype) in sorted(self.where.items()):
            if rtype != "MLVL":
                continue
            try:
                world = ed.parse_mlvl(pak.get(rid))
            except (struct.error, ValueError, IndexError, AssertionError):
                continue
            name = self._string(world.get("strg")) or self.names.get(rid) or "World %08X" % rid
            areas = []
            for area in world["areas"]:
                label = self._string(area.get("strg")) or area["iname"] or "Area %08X" % area["mrea"]
                areas.append({"mrea": area["mrea"], "name": label, "internal": area["iname"]})
                self.scanner.area_names[area["mrea"]] = area["iname"]
            worlds.append({"mlvl": rid, "name": name, "pak": os.path.basename(pak.path), "areas": areas})
        return worlds

    def _string(self, strg_id):
        entry = self.where.get(strg_id) if strg_id not in (None, ds.NO_ASSET) else None
        if entry is None or entry[1] != "STRG":
            return None
        try:
            strings = ed.strg_strings(entry[0].get(strg_id))
        except (struct.error, ValueError, AssertionError, IndexError):
            return None
        return strings[0].strip() if strings and strings[0].strip() else None

    def model_name(self, rid):
        name = self.names.get(rid)
        return "%s (CMDL %08X)" % (name, rid) if name else "CMDL %08X" % rid

    def area_materials(self, mrea_id):
        """The area's world-geometry materials."""
        if ("MREA", mrea_id) not in self._sets:
            pak, _t = self.where[mrea_id]
            section = ds.mrea_material_set(pak.get(mrea_id))
            self._sets[("MREA", mrea_id)] = [self._set_materials(section, 0)]
        return self._sets[("MREA", mrea_id)][0]

    def model_materials(self, cmdl_id):
        """[[Material]] for each of the model's material sets (most have one)."""
        if ("CMDL", cmdl_id) not in self._sets:
            pak, _t = self.where[cmdl_id]
            sets = ds.cmdl_material_sets(self.scanner.cmdl_bytes(pak, cmdl_id))
            self._sets[("CMDL", cmdl_id)] = [self._set_materials(s, n) for n, s in enumerate(sets)]
        return self._sets[("CMDL", cmdl_id)]

    @staticmethod
    def _set_materials(section, set_index):
        ids, datas = ds.parse_material_set(section)
        return [Material(i, d, ids, set_index) for i, d in enumerate(datas)]

    def resource(self, rid):
        """A resource's whole bytes (inflated), as its PAK holds it."""
        pak, _t = self.where[rid]
        return pak.get(rid)

    def texture(self, txtr_id):
        """The TXTR's bytes, or None."""
        entry = self.where.get(txtr_id)
        if entry is None or entry[1] != "TXTR":
            return None
        if txtr_id not in self._txtr:
            self._txtr[txtr_id] = entry[0].get(txtr_id)
        return self._txtr[txtr_id]

    def texture_pack_name(self, txtr_id):
        data = self.texture(txtr_id)
        if data is None:
            return None
        try:
            return ds.texture_name(data)[0]
        except (struct.error, ValueError):
            return None


# ------------------------------------------------------------------------------ the texture pack
# How the game finds a texture's replacement and its PBR maps, so the editor finds the same files:
#   names     Aurora's parse_replacement_filename (extern/aurora/lib/gfx/texture_replacement.cpp):
#             tex1_<w>x<h>[_m]_<texture hash>[_<palette hash>]_<format>[_arb].dds/.png, a hash may be
#             "$" (any), the format a number or a GameCube format name. The name's other parts don't
#             matter: a file is the texture whose key it parses to, wherever it sits in the folder.
#   lookup    find_source_replacement_key_locked: the exact key, then any palette, then any texels.
#   order     load_replacement_directory: files sorted by path (case-insensitive), the first of a key
#             wins; the user pack before the built-in set.
#   device    PortTextures' ResolveDirectory: a pack with device folders (xbox, keyboard, ...) is read
#             from the active device's folder only.
#   PBR maps  PortPbrMaps (platform/port_pbr_maps.cpp, port_pbr_rules.h SplitMapName): "<name>_nrm"
#             and "<name>_orm" (.dds or .png) anywhere in the folder, keyed by the name without the
#             suffix, looked up in the same order; both are needed.
DEVICE_DIRS = ("xbox", "playstation", "switch", "gamecube", "standard", "keyboard")
# PortTextures' first device is "keyboard" (sDevice) until a pad is used.
DEVICE_PREFERENCE = ("keyboard", "xbox", "standard", "playstation", "switch", "gamecube")
WILDCARD_TEXTURE = 0xFFFFFFFFFFFFFFFF  # aurora/texture.hpp kWildcardTextureHash
WILDCARD_TLUT = 0xFFFFFFFFFFFFFFFE     # kWildcardTlutHash
GX_FORMAT_NAMES = {"i4": 0x0, "i8": 0x1, "ia4": 0x2, "ia8": 0x3, "rgb565": 0x4, "rgb5a3": 0x5, "rgba8": 0x6,
                   "cmpr": 0xE, "c4": 0x8, "c8": 0x9, "c14x2": 0xA}  # GXEnum.h GXTexFmt


def _u32(text):
    if not text or not all("0" <= ch <= "9" for ch in text):
        return None
    value = int(text)
    return value if value <= 0xFFFFFFFF else None


def _hex64(text):
    if not text or not all(ch in "0123456789abcdefABCDEF" for ch in text):
        return None
    return int(text, 16) & 0xFFFFFFFFFFFFFFFF  # as the C++ shift does


def parse_pack_name(filename):
    """The key (texture hash, palette hash, width, height, GX format, has a palette) of a pack file's
    name, or None where the game would skip the file (Aurora's parse_replacement_filename)."""
    dot = filename.rfind(".")
    if dot < 0 or filename[dot:].lower() not in (".dds", ".png"):
        return None
    stem = filename[:dot]
    if not stem.startswith("tex1_"):
        return None
    parts = stem.split("_")
    if len(parts) < 4 or len(parts) > 6 or parts[0] != "tex1":
        return None
    if "x" not in parts[1]:
        return None
    w_text, h_text = parts[1].split("x", 1)
    width, height = _u32(w_text), _u32(h_text)
    if width is None or height is None:
        return None
    index = 3 if parts[2] == "m" else 2
    remaining = len(parts) - index
    if remaining not in (2, 3):
        return None
    tex_hash = WILDCARD_TEXTURE if parts[index] == "$" else _hex64(parts[index])
    if tex_hash is None:
        return None
    fmt_part = parts[-1]
    if fmt_part == "arb":
        fmt_part = parts[-2]
        remaining -= 1
    fmt = _u32(fmt_part)
    if fmt is None:
        fmt = GX_FORMAT_NAMES.get(fmt_part.lower())
    if fmt is None:
        return None
    tlut_hash = 0
    has_tlut = remaining == 3
    if has_tlut:
        tlut_part = parts[index + 1]
        tlut_hash = WILDCARD_TLUT if tlut_part == "$" else _hex64(tlut_part)
        if tlut_hash is None:
            return None
    return (tex_hash, tlut_hash, width, height, fmt, has_tlut)


def split_map_name(filename):
    """(the pack name it belongs to, "nrm" or "orm") of a PBR map's file name, or None
    (port_pbr_rules.h SplitMapName)."""
    lower = filename.lower()
    for ext in (".dds", ".png"):
        for kind in ("nrm", "orm"):
            suffix = "_%s%s" % (kind, ext)
            if len(lower) > len(suffix) and lower.endswith(suffix):
                return filename[:-len(suffix)] + ".dds", kind
    return None


def _is_sidecar_mip(stem):
    """Aurora's is_sidecar_mip: "<name>_mip<digits>" is a mip level of <name>, not a texture."""
    i = len(stem)
    while i > 0 and stem[i - 1].isdigit():
        i -= 1
    return i != len(stem) and i >= 4 and stem[i - 4:i] == "_mip"


def _lookup_order(key):
    """The keys Aurora tries for a texture's key, in order."""
    yield key
    tex_hash, tlut_hash, w, h, fmt, has_tlut = key
    if has_tlut:
        yield (tex_hash, WILDCARD_TLUT, w, h, fmt, has_tlut)
    yield (WILDCARD_TEXTURE, tlut_hash, w, h, fmt, has_tlut)


def resolve_pack_folder(root, device=None):
    """(the folder the game reads in the pack at `root`, why not) as PortTextures' ResolveDirectory:
    the device's folder when the pack has device folders, else the root."""
    if not root or not os.path.isdir(root):
        return None, "no folder"
    devices = (device,) if device else DEVICE_PREFERENCE
    for name in devices:
        if os.path.isdir(os.path.join(root, name)):
            return os.path.join(root, name), None
    if any(os.path.isdir(os.path.join(root, name)) for name in DEVICE_DIRS):
        return None, "device folders, none for %s" % "/".join(devices)
    return root, None


class TexturePack:
    """The pack folders the game reads (the user pack first), indexed as the game indexes them:
    textures and PBR maps by key. `folders` says what was read: [(root, folder read or None, files,
    note)]."""

    def __init__(self, roots, device=None):
        self.textures = {}
        self.maps = {}
        self.folders = []
        self.unparsed = 0
        for root in roots:
            if root:
                self._read(root, device)

    def _read(self, root, device):
        folder, why = resolve_pack_folder(root, device)
        if folder is None:
            self.folders.append((root, None, 0, why))
            return
        found = []
        for base, dirs, files in os.walk(folder, followlinks=True):
            dirs.sort()
            for name in files:
                path = os.path.join(base, name)
                found.append((tuple(c.lower() for c in os.path.relpath(path, folder).split(os.sep)),
                              tuple(os.path.relpath(path, folder).split(os.sep)), path, name))
        found.sort()
        count = 0
        for _ci, _cs, path, name in found:
            dot = name.rfind(".")
            if dot < 0 or name[dot:].lower() not in (".dds", ".png") or _is_sidecar_mip(name[:dot]):
                continue
            split = split_map_name(name)
            if split is not None:
                key = parse_pack_name(split[0])
                if key is None:
                    self.unparsed += 1
                    continue
                self.maps.setdefault(key, {}).setdefault(split[1], path)
                continue
            key = parse_pack_name(name)
            if key is None:
                self.unparsed += 1
                continue
            if key not in self.textures:
                self.textures[key] = path
                count += 1
        self.folders.append((root, folder, count, None))

    @property
    def pairs(self):
        return sum(1 for m in self.maps.values() if "nrm" in m and "orm" in m)

    def find(self, pack_name):
        """(texture file, normal map, ORM map) the game would use for the texture whose pack name (as
        disc_texture_scan.texture_name gives it) is `pack_name`; each None if there's none."""
        key = parse_pack_name(pack_name) if pack_name else None
        if key is None:
            return None, None, None
        texture = next((self.textures[k] for k in _lookup_order(key) if k in self.textures), None)
        maps = next((self.maps[k] for k in _lookup_order(key) if k in self.maps), None)
        if not maps or "nrm" not in maps or "orm" not in maps:
            return texture, None, None
        return texture, maps["nrm"], maps["orm"]

    def summary(self):
        parts = []
        for root, folder, count, note in self.folders:
            if folder is None:
                parts.append("%s: %s" % (root, note))
            else:
                parts.append("%d texture(s) in %s" % (count, folder))
        if self.maps:
            parts.append("%d PBR map pair(s)" % self.pairs)
        return "; ".join(parts) if parts else "no texture pack"


def texture_pack_roots(user_folder, settings):
    """The pack folders the game layers, user pack first: the user pack (unless the texture_pack
    setting is off) and an MP2_TEXTURES built-in set on disk. The set built into the executable has
    only the button prompts, so it isn't read here."""
    roots = []
    if setting_bool(settings.get("texture_pack"), True):
        roots.append(user_texture_folder(user_folder))
    env = os.environ.get("MP2_TEXTURES")
    if env:
        roots.append(env)
    return [r for r in roots if r]


# ------------------------------------------------------------------------------- texture decode
TXTR_PALETTE_FORMATS = {0: "IA8", 1: "RGB565", 2: "RGB5A3"}


def _rgb565(v):
    return (((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31, 255)


def _rgb5a3(v):
    if v & 0x8000:
        return (((v >> 10) & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, (v & 31) * 255 // 31, 255)
    return (((v >> 8) & 15) * 17, ((v >> 4) & 15) * 17, (v & 15) * 17, ((v >> 12) & 7) * 255 // 7)


def _ia8(v):
    i = v & 0xFF
    return (i, i, i, v >> 8)


def _palette(fmt, data):
    count = len(data) // 2
    words = struct.unpack(">%dH" % count, data[:count * 2])
    conv = {0: _ia8, 1: _rgb565, 2: _rgb5a3}.get(fmt, _rgb5a3)
    return [conv(w) for w in words]


def decode_gx(data, width, height, gx_format, palette=None):
    """RGBA bytes (row-major, 4 per texel) of a GX texture level, as Dolphin's TextureDecoder reads the
    tiled formats (tiles left to right, top to bottom; texels within a tile likewise)."""
    sx, sy, tile = ds.GX_TILES[gx_format]
    tw, th = 1 << sx, 1 << sy
    tiles_x = (width + tw - 1) // tw
    tiles_y = (height + th - 1) // th
    out = bytearray(width * height * 4)

    def put(x, y, rgba):
        if x < width and y < height:
            o = (y * width + x) * 4
            out[o:o + 4] = bytes(rgba)

    pos = 0
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            block = data[pos:pos + tile]
            pos += tile
            if len(block) < tile:
                return bytes(out)
            x0, y0 = tx * tw, ty * th
            if gx_format == 0x0:  # I4
                for i in range(64):
                    b = block[i >> 1]
                    v = (b >> 4) if i % 2 == 0 else (b & 15)
                    v *= 17
                    put(x0 + i % 8, y0 + i // 8, (v, v, v, 255))
            elif gx_format == 0x1:  # I8
                for i in range(32):
                    v = block[i]
                    put(x0 + i % 8, y0 + i // 8, (v, v, v, 255))
            elif gx_format == 0x2:  # IA4
                for i in range(32):
                    b = block[i]
                    v = (b & 15) * 17
                    put(x0 + i % 8, y0 + i // 8, (v, v, v, (b >> 4) * 17))
            elif gx_format == 0x3:  # IA8
                for i in range(16):
                    a, v = block[2 * i], block[2 * i + 1]
                    put(x0 + i % 4, y0 + i // 4, (v, v, v, a))
            elif gx_format == 0x4:  # RGB565
                for i in range(16):
                    put(x0 + i % 4, y0 + i // 4, _rgb565(struct.unpack_from(">H", block, 2 * i)[0]))
            elif gx_format == 0x5:  # RGB5A3
                for i in range(16):
                    put(x0 + i % 4, y0 + i // 4, _rgb5a3(struct.unpack_from(">H", block, 2 * i)[0]))
            elif gx_format == 0x6:  # RGBA8: AR pairs, then GB pairs
                for i in range(16):
                    a, r = block[2 * i], block[2 * i + 1]
                    g, b = block[32 + 2 * i], block[33 + 2 * i]
                    put(x0 + i % 4, y0 + i // 4, (r, g, b, a))
            elif gx_format == 0x8:  # C4
                for i in range(64):
                    b = block[i >> 1]
                    idx = (b >> 4) if i % 2 == 0 else (b & 15)
                    put(x0 + i % 8, y0 + i // 8, palette[idx] if palette and idx < len(palette) else (0, 0, 0, 255))
            elif gx_format == 0x9:  # C8
                for i in range(32):
                    idx = block[i]
                    put(x0 + i % 8, y0 + i // 8, palette[idx] if palette and idx < len(palette) else (0, 0, 0, 255))
            elif gx_format == 0xA:  # C14X2
                for i in range(16):
                    idx = struct.unpack_from(">H", block, 2 * i)[0] & 0x3FFF
                    put(x0 + i % 4, y0 + i // 4, palette[idx] if palette and idx < len(palette) else (0, 0, 0, 255))
            elif gx_format == 0xE:  # CMPR: four 4x4 DXT1 blocks, big-endian colours, MSB-first indices
                for sub in range(4):
                    c0, c1, bits = struct.unpack_from(">HHI", block, 8 * sub)
                    p0, p1 = _rgb565(c0), _rgb565(c1)
                    if c0 > c1:
                        p2 = tuple((2 * a + b) // 3 for a, b in zip(p0[:3], p1[:3])) + (255,)
                        p3 = tuple((a + 2 * b) // 3 for a, b in zip(p0[:3], p1[:3])) + (255,)
                    else:
                        p2 = tuple((a + b) // 2 for a, b in zip(p0[:3], p1[:3])) + (255,)
                        p3 = (0, 0, 0, 0)
                    colours = (p0, p1, p2, p3)
                    sx0, sy0 = x0 + (sub % 2) * 4, y0 + (sub // 2) * 4
                    for i in range(16):
                        idx = (bits >> (30 - 2 * i)) & 3
                        put(sx0 + i % 4, sy0 + i // 4, colours[idx])
            else:
                raise ValueError("GX format %d" % gx_format)
    return bytes(out)


def decode_txtr(txtr, max_size=128):
    """(width, height, RGBA) of the TXTR's largest mip no bigger than `max_size` on its longer side
    (the smallest there is, else). Raises ValueError for a format it can't read."""
    texel, width, height, mips = struct.unpack_from(">IHHI", txtr, 0)
    gx_format = ds.TEXEL_TO_GX.get(texel)
    if gx_format is None:
        raise ValueError("texel format %d" % texel)
    pos = 12
    palette = None
    if texel in ds.PALETTE_TEXELS:
        pal_format, pw, ph = struct.unpack_from(">IHH", txtr, pos)
        pos += 8
        palette = _palette(pal_format, txtr[pos:pos + pw * ph * 2])
        pos += pw * ph * 2
    w, h = width, height
    for level in range(max(mips, 1)):
        size = ds.gx_base_size(w, h, gx_format)
        last = level == max(mips, 1) - 1
        if max(w, h) <= max_size or last or w <= 1 or h <= 1:
            return w, h, decode_gx(txtr[pos:pos + size], w, h, gx_format, palette)
        pos += size
        w, h = max(w // 2, 1), max(h // 2, 1)
    raise ValueError("no mip")


def png_bytes(width, height, rgba):
    """A PNG (8-bit RGBA, no filtering) of the texels."""
    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------------------- materials.json
class Overrides:
    """One materials.json: {key text: {field: value}}, with whatever else the file holds kept."""

    def __init__(self, path):
        self.path = path
        self.extra = {}
        self.materials = {}
        self.problem = None
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
                if not isinstance(doc, dict):
                    raise ValueError("not a JSON object")
                for k, v in doc.items():
                    if k == "materials" and isinstance(v, dict):
                        for key, fields in v.items():
                            if isinstance(fields, dict) and len(key) == 16:
                                self.materials[key.lower()] = dict(fields)
                    elif k != "version":
                        self.extra[k] = v
            except (OSError, ValueError) as e:
                self.problem = "%s: %s" % (path, e)

    def get(self, key):
        return self.materials.get(key, {})

    def set(self, key, fields):
        """Sets a material's fields (only FIELD_NAMES and "note" kept); none left removes it."""
        kept = {k: (clamp_field(k, v) if k in FIELD_NAMES else str(v)) for k, v in fields.items()
                if (k in FIELD_NAMES or k == "note") and v is not None and v != ""}
        if kept:
            self.materials[key] = kept
        else:
            self.materials.pop(key, None)

    def save(self):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        doc = {"version": 1}
        doc.update(self.extra)
        doc["materials"] = {k: self._ordered(v) for k, v in sorted(self.materials.items())}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.path)  # the game never reads a half-written file

    @staticmethod
    def _ordered(fields):
        order = FIELD_NAMES + ["note"]
        out = {}
        for name in order:
            if name in fields:
                v = fields[name]
                out[name] = round(v, 4) if isinstance(v, float) else v
        return out


def overrides_path(mods, mod_name=DEFAULT_MOD):
    return os.path.join(mods, mod_name, OVERRIDES_FILE) if mods else None
