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

    def test_max_tilt_caps_the_steepest(self):
        rng = np.random.default_rng(9)
        noise = np.repeat(rng.random((32, 32, 1), dtype=np.float32), 3, axis=-1)
        for cap in (20.0, 40.0):
            normal, _ = mm.make_maps(noise, 2.0, dict(FLAT, normal=50.0, max_tilt=cap))
            tilt = np.degrees(np.arccos(np.clip(normal[..., 2], -1.0, 1.0)))
            self.assertAlmostEqual(float(np.percentile(tilt, 95)), cap, delta=1.0)
        gentle, _ = mm.make_maps(stripes(32, 32, 16, axis=1), 2.0, FLAT)  # under the cap: untouched
        free, _ = mm.make_maps(stripes(32, 32, 16, axis=1), 2.0, dict(FLAT, max_tilt=0.0))
        self.assertTrue(np.allclose(gentle, free, atol=1e-6))

    def test_chroma_sinks_bright_colour(self):
        # Grey stones with orange cracks: brighter cracks read as high, unless chroma takes them down.
        image = np.full((32, 32, 3), 0.35, np.float32)
        image[:, 15:17] = (0.9, 0.45, 0.1)
        for chroma, sign in ((0.0, 1.0), (1.0, -1.0)):
            normal, _ = mm.make_maps(image, 2.0, dict(FLAT, chroma=chroma))
            # Left of the crack the surface faces left (-X) if the crack is high, right if it is low.
            self.assertGreater(-sign * normal[:, 14, 0].mean(), 0.02)

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
        self.assertTrue((encoded[..., 2] == round(metal["metal"] * 255)).all())  # B metal
        self.assertTrue((encoded[..., 3] == 255).all())

    def test_a_hollow_is_darker_and_rougher(self):
        image = grey(32, 32, 0.6)
        image[14:18, 14:18] = 0.2
        _, orm = mm.make_maps(image, 2.0, mm.PRESETS["metal"])
        self.assertLess(orm[16, 16, 0], 0.99)
        self.assertGreater(orm[16, 16, 1], mm.PRESETS["metal"]["roughness"])
        self.assertLess(orm[16, 16, 2], 1.0)
        self.assertTrue(((orm >= 0.0) & (orm <= 1.0)).all())


class EmptySpace(unittest.TestCase):
    def atlas(self):
        """A 64x64 atlas: a textured grey piece on black ground."""
        rng = np.random.default_rng(13)
        image = np.zeros((64, 64, 3), np.float32)
        image[16:48, 16:48] = 0.5 + 0.05 * rng.standard_normal((32, 32, 1)).astype(np.float32)
        return image

    def test_pieces_have_no_bevel(self):
        image = self.atlas()
        filled, _ = mm.make_maps(image, 2.0, FLAT)
        bare, _ = mm.make_maps(image, 2.0, dict(FLAT, fill_empty=False))
        edge = (slice(20, 44), slice(16, 19))  # inside the piece, along its left edge
        self.assertGreater(np.abs(bare[edge][..., 0]).mean(), 0.15)
        self.assertLess(np.abs(filled[edge][..., 0]).mean(), 0.5 * np.abs(bare[edge][..., 0]).mean())
        inside = (slice(24, 40), slice(24, 40))
        self.assertTrue(np.allclose(filled[inside], bare[inside], atol=0.08))  # the piece's own detail

    def test_what_counts_as_empty(self):
        image = self.atlas()
        mask = mm.empty_space(image, None, 2.0)
        self.assertTrue(mask[0, 0] and mask[15, 30] and not mask[30, 30])
        groove = np.full((64, 64, 3), 0.5, np.float32)
        groove[:, 31:33] = 0.0  # one texel of black at 2 px a texel: a line, not ground
        self.assertIsNone(mm.empty_space(groove, None, 2.0))
        alpha = np.ones((64, 64), np.float32)
        alpha[:, :20] = 0.0  # a cut-out's transparent part counts, whatever its colour
        mask = mm.empty_space(np.full((64, 64, 3), 0.5, np.float32), alpha, 2.0)
        self.assertTrue(mask[:, :19].all() and not mask[:, 21:].any())
        flat = np.full((64, 64, 3), 0.5, np.float32)
        self.assertIsNone(mm.empty_space(flat, None, 2.0))
        white = 1.0 - self.atlas()  # a piece on white ground counts the same
        white[16:48, 16:48] = self.atlas()[16:48, 16:48]
        mask = mm.empty_space(white, None, 2.0)
        self.assertTrue(mask[0, 0] and mask[15, 30] and not mask[30, 30])

    def test_push_pull(self):
        values = np.zeros((16, 16), np.float32)
        values[:, :8] = 0.25
        values[:, 8:] = 0.75
        known = np.zeros((16, 16), bool)
        known[:, :3] = known[:, 13:] = True
        filled = mm.push_pull(values, known)
        self.assertTrue((filled[known] == values[known]).all())
        self.assertTrue(((filled >= 0.25 - 1e-6) & (filled <= 0.75 + 1e-6)).all())
        self.assertLess(abs(filled[:, 3].mean() - 0.25), 0.2)  # the hole meets each side near its value


