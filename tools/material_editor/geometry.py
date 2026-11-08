"""Model geometry for the Material Editor's preview: a CMDL's or an area's world models as triangles
grouped by material, with positions, normals and the UVs of each material's base texture, and what
the preview needs of each material (Look). Standard library only.

Layouts follow the engine (the reference is the code, not this file):
  CMDL     DolphinCModel.cpp CModel::CModel: material sets, then positions (f32 x3), normals,
           colours, float UVs (f32 x2), short UVs when header flags bit 2 is set, the surface count,
           then one section per surface.
  MREA     CGameArea::PostConstructArea / FillInStaticGeometry: from the geometry section (header
           word 17) the material set, then per world model (header word 14 of them) its header
           (visor flags, a 3x4 transform, bounds; CMetroidModelInstance), positions, normals,
           colours, float UVs, packed UVs, the surface count, the surfaces and, when there are any,
           two more sections (surface records and groups).
  surface  CCubeSurface: a 0x30-byte header (material index at 0xC, display list size at 0x10 with
           the top bit a hint, extra size at 0x1C), the extra (the bounds), padded to
           (0x4F + extra) & ~31 bytes in all (DolphinCModel.cpp), then the display list.
  vertices CGX::SetVtxDescv_Compressed: the material's vertex description holds 2 bits per
           attribute for POS, NRM, CLR0, CLR1, TEX0..TEX6 (GXAttrType: none, direct, index8,
           index16) and bits 24..31 the direct matrix indices (PNMTXIDX..TEX6MTXIDX, a byte each,
           first in the vertex). Formats (DolphinCGraphics.cpp): VTXFMT0 normals f32, VTXFMT1 and 2
           normals s16 with 14 fraction bits. TEX0 reads the packed UVs (u16 pairs, 15 fraction
           bits) when the material has kStateFlag_LightmapUvArray (CCubeMaterial::SetCurrent,
           CCubeModel::SetUsingPackedLightmaps), the float UVs otherwise, as the other UVs do.
  material CCubeMaterial::SetCurrent: flags, textures, vertex description, two words, the group,
           konst colours (kStateFlag_KonstValues), the blend word (source factor low 16 bits,
           destination high; SetupBlendMode), then what disc_texture_scan.parse_material reads.
  base     the texture a material's PBR maps would relight: the first TEV stage that is
           lerp(0, L, TEXC) with L lit (port_pbr_rules.h ChooseStage), its texmap (= the material's
           texture slot) and texcoord, whose texgen reads TEXn (source GX_TG_TEX0 + n).
"""

import struct
from array import array

import material_core as mc

GX_PRIMS = {0x80: "quads", 0x90: "triangles", 0x98: "strip", 0xA0: "fan", 0xA8: "lines", 0xB0: "linestrip",
            0xB8: "points"}
ATTRS = ["POS", "NRM", "CLR0", "CLR1"] + ["TEX%d" % i for i in range(7)]
# GXTevColorArg (dolphin/gx/GXEnum.h)
CC_CPREV, CC_C0, CC_C1, CC_C2, CC_TEXC, CC_TEXA, CC_RASC, CC_RASA, CC_ONE, CC_HALF, CC_KONST, CC_ZERO = (
    0, 2, 4, 6, 8, 9, 10, 11, 12, 13, 14, 15)
GX_TG_NRM, GX_TG_TEX0 = 1, 4
GX_BL_ZERO, GX_BL_ONE, GX_BL_SRCALPHA, GX_BL_INVSRCALPHA = 0, 1, 4, 5
# CCubeMaterial.hpp
FLAG_KONST, FLAG_DEPTH_SORTING, FLAG_ALPHA_TEST, FLAG_LIGHTMAP_UV_ARRAY = 1 << 3, 1 << 4, 1 << 5, 1 << 13


class Surface:
    def __init__(self, material_index, display_list):
        self.material_index = material_index
        self.display_list = display_list


