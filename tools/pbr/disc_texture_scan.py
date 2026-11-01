#!/usr/bin/env python3
"""Read every model and area material on the Echoes disc and write the texture manifest, offline.

The same records MP2_TEXTURE_MANIFEST=1 writes while playing (platform/include/port_texture_manifest.h),
but for every material on the disc at once, so nothing has to be played: each CMDL's material sets
and each MREA's world-geometry material set, read as CCubeMaterial::SetCurrent reads them, with each
texture's texture-pack file name computed as the port computes it. Feed the result to
tools/pbr/texture_report.py with your pack:

    python tools/pbr/disc_texture_scan.py "<disc image>.iso" -o texture_manifest_disc.jsonl
    python tools/pbr/texture_report.py texture_manifest_disc.jsonl "<Dolphin>/Load/Textures/G2M"
    (Windows: py -3 tools\\pbr\\disc_texture_scan.py "C:\\...\\Echoes.iso" ...)

Needs a GameCube ISO/GCM or CISO of G2ME01 (a PAL dump works the same way), Python 3.8+ and
the standard library. `pip install xxhash` makes it several times faster (the built-in XXH64 is pure
Python); the output is the same either way.

How each fact matches the port (the engine code is the reference):
  pack name   Aurora's texture_replacement.cpp: XXH64 (seed 0) of the base level's bytes, sized as
              GXGetTexBufferSize (whole tiles), and for a paletted texture XXH64 of the palette
              entries from the lowest to the highest index the texture uses; CTexture
              (DolphinCTexture.cpp) hands GX the TXTR's bytes unchanged, so the disc bytes are the
              ones hashed. Name: tex1_{w}x{h}_{hash}[_{tluthash}]_{gx format}.dds.
  material    CCubeMaterial::SetCurrent and HandleNormalTevs: the TEV words and texgens are stored
              as CGX mirrors them, so the records equal the runtime ones.
  sections    CMDL: DolphinCModel.cpp (section count 0x24, material sets 0x28, sizes 0x2C); MREA
              v0x19: CGameArea::GetSectionIndex (geometry = header word 17, its first section is the
              area's material set).
  sky         a world's default sky is the CMDL its MLVL names after the SAVW (CWorld::Update); an
              area overrides it with an AreaAttributes object (REAA, ScriptLoader.cpp) whose
              overrideSky property (0xd208c9fa, a 4-byte CMDL id; SLdrAreaAttributes.inc) is set.
              Records of those models carry "sky": true, and the report keeps their textures out
              of the PBR candidates.
"""

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import echoes_disc as ed  # noqa: E402

# ------------------------------------------------------------------------------------- XXH64
try:
    import xxhash as _xxhash  # optional: the C implementation
except ImportError:  # pragma: no cover - depends on the machine
    _xxhash = None

_P1, _P2, _P3, _P4, _P5 = (0x9E3779B185EBCA87, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9,
                           0x85EBCA77C2B2AE63, 0x27D4EB2F165667C5)
_M = 0xFFFFFFFFFFFFFFFF


def _rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & _M


def _round(acc, lane):
    acc = (acc + lane * _P2) & _M
    return (_rotl(acc, 31) * _P1) & _M


def _merge(acc, val):
    acc ^= _round(0, val)
    return (acc * _P1 + _P4) & _M


def xxh64_pure(data, seed=0):
    """XXH64 (the reference algorithm, little-endian lanes)."""
    n = len(data)
    p = 0
    if n >= 32:
        v1 = (seed + _P1 + _P2) & _M
        v2 = (seed + _P2) & _M
        v3 = seed & _M
        v4 = (seed - _P1) & _M
        limit = n - 32
        unpack = struct.Struct("<4Q").unpack_from
        while p <= limit:
            a, b, c, d = unpack(data, p)
            v1 = _round(v1, a)
            v2 = _round(v2, b)
            v3 = _round(v3, c)
            v4 = _round(v4, d)
            p += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _M
        h = _merge(h, v1)
        h = _merge(h, v2)
        h = _merge(h, v3)
        h = _merge(h, v4)
    else:
        h = (seed + _P5) & _M
    h = (h + n) & _M
    while p + 8 <= n:
        (k,) = struct.unpack_from("<Q", data, p)
        h ^= _round(0, k)
        h = (_rotl(h, 27) * _P1 + _P4) & _M
        p += 8
    if p + 4 <= n:
        (k,) = struct.unpack_from("<I", data, p)
        h ^= (k * _P1) & _M
        h = (_rotl(h, 23) * _P2 + _P3) & _M
        p += 4
    while p < n:
        h ^= (data[p] * _P5) & _M
        h = (_rotl(h, 11) * _P1) & _M
        p += 1
    h ^= h >> 33
    h = (h * _P2) & _M
    h ^= h >> 29
    h = (h * _P3) & _M
    h ^= h >> 32
    return h


