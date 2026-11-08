"""tools/material_editor/material_core.py: the key (as the game computes it), paths and settings, the
disc's worlds/areas/models and their materials, texture decoding, PNG, materials.json.

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

    def test_pack_maps(self):
        pack = self.dir / "pack"
        (pack / "sub").mkdir(parents=True)
        for name in ("tex1_8x4_00000000000000aa_1.dds", "tex1_8x4_00000000000000aa_1_NRM.png",
                     "tex1_8x4_00000000000000aa_1_orm.dds"):
            (pack / "sub" / name).write_bytes(b"x")
        index = mc.index_pack(str(pack))
        tex, nrm, orm = mc.pack_maps(index, "tex1_8x4_00000000000000aa_1.dds")
        self.assertTrue(tex and nrm and orm)
        self.assertEqual(mc.pack_maps(index, "tex1_8x4_00000000000000bb_1.dds"), (None, None, None))

    def test_fields_match_the_game(self):
        # The names port_material_overrides.h reads.
        header = (ROOT / "platform" / "include" / "port_material_overrides.h").read_text()
        for name in mc.FIELD_NAMES:
            self.assertIn('"%s"' % name, header, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