class GeoModel:
    """One model's vertex arrays (raw bytes) and surfaces, with its transform (rows of 3x4; None for
    the identity)."""

    def __init__(self, positions, normals, uvs, packed_uvs, surfaces, transform=None):
        self.positions = positions
        self.normals = normals
        self.uvs = uvs
        self.packed_uvs = packed_uvs
        self.surfaces = surfaces
        identity = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0)
        if transform is not None and all(abs(a - b) < 1e-6 for a, b in zip(transform, identity)):
            transform = None
        self.transform = transform


def _surface(section):
    if len(section) < 0x30:  # DolphinCModel.cpp's check: the header without the bounds
        raise ValueError("surface header short")
    (material_index, dl_size) = struct.unpack_from(">II", section, 0xC)
    (extra,) = struct.unpack_from(">I", section, 0x1C)
    start = (0x4F + extra) & ~31
    size = dl_size & 0x7FFFFFFF
    return Surface(material_index, section[start:start + size])


def _sections(data, start, sizes):
    out, pos = [], start
    for size in sizes:
        out.append(data[pos:pos + size])
        pos += size
    return out


def cmdl_geometry(cmdl):
    """([material set sections], GeoModel) of a whole CMDL."""
    (flags,) = struct.unpack_from(">I", cmdl, 8)
    section_count, set_count = struct.unpack_from(">II", cmdl, 0x24)
    sizes = struct.unpack_from(">%dI" % section_count, cmdl, 0x2C)
    secs = _sections(cmdl, (0x2C + section_count * 4 + 31) & ~31, sizes)
    sets = secs[:set_count]
    i = set_count
    if len(secs) < i + 5:
        raise ValueError("CMDL sections short")
    positions, normals, _colors, uvs = secs[i:i + 4]
    i += 4
    packed = None
    if flags & 4:
        packed = secs[i]
        i += 1
    (surface_count,) = struct.unpack_from(">I", secs[i], 0)
    i += 1
    surfaces = [_surface(secs[i + n]) for n in range(min(surface_count, len(secs) - i))]
    return sets, GeoModel(positions, normals, uvs, packed, surfaces)


class MreaSections:
    """The sections of a version-0x19 MREA, each block inflated when a section in it is first asked
    for (as disc_texture_scan.mrea_section reads one), so the scripts and collision after the
    geometry stay packed."""

    def __init__(self, mrea):
        h = struct.unpack_from(">32I", mrea, 0)
        if h[0] != 0xDEADBEEF:
            raise ValueError("not an MREA")
        nsec, nblk = h[16], h[28]
        pos = 0x80
        self.mrea = mrea
        self.sizes = struct.unpack_from(">%dI" % nsec, mrea, pos)
        pos += (nsec * 4 + 31) & ~31
        heads = [struct.unpack_from(">4I", mrea, pos + 16 * i) for i in range(nblk)]
        pos += (nblk * 16 + 31) & ~31
        self.blocks = []  # (first section, section count, file offset, padded size, size, packed size)
        si = 0
        for buf, dl, cl, sc in heads:
            rd = ((cl or buf) + 31) & ~31
            self.blocks.append((si, sc, pos, rd, dl, cl))
            pos += rd
            si += sc
        self._data = {}

    def __len__(self):
        return len(self.sizes)

    def __getitem__(self, i):
        if not 0 <= i < len(self.sizes):
            raise IndexError(i)
        for n, (si, sc, pos, rd, dl, cl) in enumerate(self.blocks):
            if si <= i < si + sc:
                data = self._data.get(n)
                if data is None:
                    raw = self.mrea[pos:pos + rd]
                    data = mc.ed.inflate_chunks(raw[rd - cl:], dl) if cl else raw[:dl]
                    self._data[n] = data
                off = sum(self.sizes[si:i])
                return data[off:off + self.sizes[i]]
        raise IndexError(i)


def mrea_sections(mrea):
    return MreaSections(mrea)