class Preview(unittest.TestCase):
    def test_layouts(self):
        rgb = grey(16, 16)
        normal, orm = mm.make_maps(rgb, 2.0, FLAT)
        self.assertEqual(mm.preview_image(rgb, normal, orm).size, (5 * 16 + 4 * 4, 16 + 14))  # side by side
        rgb = grey(32, 256)
        normal, orm = mm.make_maps(rgb, 2.0, FLAT)
        self.assertEqual(mm.preview_image(rgb, normal, orm).size, (256, 5 * (32 + 14) + 4 * 4))  # stacked
        rgb = grey(512, 4096)
        normal, orm = mm.make_maps(rgb, 1.0, FLAT)
        self.assertEqual(mm.preview_image(rgb, normal, orm).size[0], 1024)

    def test_metal_keeps_its_environment(self):
        # With the shader's probe-less environment a metal isn't black where the light misses it.
        rgb = grey(8, 8, 0.6)
        flat = np.zeros((8, 8, 3), np.float32)
        flat[..., 2] = 1.0
        metal = np.stack([np.ones((8, 8)), np.full((8, 8), 0.45), np.ones((8, 8))], -1).astype(np.float32)
        self.assertGreater(mm.shade(rgb, flat, metal).mean(), 0.3)

    def test_env_brdf_bounds(self):
        rough, n_v = np.meshgrid(np.linspace(0, 1, 11), np.linspace(0.01, 1, 11))
        a, b = mm.env_brdf(rough, n_v)
        self.assertTrue(((a + b) <= 1.05).all() and ((a + b) >= 0.0).all())


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

    def test_settings_file_is_read_and_kept(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                path = pathlib.Path(mm.SETTINGS_NAME)
                path.write_text(json.dumps({"textures": {"tex1_8x8_0123456789abcdef_14": {"chroma": 1.0}},
                                            "presets": {"stone": {"normal": 9.0}}}), "utf-8")
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(mm.main(["--write-settings", str(path)]), 0)
                self.assertIn("Using the settings in", out.getvalue())
                kept = json.loads(path.read_text("utf-8"))  # the edits survive a rewrite
                self.assertEqual(kept["textures"]["tex1_8x8_0123456789abcdef_14"], {"chroma": 1.0})
                self.assertEqual(kept["presets"]["stone"]["normal"], 9.0)
                self.assertIn("fill_empty", kept["presets"]["stone"])  # and new keys are added
            finally:
                os.chdir(cwd)

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
if os.environ.get("FAKE_TEXCONV_ARGS"):
    with open(os.environ["FAKE_TEXCONV_ARGS"], "w") as f:
        f.write(" ".join(args))
if os.environ.get("FAKE_TEXCONV_BC5U"):
    with open(os.path.join(out, os.path.basename(args[-1])), "wb") as f:
        f.write(bytes.fromhex(os.environ["FAKE_TEXCONV_BC5U"]))
else:
    shutil.copy(args[-1], os.path.join(out, os.path.basename(args[-1])))
'''


def dx10_dds(dxgi):
    """A 4x4 one-level DDS with the DX10 header and DXGI format `dxgi` (16 bytes of data)."""
    header = struct.pack("<7I44x", 124, 0x81007, 4, 4, 16, 0, 1)
    pixel_format = struct.pack("<2I4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    tail = struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return b"DDS " + header + pixel_format + tail + struct.pack("<5I", dxgi, 3, 0, 1, 0) + b"\0" * 16


def bc5_dds(fourcc=b"BC5U", red=200, green=60):
    """A 4x4 one-level BC5 DDS with a legacy FourCC header: every texel (red, green)."""
    header = struct.pack("<7I44x", 124, 0x81007, 4, 4, 16, 0, 1)
    pixel_format = struct.pack("<2I4s5I", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    tail = struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    block = bytes([red, red]) + b"\0" * 6 + bytes([green, green]) + b"\0" * 6
    return b"DDS " + header + pixel_format + tail + block


class Texconv(unittest.TestCase):
    def test_command(self):
        self.assertEqual(mm.texconv_command("t.exe", "BC5_UNORM", "o", "in.dds", False),
                         ["t.exe", "-nologo", "-y", "-dx10", "-f", "BC5_UNORM", "-o", "o", "in.dds"])
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
            os.environ["FAKE_TEXCONV_ARGS"] = str(pathlib.Path(d, "args.txt"))
            try:
                dest = pathlib.Path(d, "out", "Temple Grounds", "tex1_8x8_m_0123456789abcdef_14_nrm.dds")
                levels = [np.zeros((4, 4, 4), np.uint8), np.zeros((2, 2, 4), np.uint8),
                          np.zeros((1, 1, 4), np.uint8)]
                mm.write_map(levels, dest, mm.NORMAL_FORMAT, "t", False)
                self.assertEqual(dest.read_bytes(), mm.dds_rgba8(levels))
                self.assertIn("-dx10", pathlib.Path(d, "args.txt").read_text())
                # Run 41357701039's texconv (no -dx10) wrote BC5 as 'BC5U': it is stored as DX10 83.
                os.environ["FAKE_TEXCONV_BC5U"] = bc5_dds().hex()
                mm.write_map(levels, dest, mm.NORMAL_FORMAT, "t", False)
                data = dest.read_bytes()
                self.assertEqual((data[84:88], struct.unpack_from("<I", data, 128)[0]), (b"DX10", 83))
                os.environ.pop("FAKE_TEXCONV_BC5U")
                os.environ["FAKE_TEXCONV_FAIL"] = "1"
                with self.assertRaises(RuntimeError):
                    mm.write_map(levels, dest.with_name("x_orm.dds"), mm.ORM_FORMAT, "t", False)
                self.assertFalse(dest.with_name("x_orm.dds").exists())
            finally:
                mm.texconv_command = real
                os.environ.pop("FAKE_TEXCONV_FAIL", None)
                os.environ.pop("FAKE_TEXCONV_BC5U", None)
                os.environ.pop("FAKE_TEXCONV_ARGS", None)

    def test_port_readable_dds(self):
        fixed = mm.port_readable_dds(bc5_dds())
        self.assertEqual(len(fixed), len(bc5_dds()) + 20)
        self.assertEqual((fixed[84:88],) + struct.unpack_from("<4I", fixed, 128), (b"DX10", 83, 3, 0, 1))
        with Image.open(io.BytesIO(fixed)) as image:  # the same data, read as BC5
            pixels = np.asarray(image.convert("RGB"))
        self.assertTrue((pixels[..., 0] == 200).all() and (pixels[..., 1] == 60).all())
        self.assertEqual(mm.port_readable_dds(bc5_dds(b"ATI2"))[84:88], b"DX10")
        raw = mm.dds_rgba8([np.zeros((4, 4, 4), np.uint8)])
        self.assertEqual(mm.port_readable_dds(raw), raw)
        for good in (dx10_dds(98), dx10_dds(83), dx10_dds(28)):  # BC7, BC5, RGBA8
            self.assertEqual(mm.port_readable_dds(good), good)
        bgr = raw[:92] + struct.pack("<3I", 0xFF00, 0xFF0000, 0xFF) + raw[104:]  # an RGB order it lacks
        for bad in (bc5_dds(b"BC4U"), dx10_dds(95), dx10_dds(99)[:140], bgr, b"PNG"):
            with self.assertRaises(ValueError):
                mm.port_readable_dds(bad)


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
        self.assertGreater(metal_orm[..., 2].mean(), 0.8 * mm.PRESETS["metal"]["metal"] * 255)  # its preset
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
