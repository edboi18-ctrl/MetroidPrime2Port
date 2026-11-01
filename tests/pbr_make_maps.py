"""tools/pbr/make_pbr_maps.py: the map conventions, the DDS it writes, settings, and a whole run.

Usage: pbr_make_maps.py <repo root>. Needs numpy and Pillow; without them it exits 77 (skipped).
No game, pack or texconv needed (a stand-in checks the texconv step).
"""
import contextlib
import csv
import io
import json
import os
import pathlib
import struct
import sys
import tempfile
import unittest

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
sys.argv = sys.argv[:1]
sys.path.insert(0, str(ROOT / "tools" / "pbr"))
try:
    import numpy as np
    from PIL import Image
except ImportError:
    if __name__ == "__main__":
        print("numpy or Pillow isn't installed: skipped")
        sys.exit(77)
    raise
import make_pbr_maps as mm  # noqa: E402

FLAT = dict(mm.PRESETS["default"])


def decode(byte):
    """Aurora's PBR shader: sampled (byte / 255) * 1.9921875 - 1."""
    return byte / 255.0 * 1.9921875 - 1.0


def grey(h, w, value=0.5):
    return np.full((h, w, 3), value, np.float32)


def stripes(h, w, period, axis):
    """Luminance sin(2 pi i / period) along columns (axis 1) or rows (axis 0), in 0.3-0.7."""
    i = np.arange(w if axis == 1 else h, dtype=np.float32)
    wave = 0.5 + 0.2 * np.sin(2.0 * np.pi * i / period)
    lum = np.broadcast_to(wave[None, :] if axis == 1 else wave[:, None], (h, w))
    return np.repeat(lum[..., None], 3, axis=-1).astype(np.float32)


class Normals(unittest.TestCase):
    def test_flat_is_byte_128(self):
        normal, _orm = mm.make_maps(grey(16, 16), 2.0, FLAT)
        self.assertTrue(np.allclose(normal, [0.0, 0.0, 1.0], atol=1e-5))
        encoded = mm.encode_normal(normal)
        self.assertTrue((encoded[..., 0] == 128).all() and (encoded[..., 1] == 128).all())
        self.assertAlmostEqual(decode(128), 0.0, places=6)

    def test_encoding_matches_the_shader(self):
        for value in (-1.0, -0.5, 0.0, 0.25, 0.5, 0.9921875):
            byte = int(mm.encode_normal(np.array([[[value, value, 1.0]]], np.float32))[0, 0, 0])
            self.assertAlmostEqual(decode(byte), value, places=6)

    def test_x_faces_away_from_uphill(self):
        # Rising to the right at column 0 faces left (-X); falling at half a period faces right.
        normal, _ = mm.make_maps(stripes(32, 32, 16, axis=1), 2.0, FLAT)
        self.assertLess(normal[:, 0, 0].mean(), -0.05)
        self.assertGreater(normal[:, 8, 0].mean(), 0.05)
        self.assertTrue(np.allclose(normal[..., 1], 0.0, atol=1e-5))

    def test_y_is_up(self):
        # glTF +Y is up the image; rising down the rows at row 0 faces up (+Y).
        normal, _ = mm.make_maps(stripes(32, 32, 16, axis=0), 2.0, FLAT)
        self.assertGreater(normal[0, :, 1].mean(), 0.05)
        self.assertLess(normal[8, :, 1].mean(), -0.05)
        self.assertTrue(np.allclose(normal[..., 0], 0.0, atol=1e-5))

    def test_invert_and_strength(self):
        image = stripes(32, 32, 16, axis=1)
        base, _ = mm.make_maps(image, 2.0, FLAT)
        flipped, _ = mm.make_maps(image, 2.0, dict(FLAT, invert=True))
        self.assertTrue(np.allclose(flipped[..., 0], -base[..., 0], atol=1e-5))
        weak, _ = mm.make_maps(image, 2.0, dict(FLAT, normal=0.25))
        self.assertLess(abs(weak[:, 0, 0].mean()), abs(base[:, 0, 0].mean()))
        self.assertTrue(np.allclose(np.linalg.norm(base, axis=-1), 1.0, atol=1e-5))

    def test_broad_shading_is_not_a_slope(self):
        # A gradient over the whole texture (painted light) is removed by the relief high-pass.
        broad, _ = mm.make_maps(stripes(64, 64, 64, axis=1), 1.0, dict(FLAT, relief=4.0))
        fine, _ = mm.make_maps(stripes(64, 64, 8, axis=1), 1.0, dict(FLAT, relief=4.0))
        self.assertLess(np.abs(broad[..., 0]).max(), 0.5 * np.abs(fine[..., 0]).max())

    def test_filters_wrap(self):
        # A tiling texture's maps tile: the same image shifted gives the same maps shifted.
        rng = np.random.default_rng(3)
        image = np.repeat(rng.random((24, 24, 1), dtype=np.float32), 3, axis=-1)
        a, orm_a = mm.make_maps(image, 2.0, FLAT)
        b, orm_b = mm.make_maps(np.roll(image, (5, 7), axis=(0, 1)), 2.0, FLAT)
        self.assertTrue(np.allclose(np.roll(a, (5, 7), axis=(0, 1)), b, atol=1e-4))
        self.assertTrue(np.allclose(np.roll(orm_a, (5, 7), axis=(0, 1)), orm_b, atol=1e-4))