def area_geometry(mrea):
    """(material set section, [GeoModel]) of an area's world geometry."""
    h = struct.unpack_from(">32I", mrea, 0)
    model_count, first = h[14], h[17]
    secs = mrea_sections(mrea)
    material_set = secs[first]
    i = first + 1
    models = []
    for _ in range(model_count):
        if i + 7 > len(secs):
            break
        header = secs[i]
        transform = struct.unpack_from(">12f", header, 4) if len(header) >= 52 else None
        positions, normals, _colors, uvs, packed = (secs[j] for j in range(i + 1, i + 6))
        (surface_count,) = struct.unpack_from(">I", secs[i + 6], 0)
        i += 7
        surfaces = [_surface(secs[i + n]) for n in range(min(surface_count, len(secs) - i))]
        i += surface_count
        if surface_count:  # the surface records and groups follow
            i += 2
            models.append(GeoModel(positions, normals, uvs, packed, surfaces, transform))
    return material_set, models


# ------------------------------------------------------------------------------------ the material
def _kcolor(sel, konst):
    """GXTevKColorSel's value as RGB (dolphin/gx/GXEnum.h: 0..7 the fractions 8/8..1/8, 0x0C..0x0F
    K0..K3, then K0..K3's R, G, B and A broadcast)."""
    if sel <= 7:
        v = (8 - sel) / 8.0
        return (v, v, v)
    if 0x0C <= sel <= 0x0F:
        k = konst[sel - 0x0C] if sel - 0x0C < len(konst) else (1.0, 1.0, 1.0, 1.0)
        return tuple(k[:3])
    if 0x10 <= sel <= 0x1F:
        channel, n = divmod(sel - 0x10, 4)
        k = konst[n] if n < len(konst) else (1.0, 1.0, 1.0, 1.0)
        return (k[channel],) * 3
    return (1.0, 1.0, 1.0)


def _args(color_in):
    return color_in & 0x1F, (color_in >> 5) & 0x1F, (color_in >> 10) & 0x1F, (color_in >> 15) & 0x1F


class Look:
    """What the preview draws a material with: its vertex description, the base texture's slot and
    UV set, the environment-mapped stage (slot, how it combines, its weight), alpha test and blend."""

    def __init__(self, data):
        (flags, tex_count) = struct.unpack_from(">II", data, 0)
        self.flags = flags
        pos = 8 + 4 * tex_count
        (self.vtx_desc,) = struct.unpack_from(">I", data, pos)
        pos += 4 + 8 + 4
        self.konst = []
        if flags & FLAG_KONST:
            (count,) = struct.unpack_from(">I", data, pos)
            for n in range(min(count, 4)):
                (c,) = struct.unpack_from(">I", data, pos + 4 + 4 * n)
                self.konst.append(tuple(((c >> s) & 0xFF) / 255.0 for s in (24, 16, 8, 0)))
            pos += 4 * (count + 1)
        (blend,) = struct.unpack_from(">I", data, pos)
        self.blend_src, self.blend_dst = blend & 0xFFFF, blend >> 16
        parsed = mc.ds.parse_material(data)
        self.stages = parsed["stages"]
        self.texgens = parsed["texgens"]
        self.alpha_test = bool(flags & FLAG_ALPHA_TEST)
        self.packed_tex0 = bool(flags & FLAG_LIGHTMAP_UV_ARRAY)
        if self.alpha_test:
            self.blend_src, self.blend_dst = GX_BL_ONE, GX_BL_ZERO
        self.base_slot, self.base_uv = self._base()
        self.env_slot, self.env_mix, self.env_weight = self._env()

    @property
    def blended(self):
        return (self.blend_src, self.blend_dst) != (GX_BL_ONE, GX_BL_ZERO)

    @property
    def additive(self):
        return self.blend_dst == GX_BL_ONE

    def _texgen_src(self, texcoord):
        return (self.texgens[texcoord] >> 4) & 0x1F if texcoord < len(self.texgens) else None

    def _uv_set(self, texcoord):
        src = self._texgen_src(texcoord)
        return src - GX_TG_TEX0 if src is not None and GX_TG_TEX0 <= src < GX_TG_TEX0 + 7 else None

    def _base(self):
        for st in self.stages:
            order = st[0]
            a, b, c, d = _args(st[1])
            if a != CC_ZERO or d != CC_ZERO:
                continue
            light = c if b == CC_TEXC else (b if c == CC_TEXC else CC_ZERO)
            if light in (CC_RASC, CC_C0, CC_C1, CC_C2):
                return (order >> 8) & 0xFF, self._uv_set(order & 0xFF)
        # No lit stage (a glow, a decal): the first stage sampling a texture through UVs.
        for st in self.stages:
            order = st[0]
            texmap, texcoord = (order >> 8) & 0xFF, order & 0xFF
            uv = self._uv_set(texcoord) if texmap != 0xFF else None
            if uv is not None:
                return texmap, uv
        return None, None

    def _env(self):
        """(slot, mixes rather than adds, RGB weight) of the first stage sampling a texture through
        a texgen from the normal (an environment map), or (None, False, None)."""
        for st in self.stages:
            order, kcsel = st[0], st[5]
            texmap, texcoord = (order >> 8) & 0xFF, order & 0xFF
            if texmap == 0xFF or self._texgen_src(texcoord) != GX_TG_NRM:
                continue
            a, b, c, d = _args(st[1])
            mixes = a == CC_CPREV and b == CC_TEXC and d == CC_ZERO
            factor = c if (b == CC_TEXC or mixes) else (b if c == CC_TEXC else None)
            if factor == CC_KONST:
                weight = _kcolor(kcsel, self.konst)
            elif factor == CC_ONE:
                weight = (1.0, 1.0, 1.0)
            elif factor == CC_ZERO:
                weight = (0.0, 0.0, 0.0)
            else:
                weight = (0.5, 0.5, 0.5)
            return texmap, mixes, tuple(weight)
        return None, False, None


