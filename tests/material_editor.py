"""tools/material_editor/material_core.py: the key (as the game computes it), paths and settings, the
disc's worlds/areas/models and their materials, texture decoding, PNG, materials.json. And the 3D
preview's data (geometry.py, preview.py): CMDL and MREA geometry, display lists, each material's base
texture and environment stage, and the settings as the game applies them.

Usage: material_editor.py <repo root>. Standard library only; no disc needed: the disc, PAK, MLVL,
STRG, MREA, CMDL and TXTR are built here (with tests/pbr_disc_scan.py's builders).
"""
import json
import os
import pathlib
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
sys.argv = sys.argv[:1]
sys.path.insert(0, str(ROOT / "tools" / "material_editor"))
sys.path.insert(0, str(ROOT / "tests"))
import material_core as mc  # noqa: E402
import geometry  # noqa: E402
import preview  # noqa: E402
import pbr_disc_scan as builders  # noqa: E402

be = builders.be


def fourcc(text):
    return struct.unpack(">I", text.encode())[0]


def pak(resources, names=()):
    """A version-0x30005 PAK: `resources` [(type, id, data)], stored uncompressed, 32-byte aligned."""
    head = be(0x30005, 0, len(names))
    for t, rid, name in names:
        raw = name.encode() + b"\0"
        head += be(fourcc(t), rid, len(raw)) + raw
    head += be(len(resources))
    table_at = len(head)
    head += b"\0" * (20 * len(resources))
    head += b"\0" * (-len(head) % 32)
    body, entries, pos = b"", b"", len(head)
    for t, rid, data in resources:
        entries += be(0, fourcc(t), rid, len(data), pos)
        padded = data + b"\0" * (-len(data) % 32)
        body += padded
        pos += len(padded)
    head = head[:table_at] + entries + head[table_at + len(entries):]
    return head + body


def strg(text):
    """A one-language, one-string version-1 STRG (echoes_disc.strg_strings)."""
    return be(0x87654321, 1, 1, 1) + be(fourcc("ENGL"), 0, 0) + be(0, 0) + be(4) + text.encode("utf-16-be") + b"\0\0"