class Orm(unittest.TestCase):
    def test_flat_takes_the_preset(self):
        metal = mm.PRESETS["metal"]
        _, orm = mm.make_maps(grey(8, 8), 2.0, metal)
        encoded = mm.encode_unorm(orm)
        self.assertTrue((encoded[..., 0] == 255).all())  # R occlusion: none
        self.assertTrue((encoded[..., 1] == round(metal["roughness"] * 255)).all())  # G roughness
        self.assertTrue((encoded[..., 2] == 255).all())  # B metal
        self.assertTrue((encoded[..., 3] == 255).all())

    def test_a_hollow_is_darker_and_rougher(self):
        image = grey(32, 32, 0.6)
        image[14:18, 14:18] = 0.2
        _, orm = mm.make_maps(image, 2.0, mm.PRESETS["metal"])
        self.assertLess(orm[16, 16, 0], 0.99)
        self.assertGreater(orm[16, 16, 1], mm.PRESETS["metal"]["roughness"])
        self.assertLess(orm[16, 16, 2], 1.0)
        self.assertTrue(((orm >= 0.0) & (orm <= 1.0)).all())


class Mips(unittest.TestCase):
    def test_chain_sizes(self):
        sizes = [level.shape[:2] for level in mm.mip_chain(np.zeros((2, 8, 4), np.float32))]
        self.assertEqual(sizes, [(2, 8), (1, 4), (1, 2), (1, 1)])
        sizes = [level.shape[:2] for level in mm.mip_chain(np.zeros((5, 3, 4), np.float32))]
        self.assertEqual(sizes, [(5, 3), (2, 1), (1, 1)])  # max(1, n >> level), as DDS counts them

    def test_normals_stay_unit(self):
        rng = np.random.default_rng(5)
        normal, _ = mm.make_maps(np.repeat(rng.random((16, 16, 1), dtype=np.float32), 3, -1), 2.0, FLAT)
        for level in mm.mip_chain(normal, renormalize=True):
            self.assertTrue(np.allclose(np.linalg.norm(level, axis=-1), 1.0, atol=1e-4))