# ------------------------------------------------------------------------------------- triangles
def _vertex_reader(vtx_desc):
    """(matrix index bytes, [(attribute, type)]) of a vertex description."""
    mtx = sum(1 for bit in range(24, 32) if vtx_desc & (1 << bit))
    attrs = []
    for idx, name in enumerate(ATTRS):
        kind = (vtx_desc >> (2 * idx)) & 3
        if kind:
            attrs.append((name, kind))
    return mtx, attrs


def decode_display_list(dl, vtx_desc):
    """[(vertex format, [vertex {attr: index}])] of triangle lists: strips and fans unrolled, quads
    split, lines and points dropped. Raises ValueError for direct attribute data."""
    mtx, attrs = _vertex_reader(vtx_desc)
    if any(kind == 1 for _n, kind in attrs):
        raise ValueError("direct vertex data")
    out = []
    pos = 0
    n = len(dl)
    while pos + 3 <= n:
        op = dl[pos]
        if op == 0:
            pos += 1
            continue
        prim, fmt = op & 0xF8, op & 7
        if prim not in GX_PRIMS:
            break
        (count,) = struct.unpack_from(">H", dl, pos + 1)
        pos += 3
        verts = []
        for _ in range(count):
            pos += mtx
            v = {}
            for name, kind in attrs:
                if kind == 2:
                    v[name] = dl[pos] if pos < n else 0xFFFF
                    pos += 1
                else:
                    v[name] = (dl[pos] << 8 | dl[pos + 1]) if pos + 1 < n else 0xFFFF
                    pos += 2
            verts.append(v)
        if pos > n:
            break
        kind = GX_PRIMS[prim]
        tris = []
        if kind == "triangles":
            tris = verts[:len(verts) - len(verts) % 3]
        elif kind == "strip":
            for i in range(len(verts) - 2):
                a, b, c = verts[i], verts[i + 1], verts[i + 2]
                tris += [a, b, c] if i % 2 == 0 else [b, a, c]
        elif kind == "fan":
            for i in range(1, len(verts) - 1):
                tris += [verts[0], verts[i], verts[i + 1]]
        elif kind == "quads":
            for i in range(0, len(verts) - 3, 4):
                q = verts[i:i + 4]
                tris += [q[0], q[1], q[2], q[0], q[2], q[3]]
        if tris:
            out.append((fmt, tris))
    return out


class Batch:
    """A material's triangles: flat arrays of positions (x3), normals (x3) and base UVs (x2)."""

    def __init__(self, material_index):
        self.material_index = material_index
        self.positions = array("f")
        self.normals = array("f")
        self.uvs = array("f")

    @property
    def count(self):
        return len(self.positions) // 3