def xxh64(data):
    if _xxhash is not None:
        return _xxhash.xxh64(bytes(data), seed=0).intdigest()
    return xxh64_pure(data)


# ------------------------------------------------------------------------------------- textures
# CTexture's ETexelFormat (the TXTR header) -> GX format (DolphinCTexture.cpp).
TEXEL_TO_GX = {0: 0x0, 1: 0x1, 2: 0x2, 3: 0x3, 4: 0x8, 5: 0x9, 6: 0xA, 7: 0x4, 8: 0x5, 9: 0x6, 10: 0xE}
PALETTE_TEXELS = (4, 5, 6)  # C4, C8, C14X2
# GX format -> (tile width shift, tile height shift, bytes per tile): GXGetTexBufferSize.
GX_TILES = {0x0: (3, 3, 32), 0x8: (3, 3, 32), 0xE: (3, 3, 32),
            0x1: (3, 2, 32), 0x2: (3, 2, 32), 0x9: (3, 2, 32),
            0x3: (2, 2, 32), 0x4: (2, 2, 32), 0x5: (2, 2, 32), 0xA: (2, 2, 32),
            0x6: (2, 2, 64)}


def gx_base_size(width, height, gx_format):
    """GXGetTexBufferSize(width, height, format, no mips): whole tiles."""
    sx, sy, tile = GX_TILES[gx_format]
    return ((width + (1 << sx) - 1) >> sx) * ((height + (1 << sy) - 1) >> sy) * tile