def txtr_i8(width, height, mips=1):
    """An I8 TXTR (texel 1): each level's texel at (x, y) is (x * 16 + y) & 0xFF, in 8x4 tiles."""
    out = be(1) + struct.pack(">HH", width, height) + be(mips)
    w, h = width, height
    for _ in range(mips):
        level = bytearray(((w + 7) // 8 * 8) * ((h + 3) // 4 * 4))
        pos = 0
        for ty in range((h + 3) // 4):
            for tx in range((w + 7) // 8):
                for i in range(32):
                    x, y = tx * 8 + i % 8, ty * 4 + i // 8
                    level[pos] = (x * 16 + y) & 0xFF
                    pos += 1
        out += bytes(level)
        w, h = max(w // 2, 1), max(h // 2, 1)
    return out


STAGE = (builders.ARGS(15, 8, 15, 15), 0, 0, 0, 0, 0)  # colour = TEXC


class Key(unittest.TestCase):
    def test_same_as_the_game(self):
        # tests/port_material_overrides.cpp's vector.
        material = be(0x10, 2, 1, 5) + bytes(range(12))
        ids = [0x11111111, 0x22222222, 0x33333333]
        self.assertEqual(mc.key_text(mc.material_key(material, ids)), "447d14554c8ab0f3")

    def test_textures_and_bytes_matter(self):
        material = be(0x10, 1, 0) + bytes(8)
        base = mc.material_key(material, [1])
        self.assertNotEqual(base, mc.material_key(material, [2]))
        self.assertNotEqual(base, mc.material_key(material[:-1] + b"\x01", [1]))
        self.assertEqual(base, mc.material_key(material, [1, 99]))  # ids it doesn't list don't count


class Paths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = {k: os.environ.get(k) for k in ("MP2_USER_PATH", "MP2_MODS", "MP2_DISC", "APPDATA",
                                                   "XDG_DATA_HOME", "HOME", "MP2_USER_TEXTURES")}
        for k in self.env:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.dir / "home")
        os.environ["XDG_DATA_HOME"] = str(self.dir / "data")
        os.environ["APPDATA"] = str(self.dir / "data")

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_user_folder_and_settings(self):
        game = self.dir / "game"
        (game / "user").mkdir(parents=True)
        self.assertEqual(mc.find_user_folder(str(game)), str(game / "user"))
        # An older install with only the per-user folder's settings stays there.
        per_user = pathlib.Path(mc.per_user_folder())
        per_user.mkdir(parents=True)
        (per_user / "settings.cfg").write_text("disc_path=/x.iso\n")
        self.assertEqual(mc.find_user_folder(str(game)), str(per_user))
        (game / "user" / "settings.cfg").write_text("# comment\ndisc_path=C:\\Games\\Echoes.iso\nmods=0\n"
                                                     "mods_disabled=A/B\n")
        user = mc.find_user_folder(str(game))
        self.assertEqual(user, str(game / "user"))
        settings = mc.read_settings(user)
        self.assertEqual(settings["disc_path"], "C:\\Games\\Echoes.iso")
        self.assertFalse(mc.setting_bool(settings.get("mods"), True))
        self.assertTrue(mc.setting_bool(None, True))
        self.assertEqual(mc.mods_folder(user), os.path.join(user, "mods"))
        os.environ["MP2_USER_PATH"] = "/elsewhere"
        self.assertEqual(mc.find_user_folder(str(game)), "/elsewhere")
        os.environ["MP2_MODS"] = "/mods"
        self.assertEqual(mc.mods_folder(user), "/mods")

    def test_disc_beside_the_game(self):
        game = self.dir / "game"
        game.mkdir()
        (game / "Echoes.ciso").write_bytes(b"CISO")
        self.assertEqual(mc.find_disc({}, str(game)), str(game / "Echoes.ciso"))
        real = self.dir / "real.iso"
        real.write_bytes(b"x")
        self.assertEqual(mc.find_disc({"disc_path": str(real)}, str(game)), str(real))
        self.assertEqual(mc.find_disc({"disc_path": str(self.dir / "gone.iso")}, str(game)),
                         str(game / "Echoes.ciso"))


class Decode(unittest.TestCase):
    def test_i8_and_mip_choice(self):
        tex = txtr_i8(16, 8, mips=2)
        w, h, rgba = mc.decode_txtr(tex, max_size=16)
        self.assertEqual((w, h), (16, 8))
        for x, y in ((0, 0), (9, 3), (15, 7), (3, 5)):
            o = (y * 16 + x) * 4
            v = (x * 16 + y) & 0xFF
            self.assertEqual(tuple(rgba[o:o + 4]), (v, v, v, 255), (x, y))
        w, h, rgba = mc.decode_txtr(tex, max_size=8)
        self.assertEqual((w, h), (8, 4))
        self.assertEqual(tuple(rgba[(3 * 8 + 5) * 4:(3 * 8 + 5) * 4 + 4]), ((5 * 16 + 3),) * 3 + (255,))

    def test_rgb565_rgb5a3_ia8_rgba8(self):
        # One 4x4 tile each.
        rgb565 = struct.pack(">16H", *([0xF800] + [0x07E0] * 15))
        out = mc.decode_gx(rgb565, 4, 4, 0x4)
        self.assertEqual(tuple(out[0:4]), (255, 0, 0, 255))
        self.assertEqual(tuple(out[4:8]), (0, 255, 0, 255))
        rgb5a3 = struct.pack(">16H", *([0x801F] + [0x3F00] * 15))  # opaque blue; alpha 3/7, red 15
        out = mc.decode_gx(rgb5a3, 4, 4, 0x5)
        self.assertEqual(tuple(out[0:4]), (0, 0, 255, 255))
        self.assertEqual(tuple(out[4:8]), (255, 0, 0, 3 * 255 // 7))
        ia8 = bytes([0x80, 0x40] * 16)
        self.assertEqual(tuple(mc.decode_gx(ia8, 4, 4, 0x3)[0:4]), (0x40, 0x40, 0x40, 0x80))
        rgba8 = bytes([0xFF, 0x10] * 16 + [0x20, 0x30] * 16)  # A R pairs, then G B pairs
        self.assertEqual(tuple(mc.decode_gx(rgba8, 4, 4, 0x6)[0:4]), (0x10, 0x20, 0x30, 0xFF))

    def test_cmpr(self):
        # Four DXT1 blocks: red/blue ends, every texel index 0 except the first texel's 1.
        block = struct.pack(">HHI", 0xF800, 0x001F, 0x40000000)
        out = mc.decode_gx(block * 4, 8, 8, 0xE)
        self.assertEqual(tuple(out[0:4]), (0, 0, 255, 255))  # index 1: colour 1, blue
        self.assertEqual(tuple(out[4:8]), (255, 0, 0, 255))
        # Sub-block 1 sits at x 4.
        self.assertEqual(tuple(out[16:20]), (0, 0, 255, 255))
        # c0 <= c1: index 3 is transparent.
        clear = struct.pack(">HHI", 0x001F, 0xF800, 0xFFFFFFFF)
        self.assertEqual(tuple(mc.decode_gx(clear * 4, 8, 8, 0xE)[0:4]), (0, 0, 0, 0))

    def test_c8_palette(self):
        # A C8 TXTR (texel 5) with an RGB565 palette of 2 entries.
        tex = be(5) + struct.pack(">HH", 8, 4) + be(1) + be(1) + struct.pack(">HH", 2, 1)
        tex += struct.pack(">2H", 0xF800, 0x001F) + bytes([1] + [0] * 31)
        w, h, rgba = mc.decode_txtr(tex)
        self.assertEqual(tuple(rgba[0:4]), (0, 0, 255, 255))
        self.assertEqual(tuple(rgba[4:8]), (255, 0, 0, 255))

    def test_png(self):
        png = mc.png_bytes(2, 1, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (2, 1))
        idat = png.index(b"IDAT")
        (length,) = struct.unpack(">I", png[idat - 4:idat])
        self.assertEqual(zlib.decompress(png[idat + 4:idat + 4 + length]), b"\0" + bytes(range(1, 9)))


class Disc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        m0 = builders.material(0, [0], 0, [STAGE], [0x10])            # a texgen from the normal: reflective
        m1 = builders.material(0, [1, 0], 0, [STAGE, STAGE], [0])
        self.area_set = builders.material_set([0x7001, 0x7002], [m0, m1])
        model_set = builders.material_set([0x7002], [builders.material(0, [0], 0, [STAGE], [0])])
        self.cmdl, _ = builders.cmdl([model_set])
        layer = builders.scly([])
        world = builders.mlvl(0xFFFFFFFF, [0x5001, 0x5002])
        world = world[:8] + be(0x6001) + world[12:]  # the world's name STRG
        # Area 0's name STRG (0x20 is what the builder writes for area 0).
        resources = [("MLVL", 0x4001, world), ("STRG", 0x6001, strg("Torvus Bog")),
                     ("STRG", 0x20, strg("Torvus Lagoon")),
                     ("MREA", 0x5001, builders.mrea(self.area_set, layer)),
                     ("MREA", 0x5002, builders.mrea(self.area_set, layer)),
                     ("CMDL", 0x3001, self.cmdl), ("TXTR", 0x7001, txtr_i8(16, 8)),
                     ("TXTR", 0x7002, txtr_i8(8, 4))]
        image = builders.gc_disc({"Metroid4.pak": pak(resources, [("CMDL", 0x3001, "Rock")])})
        self.path = self.dir / "echoes.iso"
        self.path.write_bytes(image)

    def tearDown(self):
        self.tmp.cleanup()

    def test_worlds_areas_models_and_materials(self):
        lib = mc.Library(str(self.path))
        self.assertEqual(len(lib.worlds), 1)
        world = lib.worlds[0]
        self.assertEqual(world["name"], "Torvus Bog")
        self.assertEqual([a["name"] for a in world["areas"]], ["Torvus Lagoon", "area_1"])
        mats = lib.area_materials(0x5001)
        self.assertEqual(len(mats), 2)
        ids, datas = builders.ds.parse_material_set(self.area_set)
        self.assertEqual(mats[0].key, mc.material_key(datas[0], ids))
        self.assertEqual(mats[0].textures, [0x7001])
        self.assertEqual(mats[1].textures, [0x7002, 0x7001])
        self.assertTrue(mats[0].reflective)
        self.assertFalse(mats[1].reflective)
        self.assertEqual(lib.models, [0x3001])
        self.assertEqual(lib.model_name(0x3001), "Rock (CMDL 00003001)")
        sets = lib.model_materials(0x3001)
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0][0].textures, [0x7002])
        # Identical bytes and textures: the same key in the area as in the model's set? Not here
        # (the model's material lists texture index 0 = 0x7002, the area's index 1 = 0x7002, with
        # other bytes), so they differ.
        self.assertNotEqual(sets[0][0].key, mats[1].key)
        w, h, rgba = mc.decode_txtr(lib.texture(0x7001))
        self.assertEqual((w, h), (16, 8))
        self.assertTrue(lib.texture_pack_name(0x7001).startswith("tex1_16x8_"))
        self.assertIsNone(lib.texture(0x9999))
        lib.close()

    def test_rvz_refused(self):
        rvz = self.dir / "echoes.rvz"
        rvz.write_bytes(b"RVZ\x01" + bytes(64))
        with self.assertRaises(ValueError):
            mc.Library(str(rvz))


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_clamping(self):
        path = mc.overrides_path(str(self.dir / "mods"))
        self.assertTrue(path.endswith(os.path.join("Material Editor", "materials.json")))
        o = mc.Overrides(path)
        self.assertEqual(o.materials, {})
        o.set("447d14554c8ab0f3", {"pbr": False, "roughness": 9, "occlusion": 0.5, "note": "rock",
                                   "bogus": 1, "metal": None})
        o.set("00000000000000aa", {"highlight": 2})
        o.set("00000000000000aa", {})  # nothing left: removed
        o.save()
        doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["materials"], {"447d14554c8ab0f3": {"pbr": False, "roughness": 4.0,
                                                                  "occlusion": 0.5, "note": "rock"}})
        # Other top-level keys a hand edit added are kept.
        doc["author"] = "me"
        pathlib.Path(path).write_text(json.dumps(doc), encoding="utf-8")
        o2 = mc.Overrides(path)
        o2.set("447d14554c8ab0f3", dict(o2.get("447d14554c8ab0f3"), reflection=True))
        o2.save()
        doc2 = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.assertEqual(doc2["author"], "me")
        self.assertTrue(doc2["materials"]["447d14554c8ab0f3"]["reflection"])
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_bad_file(self):
        path = self.dir / "materials.json"
        path.write_text("{nope", encoding="utf-8")
        o = mc.Overrides(str(path))
        self.assertIsNotNone(o.problem)

    def test_pack_names_as_aurora_reads_them(self):
        key = (0xaa, 0, 8, 4, 1, False)
        for name in ("tex1_8x4_00000000000000aa_1.dds", "tex1_8x4_m_00000000000000aa_1.png",
                     "tex1_8x4_00000000000000AA_I8.dds", "tex1_8x4_00000000000000aa_1_arb.dds",
                     "tex1_8x4_aa_1.DDS"):
            self.assertEqual(mc.parse_pack_name(name), key, name)
        self.assertEqual(mc.parse_pack_name("tex1_8x4_$_14.dds"), (mc.WILDCARD_TEXTURE, 0, 8, 4, 14, False))
        self.assertEqual(mc.parse_pack_name("tex1_8x4_00000000000000aa_$_9.dds"),
                         (0xaa, mc.WILDCARD_TLUT, 8, 4, 9, True))
        self.assertEqual(mc.parse_pack_name("tex1_8x4_m_00000000000000aa_00000000000000bb_9.png"),
                         (0xaa, 0xbb, 8, 4, 9, True))
        # Seven parts: more than Aurora's six (its parser stops there and refuses the name).
        self.assertIsNone(mc.parse_pack_name("tex1_8x4_m_00000000000000aa_00000000000000bb_9_arb.png"))
        for bad in ("tex1_8x4_00000000000000aa_1.jpg", "TEX1_8x4_aa_1.dds", "tex1_8x4_zz_1.dds",
                    "tex1_8by4_aa_1.dds", "tex1_8x4_aa_nrm.dds", "tex1_8x4_m_aa_bb_1_arb_x.dds", "tex1_8x4_aa.dds"):
            self.assertIsNone(mc.parse_pack_name(bad), bad)
        self.assertEqual(mc.split_map_name("tex1_8x4_m_aa_1_NRM.png"), ("tex1_8x4_m_aa_1.dds", "nrm"))
        self.assertIsNone(mc.split_map_name("tex1_8x4_aa_1.dds"))

    def test_pack_maps(self):
        pack = self.dir / "pack"
        (pack / "sub").mkdir(parents=True)
        # Dolphin's mipmapped name (_m), maps beside it in another case, a sidecar mip level.
        for name in ("tex1_8x4_m_00000000000000aa_1.dds", "tex1_8x4_m_00000000000000aa_1_NRM.png",
                     "tex1_8x4_m_00000000000000aa_1_orm.dds", "tex1_8x4_m_00000000000000aa_1_mip1.dds",
                     "tex1_16x16_$_00000000000000cc_9.png", "readme.txt"):
            (pack / "sub" / name).write_bytes(b"x")
        index = mc.TexturePack([str(pack)])
        tex, nrm, orm = index.find("tex1_8x4_00000000000000aa_1.dds")  # the editor's name has no _m
        self.assertTrue(tex.endswith("_1.dds") and nrm.endswith("_NRM.png") and orm.endswith("_orm.dds"))
        self.assertEqual(index.find("tex1_8x4_00000000000000bb_1.dds"), (None, None, None))
        # Any texels with that palette: the texture wildcard.
        self.assertTrue(index.find("tex1_16x16_0000000000000123_00000000000000cc_9.dds")[0].endswith("_9.png"))
        self.assertEqual(index.pairs, 1)
        self.assertEqual(len(index.textures), 2)
        # A pack with device folders is read from one of them (the keyboard's first), as the game does.
        dev = self.dir / "dev"
        (dev / "xbox").mkdir(parents=True)
        (dev / "keyboard").mkdir()
        (dev / "keyboard" / "tex1_8x4_00000000000000aa_1.dds").write_bytes(b"k")
        (dev / "xbox" / "tex1_8x4_00000000000000aa_1.dds").write_bytes(b"x")
        (dev / "tex1_8x4_00000000000000aa_1.dds").write_bytes(b"root")
        self.assertIn("keyboard", mc.TexturePack([str(dev)]).find("tex1_8x4_00000000000000aa_1.dds")[0])
        self.assertIn("xbox", mc.TexturePack([str(dev)], device="xbox").find("tex1_8x4_00000000000000aa_1.dds")[0])
        self.assertEqual(mc.TexturePack([str(dev)], device="switch").textures, {})
        # The user pack wins over a later folder, and texture_pack=0 leaves it out.
        first = mc.TexturePack([str(dev), str(pack)])
        self.assertIn("keyboard", first.find("tex1_8x4_00000000000000aa_1.dds")[0])
        self.assertEqual(mc.texture_pack_roots(str(self.dir), {"texture_pack": "0"}), [])
        self.assertEqual(mc.texture_pack_roots(str(self.dir), {}), [str(self.dir / "user_textures")])

    def test_fields_match_the_game(self):
        # The names port_material_overrides.h reads.
        header = (ROOT / "platform" / "include" / "port_material_overrides.h").read_text()
        for name in mc.FIELD_NAMES:
            self.assertIn('"%s"' % name, header, name)


# ------------------------------------------------------------------------------- the 3D preview
ARGS = builders.ARGS
POS8, POS16, NRM8, TEX0_8, TEX0_16, TEX1_8, PNMTX = 2, 3, 2 << 2, 2 << 8, 3 << 8, 2 << 10, 1 << 24
LIT = (ARGS(15, 10, 8, 15), 0, 0, 0, 0, 0 | 0 << 8)  # 0 + lerp(0, RASC, TEXC): texcoord 0, texmap 0


def with_vtx(data, vtx):
    """A builders.material with its vertex description set (the builder writes 0x11111111)."""
    (count,) = struct.unpack_from(">I", data, 4)
    at = 8 + 4 * count
    return data[:at] + be(vtx) + data[at + 4:]


def surface(material_index, dl, extra=0):
    """A surface section: the 0x30-byte header, `extra` bytes, padding to (0x4F + extra) & ~31, the
    display list."""
    head = struct.pack(">3f", 0, 0, 0) + be(material_index, len(dl) | 0x80000000, 0, 0, extra)
    head += struct.pack(">3f", 0, 0, 1) + struct.pack(">h", 0) + b"\0\0"
    head += b"\0" * extra
    head += b"\0" * (((0x4F + extra) & ~31) - len(head))
    return head + dl


def cmdl_with_geometry(flags, sets, arrays, surfaces):
    sections = list(sets) + list(arrays) + [be(len(surfaces))] + list(surfaces)
    padded = [x + b"\0" * (-len(x) % 32) for x in sections]
    head = b"\xDE\xAD\xBA\xBE" + be(2, flags) + b"\0" * 24 + be(len(padded), len(sets)) + be(*map(len, padded))
    head += b"\0" * (-len(head) % 32)
    return head + b"".join(padded)


def floats(*values):
    return struct.pack(">%df" % len(values), *values)


QUAD = floats(0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0)
UP = floats(*([0, 0, 1] * 4))
UVS = floats(0, 0, 0.25, 0.5, 0.5, 1.0, 0.75, 1.5)


def strip_dl():
    """A strip of the quad's 4 vertices: PNMTXIDX, POS u16, NRM u8, TEX0 u16 each."""
    return bytes([0x98]) + struct.pack(">H", 4) + b"".join(
        bytes([0]) + struct.pack(">H", i) + bytes([0]) + struct.pack(">H", i) for i in range(4))


def quads_dl():
    """One quad: POS u8, NRM u8, TEX1 u8 per vertex."""
    return bytes([0x80]) + struct.pack(">H", 4) + b"".join(bytes([i, 0, i]) for i in range(4))


def test_model():
    m0 = with_vtx(builders.material(0, [0], 0x00000001, [LIT], [4 << 4]), PNMTX | POS16 | NRM8 | TEX0_16)
    env = (ARGS(15, 8, 14, 0), 0, 0, 0, 0x0C << 8, 0 | 0 << 8)  # CPREV + TEXC x KONST(K0), from the normal
    m1 = with_vtx(builders.material(geometry.FLAG_KONST, [0, 1], 4 | 5 << 16,
                                    [(ARGS(15, 8, 2, 15), 0, 0, 0, 0, 1 | 1 << 8), env], [1 << 4, 5 << 4],
                                    konst=(0x80402000,)), POS8 | NRM8 | TEX1_8)
    mset = builders.material_set([0x7001, 0x7002], [m0, m1])
    return cmdl_with_geometry(0, [mset], [QUAD, UP, b"", UVS], [surface(0, strip_dl()), surface(1, quads_dl(), 0x18)])


class PreviewGeometry(unittest.TestCase):
    def test_cmdl_triangles_by_material(self):
        sets, model = geometry.cmdl_geometry(test_model())
        self.assertEqual(len(sets), 1)
        self.assertEqual([s.material_index for s in model.surfaces], [0, 1])
        ids, datas = builders.ds.parse_material_set(sets[0])
        materials = [mc.Material(i, d, ids) for i, d in enumerate(datas)]
        batches, looks = geometry.build_batches([model], materials)
        # The strip 0 1 2 3 is the triangles (0, 1, 2) and (2, 1, 3) (GX flips every other one).
        pos = list(batches[0].positions)
        corner = [QUAD[i * 12:i * 12 + 12] for i in range(4)]
        expect = b"".join(corner[i] for i in (0, 1, 2, 2, 1, 3))
        self.assertEqual(floats(*pos), expect)
        self.assertEqual(list(batches[0].uvs)[:6], [0, 0, 0.25, 0.5, 0.5, 1.0])
        self.assertEqual(list(batches[0].normals)[:3], [0, 0, 1])
        # The quad 0 1 2 3: (0, 1, 2) and (0, 2, 3), with TEX1's UVs (the base stage's texcoord 1).
        self.assertEqual(batches[1].count, 6)
        self.assertEqual(list(batches[1].uvs)[4:8], [0.5, 1.0, 0, 0])  # vertices 2, then 0 again
        self.assertEqual(list(batches[1].uvs)[10:12], [0.75, 1.5])
        look0, look1 = looks[0], looks[1]
        self.assertEqual((look0.base_slot, look0.base_uv), (0, 0))
        self.assertFalse(look0.blended)
        self.assertIsNone(look0.env_slot)
        self.assertEqual((look1.base_slot, look1.base_uv), (1, 1))
        self.assertEqual(look1.env_slot, 0)
        self.assertFalse(look1.env_mix)
        for got, want in zip(look1.env_weight, (128 / 255.0, 64 / 255.0, 32 / 255.0)):
            self.assertAlmostEqual(got, want)
        self.assertTrue(look1.blended)
        self.assertFalse(look1.additive)
        self.assertEqual(geometry.bounds(batches), ([0, 0, 0], [1, 1, 0]))

    def test_display_list_kinds(self):
        vtx = POS8
        tri = bytes([0x90]) + struct.pack(">H", 3) + bytes([0, 1, 2])
        fan = bytes([0xA0]) + struct.pack(">H", 4) + bytes([0, 1, 2, 3])
        lines = bytes([0xA8]) + struct.pack(">H", 2) + bytes([0, 1])
        prims = geometry.decode_display_list(tri + b"\0\0" + fan + lines, vtx)
        self.assertEqual([[v["POS"] for v in tris] for _f, tris in prims], [[0, 1, 2], [0, 1, 2, 0, 2, 3]])
        with self.assertRaises(ValueError):
            geometry.decode_display_list(tri, 1)  # direct positions: not in these models

    def test_area_world_models(self):
        m0 = with_vtx(builders.material(geometry.FLAG_ALPHA_TEST | geometry.FLAG_LIGHTMAP_UV_ARRAY, [0],
                                        4 | 5 << 16, [LIT], [4 << 4]), POS8 | NRM8 | TEX0_8)
        mset = builders.material_set([0x7001], [m0])
        transform = floats(1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0)
        header = be(0) + transform + floats(0, 0, 0, 1, 1, 1)
        normals = struct.pack(">9h", 0, 0, 16384, 0, 16384, 0, 16384, 0, 0)
        packed = struct.pack(">6H", 0, 0, 16384, 0, 0, 32768)
        dl = bytes([0x90 | 2]) + struct.pack(">H", 3) + bytes([0, 0, 0, 1, 1, 1, 2, 2, 2])
        model0 = [header, QUAD[:36], normals, b"", b"", packed, be(1), surface(0, dl), b"\0" * 4, b"\0" * 4]
        model1 = [header, b"", b"", b"", b"", b"", be(0)]  # no surfaces: no records or groups
        sections = [b"\x01" * 32, mset] + model0 + model1
        sections = [x + b"\0" * (-len(x) % 32) for x in sections] + [b"\x05" * 64]
        h = [0] * 32
        h[0], h[1], h[14], h[16], h[17], h[28] = 0xDEADBEEF, 0x19, 2, len(sections), 1, 2
        body = be(*h) + be(*map(len, sections))
        body += b"\0" * (-len(body) % 32)
        stored = sum(map(len, sections[:-1]))
        # The last block claims LZO data it hasn't got: read only if a section in it is asked for.
        body += be(stored, stored, 0, len(sections) - 1) + be(64, 64, 32, 1)
        body += b"\0" * (-len(body) % 32)
        body += b"".join(sections[:-1]) + b"\xFF" * 32
        material_section, models = geometry.area_geometry(body)
        self.assertEqual(material_section[:len(mset)], mset)
        self.assertEqual(len(models), 1)
        ids, datas = builders.ds.parse_material_set(mset)
        batches, looks = geometry.build_batches(models, [mc.Material(0, datas[0], ids)])
        self.assertEqual(list(batches[0].positions)[:6], [10, 0, 0, 11, 0, 0])  # moved by the transform
        self.assertEqual(list(batches[0].normals)[:6], [0, 0, 1, 0, 1, 0])      # s16, 14 fraction bits
        self.assertEqual(list(batches[0].uvs), [0, 0, 0.5, 0, 0, 1.0])          # packed, 15 fraction bits
        self.assertTrue(looks[0].alpha_test)
        self.assertFalse(looks[0].blended)  # alpha-tested: drawn opaque (SetupBlendMode)


def stage(a, b, c, d, texcoord=1, texmap=1, op=0x100):
    """A stage as parse_material gives it: [order, colour in, alpha in, colour op, alpha op, kc, ka]."""
    return [texcoord | texmap << 8, ARGS(a, b, c, d), 0, op, 0x100, 0, 0]


ZERO, ONE, KONST, TEXC, RASC, CPREV = 15, 12, 14, 8, 10, 0
UV_TEXGENS = [4 << 4, 5 << 4]  # TEX0, TEX1


class Glow(unittest.TestCase):
    """material_core.glow_stages: the same shapes and refusals as Aurora's port_emissive_stages
    (extern/aurora/tests/gx_surface_pbr_test.cpp GlowShapes, GlowRefusals)."""

    def glow(self, second, texgens=UV_TEXGENS, more=()):
        lit = stage(ZERO, TEXC, RASC, ZERO, texcoord=0, texmap=0)
        return mc.glow_stages([lit, second] + list(more), texgens)

    def test_shapes(self):
        self.assertEqual(self.glow(stage(ZERO, TEXC, ONE, CPREV)), [1])      # CPREV + TEXC (the disc's)
        self.assertEqual(self.glow(stage(TEXC, KONST, ZERO, CPREV)), [1])    # lerp(TEXC, K, 0) + CPREV
        self.assertEqual(self.glow(stage(CPREV, ZERO, ZERO, TEXC)), [1])     # TEXC + CPREV
        self.assertEqual(self.glow(stage(ZERO, KONST, TEXC, CPREV)), [1])    # CPREV + K x TEXC

    def test_refusals(self):
        self.assertEqual(self.glow(stage(ZERO, TEXC, RASC, CPREV)), [], "lit")
        self.assertEqual(self.glow(stage(ZERO, TEXC, ONE, CPREV, op=0x100 | 2 << 9)), [], "to a register")
        self.assertEqual(self.glow(stage(ZERO, TEXC, ONE, CPREV, op=0x101)), [], "subtracted")
        self.assertEqual(self.glow(stage(ZERO, TEXC, ONE, CPREV), texgens=[4 << 4, 0]), [], "projected")
        self.assertEqual(self.glow(stage(ZERO, TEXC, ONE, CPREV), texgens=[4 << 4, 1 << 4]), [], "env map")
        self.assertEqual(self.glow(stage(CPREV, KONST, TEXC, ZERO)), [], "a mask's blend")
        self.assertEqual(self.glow(stage(ZERO, TEXC, ONE, CPREV),
                                   more=[stage(ZERO, CPREV, RASC, ZERO, texcoord=0, texmap=0)]), [], "lit later")
        self.assertEqual(mc.glow_stages([stage(ZERO, TEXC, ONE, CPREV)], UV_TEXGENS), [], "the first stage")

    def test_material_and_look(self):
        glow = builders.material(geometry.FLAG_KONST, [0, 1], 1,
                                 [LIT, (ARGS(ZERO, KONST, TEXC, CPREV), 0, 0x100, 0x100, 0x0C << 8, 1 | 1 << 8)],
                                 UV_TEXGENS, konst=(0x40804000,))
        m = mc.Material(0, glow, [0x7001, 0x7002])
        self.assertTrue(m.glows)
        self.assertIn("glows", m.summary())
        look = geometry.Look(glow)
        self.assertEqual((look.glow_slot, look.glow_uv), (1, 1))
        for got, want in zip(look.glow_weight, (64 / 255.0, 128 / 255.0, 64 / 255.0)):
            self.assertAlmostEqual(got, want)
        plain = geometry.Look(builders.material(0, [0], 1, [LIT], [4 << 4]))
        self.assertIsNone(plain.glow_slot)
        game = {"luminance": 1.5}
        self.assertAlmostEqual(preview.params({}, look, False, game)["glow"], 1.5)
        self.assertAlmostEqual(preview.params({"luminance": 2}, look, False, game)["glow"], 3.0)
        self.assertAlmostEqual(preview.params({"luminance": 99}, look, False, {"luminance": 4.0})["glow"], 32.0)


class PreviewSettings(unittest.TestCase):
    def setUp(self):
        sets, _model = geometry.cmdl_geometry(test_model())
        ids, datas = builders.ds.parse_material_set(sets[0])
        self.plain, self.shiny = geometry.Look(datas[0]), geometry.Look(datas[1])

    def test_as_the_game_applies_them(self):
        game = {"pbr_maps": True, "live_reflections": True, "pbr_highlight": 1.5}
        p = preview.params({}, self.plain, True, game)
        self.assertTrue(p["pbr"])
        self.assertEqual((p["roughness"], p["strength"], p["env"]), (1.0, 1.0, "none"))
        self.assertAlmostEqual(p["highlight"], 1.5)
        p = preview.params({"pbr": False, "roughness": 9}, self.plain, True, game)
        self.assertFalse(p["pbr"])
        self.assertEqual(p["roughness"], 4.0)  # clamped as the game clamps it
        self.assertFalse(preview.params({}, self.plain, False, game)["pbr"])  # no maps
        self.assertFalse(preview.params({}, self.plain, True, dict(game, pbr_maps=False))["pbr"])
        # The reflection: the painted map unless switched on (and Live reflections on in the game).
        self.assertEqual(preview.params({}, self.shiny, False, game)["env"], "painted")
        p = preview.params({"reflection": True, "reflection_blur": 50, "reflection_strength": 2}, self.shiny,
                           False, game)
        self.assertEqual((p["env"], p["env_blur"], p["env_strength"]), ("live", 0.5, 2.0))
        p = preview.params({"reflection": True}, self.shiny, False, dict(game, live_reflections=False))
        self.assertEqual(p["env"], "painted")
        self.assertEqual(preview.setting_percent({"pbr_highlight": "150"}, "pbr_highlight", 100), 1.5)
        self.assertEqual(preview.setting_percent({}, "pbr_highlight", 100), 1.0)


class PreviewScene(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        resources = [("CMDL", 0x3001, test_model()), ("TXTR", 0x7001, txtr_i8(16, 8)),
                     ("TXTR", 0x7002, txtr_i8(8, 4))]
        image = builders.gc_disc({"Metroid4.pak": pak(resources)})
        self.path = self.dir / "echoes.iso"
        self.path.write_bytes(image)
        self.lib = mc.Library(str(self.path))

    def tearDown(self):
        self.lib.close()
        self.tmp.cleanup()

    def test_disc_textures(self):
        scene = preview.build_scene(self.lib, "CMDL", 0x3001)
        self.assertEqual(sorted(scene.batches), [0, 1])
        self.assertEqual(scene.triangles, 4)
        self.assertEqual(scene.base_texture(0), 0x7001)
        self.assertEqual(scene.base_texture(1), 0x7002)
        self.assertEqual(scene.env_texture(1), 0x7001)
        self.assertEqual(scene.pictures[0x7001].source, "disc")
        self.assertEqual((scene.pictures[0x7001].width, scene.pictures[0x7001].height), (16, 8))
        self.assertFalse(scene.has_maps(0))

    @unittest.skipUnless(preview.Image is not None, "Pillow isn't installed")
    def test_pack_textures_and_maps(self):
        pack = self.dir / "pack"
        pack.mkdir()
        stem = self.lib.texture_pack_name(0x7001)[:-4].replace("tex1_16x8_", "tex1_16x8_m_")
        for suffix, colour in (("", (200, 10, 10, 255)), ("_nrm", (128, 128, 255, 255)), ("_orm", (255, 128, 0, 255))):
            preview.Image.new("RGBA", (32, 16), colour).save(str(pack / (stem + suffix + ".png")))
        # Named as Dolphin names a mipmapped texture (_m), which the game accepts.
        scene = preview.build_scene(self.lib, "CMDL", 0x3001, pack=mc.TexturePack([str(pack)]))
        self.assertEqual(scene.pictures[0x7001].source, "pack")
        self.assertEqual(scene.pictures[0x7001].rgba[:4], bytes((200, 10, 10, 255)))
        self.assertTrue(scene.has_maps(0))
        self.assertFalse(scene.has_maps(1))
        nrm, orm = scene.maps[0x7001]
        self.assertEqual(orm.rgba[:4], bytes((255, 128, 0, 255)))


if __name__ == "__main__":
    unittest.main(verbosity=1)