class Dds(unittest.TestCase):
    def check_header(self, data, w, h, mips):
        """Aurora's rules (extern/aurora/lib/gfx/dds_io.cpp) and the RGBA8 layout."""
        self.assertEqual(data[:4], b"DDS ")
        size, flags, height, width, pitch, _depth, count = struct.unpack_from("<7I", data, 4)
        self.assertEqual((size, height, width, pitch, count), (124, h, w, w * 4, mips))
        pf = struct.unpack_from("<8I", data, 76)
        self.assertEqual(pf, (32, 0x41, 0, 32, 0xFF, 0xFF00, 0xFF0000, 0xFF000000))
        caps, caps2 = struct.unpack_from("<2I", data, 108)
        self.assertEqual(caps2, 0)
        if mips > 1:
            self.assertTrue(flags & 0x20000 and caps & 0x8 and caps & 0x400000)
        expected, lw, lh = 0, w, h
        for _ in range(mips):
            expected += lw * lh * 4
            lw, lh = max(1, lw >> 1), max(1, lh >> 1)
        self.assertEqual(len(data), 128 + expected)

    def test_rgba8_chain(self):
        rng = np.random.default_rng(7)
        top = rng.integers(0, 256, (8, 16, 4), dtype=np.uint8)
        levels = mm.mip_chain(top.astype(np.float32))
        data = mm.dds_rgba8([np.rint(level).astype(np.uint8) for level in levels])
        self.check_header(data, 16, 8, 5)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (16, 8))
            self.assertTrue((np.asarray(image.convert("RGBA")) == top).all())

    def test_single_level(self):
        data = mm.dds_rgba8([np.zeros((4, 4, 4), np.uint8)])
        self.check_header(data, 4, 4, 1)


class Settings(unittest.TestCase):
    def test_texture_key(self):
        for name in ("tex1_64x64_m_0123456789abcdef_14.dds", "TEX1_64x64_0123456789ABCDEF_14.DDS",
                     "tex1_64x64_0123456789abcdef_14"):
            self.assertEqual(mm.texture_key(name), "tex1_64x64_0123456789abcdef_14")

    def test_presets_by_folder_and_override(self):
        settings = mm.default_settings()
        rel = mm.rel_path("Sanctuary Fortress\\tex1_64x64_m_0123456789abcdef_14.dds")
        self.assertEqual(mm.params_for(settings, rel, "")[0], "metal")
        self.assertEqual(mm.params_for(settings, mm.rel_path("Genn/x.dds"), "")[0], "default")
        self.assertEqual(mm.params_for(settings, mm.rel_path("x.dds"), "")[0], "default")
        warnings = []
        settings = mm.merge_settings(settings, {
            "folders": {"genn": "stone"},
            "presets": {"stone": {"normal": 3, "bogus": 1}, "glass": {"roughness": 0.1}},
            "textures": {"tex1_64x64_0123456789abcdef_14.dds": {"preset": "glass", "metal": "lots"}},
        }, warnings)
        self.assertEqual(len(warnings), 2)  # bogus, and metal isn't a number
        self.assertEqual(mm.params_for(settings, mm.rel_path("Genn/x.dds"), ""),
                         ("stone", dict(mm.PRESETS["stone"], normal=3.0)))
        preset, params = mm.params_for(settings, rel, "")  # matched by the pack name, _m and all
        self.assertEqual((preset, params["roughness"], params["metal"]), ("glass", 0.1, 0.0))
        preset, _ = mm.params_for(settings, mm.rel_path("Agon Wastes/other.dds"),
                                  "tex1_64x64_0123456789abcdef_14.dds")  # by the manifest name
        self.assertEqual(preset, "glass")

    def test_written_settings_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d, "s.json")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mm.main(["--write-settings", str(path)]), 0)
            warnings = []
            merged = mm.merge_settings(mm.default_settings(), json.loads(path.read_text("utf-8")), warnings)
            self.assertEqual(warnings, [])
            self.assertEqual(merged, mm.default_settings())

    def test_output_size(self):
        self.assertEqual(mm.output_size(1024, 512, 256, 128, 0.5), (512, 256))
        self.assertEqual(mm.output_size(1024, 1024, 256, 256, 0.1), (256, 256))  # not below the original
        self.assertEqual(mm.output_size(1024, 1024, 256, 256, 1.0), (1024, 1024))
        self.assertEqual(mm.output_size(100, 100, 25, 25, 0.5), (48, 48))  # whole 4x4 blocks
        self.assertEqual(mm.output_size(2, 2, 2, 2, 0.5), (2, 2))