def referenced_palette_range(image, gx_format):
    """(lowest, highest) palette index the base level uses (Aurora's compute_referenced_tlut_hash)."""
    if gx_format == 0x8:  # C4: both nibbles
        lo = min(min(b & 0xF, b >> 4) for b in image)
        hi = max(max(b & 0xF, b >> 4) for b in image)
    elif gx_format == 0x9:  # C8
        lo, hi = min(image), max(image)
    else:  # C14X2: big-endian u16, 14 bits
        values = [v & 0x3FFF for v in struct.unpack(">%dH" % (len(image) // 2), image[:len(image) // 2 * 2])]
        lo, hi = min(values), max(values)
    return lo, hi


def texture_name(txtr):
    """(pack file name, texel format) of a TXTR's bytes, or (None, texel format) if it can't be
    hashed (an unknown format, or data shorter than its header says)."""
    texel, width, height, _mips = struct.unpack_from(">IHHI", txtr, 0)
    gx_format = TEXEL_TO_GX.get(texel)
    if gx_format is None:
        return None, texel
    pos = 12
    palette = None
    if texel in PALETTE_TEXELS:
        _pal_format, pw, ph = struct.unpack_from(">IHH", txtr, pos)
        pos += 8
        palette = txtr[pos:pos + pw * ph * 2]
        pos += pw * ph * 2
    size = gx_base_size(width, height, gx_format)
    image = txtr[pos:pos + size]
    if len(image) != size or size == 0:
        return None, texel
    tex_hash = xxh64(image)
    if palette is None:
        return "tex1_%dx%d_%016x_%d.dds" % (width, height, tex_hash, gx_format), texel
    lo, hi = referenced_palette_range(image, gx_format)
    if 2 * (hi + 1) > len(palette):
        return None, texel
    tlut_hash = xxh64(palette[2 * lo:2 * (hi + 1)])
    return "tex1_%dx%d_%016x_%016x_%d.dds" % (width, height, tex_hash, tlut_hash, gx_format), texel


# ------------------------------------------------------------------------------------- materials
FLAG_KONST = 1 << 3
FLAG_INDIRECT = 1 << 10


def _words(data, pos, count):
    return list(struct.unpack_from(">%dI" % count, data, pos))


def parse_material(data):
    """A material as CCubeMaterial::SetCurrent and HandleNormalTevs read it:
    {"flags", "textures" (indices into the set's list), "blend", "stages" (the manifest's 7-number
    form), "texgens"}. Raises ValueError (or struct.error) on data that doesn't fit."""
    r = 0
    (flags, tex_count) = _words(data, r, 2)
    r += 8
    if tex_count > 32:
        raise ValueError("texture count %d" % tex_count)
    textures = _words(data, r, tex_count)
    r += 4 * tex_count
    r += 4 + 8 + 4  # vertex description, two words, the material group
    if flags & FLAG_KONST:
        (konst,) = _words(data, r, 1)
        r += 4 * (konst + 1)
    (blend,) = _words(data, r, 1)
    r += 4
    if flags & FLAG_INDIRECT:
        r += 4
    (chan_count,) = _words(data, r, 1)
    r += 4 * (chan_count + 1)
    (tev_count,) = _words(data, r, 1)
    r += 4
    if tev_count > 16:
        raise ValueError("TEV stage count %d" % tev_count)
    raw = _words(data, r, 5 * tev_count)
    r += 20 * tev_count
    orders = _words(data, r, tev_count)
    r += 4 * tev_count
    (tcg_count,) = _words(data, r, 1)
    r += 4
    if tcg_count > 8:
        raise ValueError("texgen count %d" % tcg_count)
    texgens = _words(data, r, tcg_count)
    stages = []
    for i in range(tev_count):
        color_in, alpha_in, color_op, alpha_op, mat = raw[5 * i:5 * i + 5]
        tmtc = orders[i]
        order = (tmtc & 0xFF) | ((tmtc >> 8) & 0xFF) << 8 | (mat & 0xFF) << 16
        stages.append([order, color_in, alpha_in, color_op, alpha_op, (mat >> 8) & 0xFF, (mat >> 16) & 0xFF])
    return {"flags": flags, "textures": textures, "blend": blend, "stages": stages, "texgens": texgens}


def parse_material_set(section):
    """(TXTR ids, [material bytes]) of a material-set section (CCubeModel::MakeTexturesFromMats,
    CacheMaterialData)."""
    (tex_count,) = struct.unpack_from(">I", section, 0)
    ids = _words(section, 4, tex_count)
    pos = 4 + 4 * tex_count
    (mat_count,) = struct.unpack_from(">I", section, pos)
    ends = _words(section, pos + 4, mat_count)
    base = pos + 4 + 4 * mat_count
    materials, start = [], 0
    for end in ends:
        materials.append(section[base + start:base + end])
        start = end
    return ids, materials


def cmdl_material_sets(cmdl):
    """The material-set sections of a CMDL (DolphinCModel.cpp)."""
    section_count, set_count = struct.unpack_from(">II", cmdl, 0x24)
    sizes = _words(cmdl, 0x2C, section_count)
    pos = (0x2C + section_count * 4 + 31) & ~31
    sets = []
    for i in range(min(set_count, section_count)):
        sets.append(cmdl[pos:pos + sizes[i]])
        pos += sizes[i]
    return sets


def cmdl_prefix_needed(head):
    """How many leading bytes of a CMDL hold its material sets, from its first bytes (None: more
    bytes are needed to tell)."""
    if len(head) < 0x2C:
        return None
    section_count, set_count = struct.unpack_from(">II", head, 0x24)
    table_end = 0x2C + section_count * 4
    if len(head) < table_end:
        return None
    sizes = _words(head, 0x2C, section_count)
    return ((table_end + 31) & ~31) + sum(sizes[:min(set_count, section_count)])


def inflate_prefix(buf, out_len, pos, need):
    """echoes_disc.inflate_chunks, stopping once `need(output so far)` bytes are out (`need` returns
    None until it can tell): the material sets lead a CMDL, so its geometry is never decoded."""
    out = bytearray()
    want = None
    while len(out) < out_len:
        if want is None:
            want = need(out)
        if want is not None and len(out) >= want:
            break
        (cs,) = struct.unpack_from(">h", buf, pos)
        pos += 2
        if cs < 0:
            out += buf[pos:pos - cs]
            pos += -cs
        else:
            out += ed.lzo1x_decompress(buf[pos:pos + cs])
            pos += cs
    return bytes(out)


def mrea_section(mrea, want):
    """Section `want` of a version-0x19 MREA, inflating only its block (echoes_disc.mrea_generated)."""
    h = struct.unpack_from(">32I", mrea, 0)
    if h[0] != 0xDEADBEEF:
        raise ValueError("not an MREA")
    nsec, nblk = h[16], h[28]
    pos = 0x80
    sizes = struct.unpack_from(">%dI" % nsec, mrea, pos)
    pos += (nsec * 4 + 31) & ~31
    blocks = [struct.unpack_from(">4I", mrea, pos + 16 * i) for i in range(nblk)]
    pos += (nblk * 16 + 31) & ~31
    si = 0
    for buf, dl, cl, sc in blocks:
        rd = ((cl or buf) + 31) & ~31
        if si <= want < si + sc:
            raw = mrea[pos:pos + rd]
            data = ed.inflate_chunks(raw[rd - cl:], dl) if cl else raw[:dl]
            off = sum(sizes[si:want])
            return data[off:off + sizes[want]]
        pos += rd
        si += sc
    raise ValueError("section %d not found" % want)


def mrea_material_set(mrea):
    """The world geometry's material set (CGameArea::GetSectionIndex(0): header word 17)."""
    return mrea_section(mrea, struct.unpack_from(">I", mrea, 17 * 4)[0])


OVERRIDE_SKY = b"\xd2\x08\xc9\xfa\x00\x04"  # property id 0xd208c9fa, 4 bytes of data
NO_ASSET = 0xFFFFFFFF


def area_sky_models(mrea):
    """The CMDL ids an area's AreaAttributes (REAA) objects set as its sky (overrideSky)."""
    out = set()
    for layer in ed.mrea_layers(mrea):
        for fourcc, _eid, props in ed.scly_objects(layer):
            if fourcc != "REAA":
                continue
            at = props.find(OVERRIDE_SKY)
            while at >= 0 and at + 10 <= len(props):
                (model,) = struct.unpack_from(">I", props, at + 6)
                if model != NO_ASSET:
                    out.add(model)
                at = props.find(OVERRIDE_SKY, at + 1)
    return out


# ------------------------------------------------------------------------------------- scan
class Scanner:
    """Walks PAK-like objects (echoes_disc.Pak: .path, .res {id: (type, flags, size, offset)},
    .get(id), and raw access through .iso) and yields manifest records."""

    def __init__(self, paks, log=lambda msg: None):
        self.paks = paks
        self.log = log
        self.where = {}  # resource id -> (pak, type)
        for pak in paks:
            for rid, (rtype, _fl, _sz, _off) in pak.res.items():
                self.where.setdefault(rid, (pak, rtype))
        self.names = {}  # TXTR id -> (name or None, texel)
        self.area_names = {}
        self.sky = set()  # CMDL ids drawn as a sky
        self.stats = {"cmdl": 0, "mrea": 0, "materials": 0, "bad_materials": 0, "missing_txtr": 0,
                      "unhashable_txtr": 0, "records": 0}

    def load_worlds(self):
        """Area names and each world's default sky, from the MLVLs."""
        for rid, (pak, rtype) in self.where.items():
            if rtype != "MLVL":
                continue
            try:
                world = ed.parse_mlvl(pak.get(rid))
            except (struct.error, ValueError, IndexError, AssertionError):
                continue
            if world["skybox"] != NO_ASSET:
                self.sky.add(world["skybox"])
            for area in world["areas"]:
                self.area_names[area["mrea"]] = area["iname"]

    def texture(self, txtr_id):
        if txtr_id not in self.names:
            entry = self.where.get(txtr_id)
            if entry is None or entry[1] != "TXTR":
                self.names[txtr_id] = (None, -1)
                self.stats["missing_txtr"] += 1
            else:
                try:
                    self.names[txtr_id] = texture_name(entry[0].get(txtr_id))
                except (struct.error, ValueError):
                    self.names[txtr_id] = (None, -1)
                if self.names[txtr_id][0] is None:
                    self.stats["unhashable_txtr"] += 1
        return self.names[txtr_id]

    def cmdl_bytes(self, pak, rid):
        """The CMDL's leading bytes up to the end of its material sets."""
        _t, flags, size, off = pak.res[rid]
        if not flags:
            return pak.get(rid)
        raw = pak.iso.read(pak.path, off, size)
        (out_len,) = struct.unpack_from(">I", raw, 0)
        return inflate_prefix(raw, out_len, 4, cmdl_prefix_needed)

    def records_for_set(self, section, source, sky=False):
        ids, materials = parse_material_set(section)
        for index, data in enumerate(materials):
            self.stats["materials"] += 1
            try:
                mat = parse_material(data)
            except (struct.error, ValueError):
                self.stats["bad_materials"] += 1
                continue
            for slot, tex_index in enumerate(mat["textures"]):
                if tex_index >= len(ids):
                    continue
                name, texel = self.texture(ids[tex_index])
                if name is None:
                    continue
                self.stats["records"] += 1
                record = {"name": name, "texel": texel, "slot": slot, "flags": mat["flags"],
                          "blend": mat["blend"], "mat": index, "src": source, "group": False,
                          "stages": mat["stages"], "texgens": mat["texgens"]}
                if sky:
                    record["sky"] = True
                yield record

    def scan(self):
        """Areas first, so their sky overrides are known before the models are read."""
        self.load_worlds()
        order = {"MREA": 0, "CMDL": 1}
        ids = sorted(self.where.items(), key=lambda kv: (order.get(kv[1][1], 2), kv[0]))
        total = sum(1 for _rid, (_p, t) in ids if t in ("CMDL", "MREA"))
        done = 0
        for rid, (pak, rtype) in ids:
            if rtype not in ("CMDL", "MREA"):
                continue
            done += 1
            if done % 100 == 0:
                self.log("  %d of %d models and areas" % (done, total))
            try:
                if rtype == "CMDL":
                    sets = cmdl_material_sets(self.cmdl_bytes(pak, rid))
                    self.stats["cmdl"] += 1
                    sky = rid in self.sky
                    for n, section in enumerate(sets):
                        src = "CMDL 0x%08X" % rid + (" set %d" % n if len(sets) > 1 else "")
                        yield from self.records_for_set(section, src + (" (sky)" if sky else ""), sky)
                else:
                    mrea = pak.get(rid)
                    try:
                        self.sky |= area_sky_models(mrea)
                    except (struct.error, ValueError, IndexError, AssertionError) as e:
                        self.log("  no script layers read for MREA 0x%08X: %s" % (rid, e))
                    section = mrea_material_set(mrea)
                    self.stats["mrea"] += 1
                    name = self.area_names.get(rid)
                    src = "area %s (MREA 0x%08X)" % (name, rid) if name else "area MREA 0x%08X" % rid
                    yield from self.records_for_set(section, src)
            except (struct.error, ValueError, IndexError, AssertionError) as e:
                self.log("  skipped %s 0x%08X: %s" % (rtype, rid, e))


GC_MAGIC, WII_MAGIC = 0xC2339F3D, 0x5D1C9EA3  # disc header words at 0x1C and 0x18
CONTAINERS = {b"RVZ\x01": "RVZ", b"WIA\x01": "WIA", b"\x01\xC0\x0B\xB1": "GCZ"}


def disc_problem(path):
    """Why `path` isn't a GameCube image this reader can open (a plain ISO/GCM, or a CISO), or None."""
    with open(path, "rb") as f:
        kind = CONTAINERS.get(f.read(4))
    if kind:
        return ("it is a compressed %s image; this reader takes a plain ISO or a CISO. In Dolphin: "
                "right-click the game > Convert File... > Format: ISO" % kind)
    try:
        disc = ed.open_disc(path)
        head = disc.read(0x20)
    except ValueError as e:
        return str(e)
    if len(head) < 0x20:
        return "the file is too short to be a disc image"
    if struct.unpack_from(">I", head, 0x18)[0] == WII_MAGIC:
        return ("it is a Wii disc (Metroid Prime Trilogy), whose data is encrypted; this reader reads "
                "the GameCube Echoes. Use MP2_TEXTURE_MANIFEST=1 in the game instead")
    if struct.unpack_from(">I", head, 0x1C)[0] != GC_MAGIC:
        return "it has no GameCube disc header"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("disc", help="the Echoes disc image (.iso/.gcm/.ciso)")
    parser.add_argument("-o", "--out", default="texture_manifest_disc.jsonl",
                        help="the manifest to write (default: texture_manifest_disc.jsonl)")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.disc):
        parser.error("no disc image at %s" % args.disc)
    problem = disc_problem(args.disc)
    if problem:
        parser.error("can't read %s: %s" % (args.disc, problem))
    if _xxhash is None:
        print("note: the xxhash module isn't installed; hashing in pure Python (slower). "
              "`pip install xxhash` speeds it up.")
    iso = ed.Iso(args.disc)
    paks = ed.load_paks(iso)
    print("%d PAKs on the disc" % len(paks))
    scanner = Scanner(paks, log=print)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for record in scanner.scan():
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    s = scanner.stats
    print("%d models and %d areas, %d materials (%d unreadable), %d records; %d textures named, "
          "%d missing, %d unhashable" % (s["cmdl"], s["mrea"], s["materials"], s["bad_materials"],
                                         s["records"], sum(1 for n, _t in scanner.names.values() if n),
                                         s["missing_txtr"], s["unhashable_txtr"]))
    print("%d sky models (world defaults and area overrides)" % len(scanner.sky))
    print("Wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