def _apply(t, x, y, z, w):
    return (t[0] * x + t[1] * y + t[2] * z + t[3] * w, t[4] * x + t[5] * y + t[6] * z + t[7] * w,
            t[8] * x + t[9] * y + t[10] * z + t[11] * w)


def _unpack(fmt_char, data, width):
    """The big-endian array `data` as a flat tuple (whole elements of `width` values only)."""
    if not data:
        return ()
    size = struct.calcsize(">" + fmt_char)
    n = len(data) // (size * width) * width
    return struct.unpack_from(">%d%s" % (n, fmt_char), data, 0)


def build_batches(models, materials):
    """({material index: Batch}, {material index: Look}) of every model's surfaces; `materials` the
    set's Material objects. A triangle with an index outside its array is dropped."""
    batches = {}
    looks = {}
    for model in models:
        pos = _unpack("f", model.positions, 3)
        nrm_f = nrm_s = None
        uvs = _unpack("f", model.uvs, 2)
        packed = _unpack("H", model.packed_uvs, 2)
        npos = len(pos) // 3
        t = model.transform
        for surface in model.surfaces:
            mi = surface.material_index
            if mi >= len(materials):
                continue
            if mi not in looks:
                try:
                    looks[mi] = Look(materials[mi].data)
                except (struct.error, ValueError):
                    looks[mi] = None
            look = looks[mi]
            if look is None:
                continue
            try:
                prims = decode_display_list(surface.display_list, look.vtx_desc)
            except (struct.error, ValueError):
                continue
            batch = batches.setdefault(mi, Batch(mi))
            uv_name = "TEX%d" % look.base_uv if look.base_uv is not None else None
            use_packed = uv_name == "TEX0" and look.packed_tex0 and bool(packed)
            uv_src, uv_scale = (packed, 1.0 / 32768.0) if use_packed else (uvs, 1.0)
            nuv = len(uv_src) // 2
            out_p, out_n, out_t = batch.positions, batch.normals, batch.uvs
            for fmt, tris in prims:
                if fmt in (1, 2):
                    if nrm_s is None:
                        nrm_s = tuple(c / 16384.0 for c in _unpack("h", model.normals, 3))
                    nrm = nrm_s
                else:
                    if nrm_f is None:
                        nrm_f = _unpack("f", model.normals, 3)
                    nrm = nrm_f
                nnrm = len(nrm) // 3
                for k in range(0, len(tris), 3):
                    tri = tris[k:k + 3]
                    if any(v.get("POS", npos) >= npos for v in tri):
                        continue
                    for v in tri:
                        p = 3 * v["POS"]
                        if t is None:
                            out_p.extend(pos[p:p + 3])
                        else:
                            out_p.extend(_apply(t, pos[p], pos[p + 1], pos[p + 2], 1.0))
                        ni = v.get("NRM")
                        if ni is not None and ni < nnrm:
                            q = 3 * ni
                            if t is None:
                                out_n.extend(nrm[q:q + 3])
                            else:
                                out_n.extend(_apply(t, nrm[q], nrm[q + 1], nrm[q + 2], 0.0))
                        else:
                            out_n.extend((0.0, 0.0, 1.0))
                        ui = v.get(uv_name) if uv_name else None
                        if ui is not None and ui < nuv:
                            out_t.append(uv_src[2 * ui] * uv_scale)
                            out_t.append(uv_src[2 * ui + 1] * uv_scale)
                        else:
                            out_t.extend((0.0, 0.0))
    return {k: b for k, b in batches.items() if b.count}, looks


def bounds(batches, only=None):
    """(min xyz, max xyz) of the batches (those in `only`, when given), or None."""
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for key, b in batches.items():
        if only is not None and key not in only:
            continue
        p = b.positions
        for k in range(3):
            axis = p[k::3]
            if axis:
                lo[k] = min(lo[k], min(axis))
                hi[k] = max(hi[k], max(axis))
    return (lo, hi) if lo[0] <= hi[0] else None