FAKE_TEXCONV = r'''
import os, shutil, sys
args = sys.argv[1:]
if os.environ.get("FAKE_TEXCONV_FAIL"):
    print("ERROR: no"); sys.exit(1)
out = args[args.index("-o") + 1]
with open(os.path.join(out, "..", "args.txt"), "w") as f:
    f.write(" ".join(args))
shutil.copy(args[-1], os.path.join(out, os.path.basename(args[-1])))
'''


class Texconv(unittest.TestCase):
    def test_command(self):
        self.assertEqual(mm.texconv_command("t.exe", "BC5_UNORM", "o", "in.dds", False),
                         ["t.exe", "-nologo", "-y", "-f", "BC5_UNORM", "-o", "o", "in.dds"])
        self.assertEqual(mm.texconv_command("t.exe", "BC7_UNORM", "o", "in.dds", True)[-3:],
                         ["-bc", "q", "in.dds"])

    def test_find(self):
        with tempfile.TemporaryDirectory() as d:
            exe = pathlib.Path(d, "texconv.exe")
            exe.write_bytes(b"")
            self.assertEqual(mm.find_texconv(str(exe)), str(exe))
            self.assertIsNone(mm.find_texconv(str(exe) + ".missing"))

    def test_write_map_through_texconv(self):
        with tempfile.TemporaryDirectory() as d:
            fake = pathlib.Path(d, "fake_texconv.py")
            fake.write_text(FAKE_TEXCONV, encoding="utf-8")
            real = mm.texconv_command
            mm.texconv_command = lambda exe, fmt, out, src, quick: \
                [sys.executable, str(fake)] + real("t", fmt, out, src, quick)[1:]
            try:
                dest = pathlib.Path(d, "out", "Temple Grounds", "tex1_8x8_m_0123456789abcdef_14_nrm.dds")
                levels = [np.zeros((4, 4, 4), np.uint8), np.zeros((2, 2, 4), np.uint8),
                          np.zeros((1, 1, 4), np.uint8)]
                mm.write_map(levels, dest, mm.NORMAL_FORMAT, "t", False)
                self.assertEqual(dest.read_bytes(), mm.dds_rgba8(levels))
                os.environ["FAKE_TEXCONV_FAIL"] = "1"
                with self.assertRaises(RuntimeError):
                    mm.write_map(levels, dest.with_name("x_orm.dds"), mm.ORM_FORMAT, "t", False)
                self.assertFalse(dest.with_name("x_orm.dds").exists())
            finally:
                mm.texconv_command = real
                os.environ.pop("FAKE_TEXCONV_FAIL", None)


STONE = "tex1_16x16_m_00000000000000a1_14"
METAL = "tex1_16x16_00000000000000b2_5"
GLOW = "tex1_16x16_m_00000000000000c3_14"


class WholeRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.pack = self.dir / "Pack"
        rng = np.random.default_rng(11)
        texels = rng.integers(40, 220, (16, 16, 1), dtype=np.uint8)
        art = np.repeat(np.repeat(np.repeat(texels, 4, 0), 4, 1), 3, 2)  # a 4x upscale, 64x64
        rgba = np.concatenate([art, np.full((64, 64, 1), 255, np.uint8)], -1)
        (self.pack / "Temple Grounds").mkdir(parents=True)
        (self.pack / "Temple Grounds" / (STONE + ".dds")).write_bytes(mm.dds_rgba8([rgba]))
        (self.pack / "Sanctuary Fortress").mkdir()
        Image.fromarray(rgba).save(str(self.pack / "Sanctuary Fortress" / (METAL + ".png")))
        (self.pack / "FX").mkdir()
        Image.fromarray(rgba).save(str(self.pack / "FX" / (GLOW + ".png")))
        self.report = self.dir / "texture_report.csv"
        with open(str(self.report), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pbr_candidate", "roles", "name", "pack_file", "category", "width", "height",
                        "format", "uses", "additive", "alpha_test", "sources"])
            w.writerow(["yes", "diffuse", "x", "Temple Grounds\\%s.dds" % STONE, "Temple Grounds",
                        16, 16, 14, 1, "", "", "area 1"])
            w.writerow(["yes", "diffuse", "y", "Sanctuary Fortress\\%s.png" % METAL, "Sanctuary Fortress",
                        16, 16, 5, 1, "", "", "area 2"])
            w.writerow(["no", "glow", "z", "FX\\%s.png" % GLOW, "FX", 16, 16, 14, 1, "yes", "", "CMDL 1"])
            w.writerow(["yes", "diffuse", "w", "Temple Grounds\\gone.dds", "Temple Grounds",
                        16, 16, 14, 1, "", "", "area 3"])
        self.out = self.dir / "Pack PBR"

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mm.main([str(self.report), str(self.pack), "--no-compress", "-j", "1"] + list(extra))
        return code, buffer.getvalue()

    def read(self, rel):
        with Image.open(str(self.out / rel)) as image:
            return np.asarray(image.convert("RGBA"))

    def test_run(self):
        code, text = self.run_tool("--preview")
        self.assertEqual(code, 0, text)
        self.assertIn("2 to make, 0 up to date, 1 missing", text)
        stone_nrm = self.read("Temple Grounds/%s_nrm.dds" % STONE)
        self.assertEqual(stone_nrm.shape, (32, 32, 4))  # half the 64x64 pack file
        self.assertGreater(np.abs(stone_nrm[..., 0].astype(int) - 128).max(), 4)  # it has relief
        metal_orm = self.read("Sanctuary Fortress/%s_orm.dds" % METAL)
        self.assertGreater(metal_orm[..., 2].mean(), 200)  # the folder's metal preset
        stone_orm = self.read("Temple Grounds/%s_orm.dds" % STONE)
        self.assertEqual(stone_orm[..., 2].max(), 0)
        self.assertFalse((self.out / "FX").exists())  # not a candidate
        self.assertTrue((self.out / "_preview" / "Temple Grounds" / (STONE + ".png")).is_file())
        index = json.loads((self.out / mm.INDEX_NAME).read_text("utf-8"))
        self.assertEqual(sorted(index["maps"]), ["Sanctuary Fortress/%s.png" % METAL,
                                                 "Temple Grounds/%s.dds" % STONE])
        self.assertEqual(index["maps"]["Temple Grounds/%s.dds" % STONE]["preset"], "stone")
        self.assertEqual(index["maps"]["Temple Grounds/%s.dds" % STONE]["nrm"],
                         "Temple Grounds/%s_nrm.dds" % STONE)
        self.assertEqual(sorted(p.name for p in self.pack.rglob("*")),
                         sorted(["Temple Grounds", "Sanctuary Fortress", "FX", STONE + ".dds",
                                 METAL + ".png", GLOW + ".png"]))  # the pack is untouched

        code, text = self.run_tool("--preview")
        self.assertIn("0 to make, 2 up to date", text)
        settings = self.dir / "s.json"
        settings.write_text(json.dumps({"presets": {"metal": {"roughness": 0.2}}}), "utf-8")
        code, text = self.run_tool("--preview", "--settings", str(settings))
        self.assertIn("1 to make, 1 up to date", text)
        self.assertTrue((self.read("Sanctuary Fortress/%s_orm.dds" % METAL)[..., 1] < 80).mean() > 0.5)
        code, text = self.run_tool("--only", "temple*", "--force")
        self.assertIn("1 to make, 0 up to date", text)
        code, text = self.run_tool("--force", "-j", "2")  # two worker processes
        self.assertEqual(code, 0, text)
        self.assertIn("Made 2, 0 up to date, 0 failed", text)

    def test_output_inside_the_pack_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_tool("-o", str(self.pack / "maps"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
