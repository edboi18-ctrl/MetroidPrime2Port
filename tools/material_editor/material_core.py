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

    def summary(self):
        parts = ["%d texture(s)" % len(self.textures), "%d stage(s)" % len(self.stages)]
        if self.reflective:
            parts.append("reflective")
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


# ------------------------------------------------------------------------- texture pack's PBR maps
def index_pack(folder):
    """{lower-case file name: path} of every file under a texture pack folder."""
    out = {}
    if folder and os.path.isdir(folder):
        for root, _dirs, files in os.walk(folder):
            for name in files:
                out.setdefault(name.lower(), os.path.join(root, name))
    return out


def pack_maps(pack_index, pack_name):
    """(texture, normal map, ORM map) paths the user pack has for a texture's pack name (Aurora's
    texture_replacement.cpp: the _nrm and _orm siblings of the replacement), each None if absent."""
    if not pack_name:
        return None, None, None
    stem = pack_name[:-4] if pack_name.lower().endswith(".dds") else pack_name
    found = []
    for suffix in ("", "_nrm", "_orm"):
        hit = None
        for ext in (".dds", ".png"):
            hit = pack_index.get((stem + suffix + ext).lower())
            if hit:
                break
        found.append(hit)
    return tuple(found)


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
