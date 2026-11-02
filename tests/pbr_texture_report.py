"""tools/pbr/texture_report.py: name parsing, the role rules, pack matching and a whole run.

Usage: pbr_texture_report.py <repo root>. Standard library only; no game or pack needed.
"""
import csv
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
sys.argv = sys.argv[:1]
sys.path.insert(0, str(ROOT / "tools" / "pbr"))
import texture_report as tr  # noqa: E402

ZERO, TEXC, TEXA, RASC, KONST, CPREV = tr.CC_ZERO, tr.CC_TEXC, tr.CC_TEXA, tr.CC_RASC, 14, tr.CC_CPREV
CA_ZERO, CA_TEXA, CA_KONST = 7, tr.CA_TEXA, 6
TEX0 = 4  # GX_TG_TEX0


def args(a, b, c, d):
    return a | b << 5 | c << 10 | d << 15


def stage(texmap, texcoord, color, alpha=(CA_ZERO, CA_ZERO, CA_ZERO, CA_KONST), out=0):
    """`out`: the colour result's register (0 PREV, 1-3 C0-C2), bits 9-10 of the op word."""
    return [texcoord | texmap << 8 | 0xFF << 16, args(*color), args(*alpha), 0x100 | out << 9, 0x100, 12, 28]


def texgen(src):
    return src << 4


def record(name, slot, stages, texgens=(texgen(TEX0), texgen(TEX0)), flags=0, blend=1, src="area 0"):
    return {"name": name, "texel": 0, "slot": slot, "flags": flags, "blend": blend, "mat": 0,
            "src": src, "group": False, "stages": list(stages), "texgens": list(texgens)}


DIFFUSE = stage(0, 0, (ZERO, TEXC, RASC, ZERO))  # texture times lighting


class Names(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(tr.parse_name("tex1_64x32_0123456789abcdef_14.dds"),
                         (64, 32, "0123456789abcdef", None, 14))
        self.assertEqual(tr.parse_name("tex1_64x32_m_0123456789ABCDEF_14.png"),
                         (64, 32, "0123456789abcdef", None, 14))
        self.assertEqual(tr.parse_name("tex1_8x8_0123456789abcdef_fedcba9876543210_9.dds"),
                         (8, 8, "0123456789abcdef", "fedcba9876543210", 9))
        self.assertEqual(tr.parse_name("tex1_8x8_0123456789abcdef_$_9.dds"),
                         (8, 8, "0123456789abcdef", "$", 9))
        self.assertEqual(tr.parse_name("tex1_64x32_0123456789abcdef_14_arb.dds"),
                         (64, 32, "0123456789abcdef", None, 14))
        # A mip level belongs to its base file; anything else isn't a replacement.
        self.assertIsNone(tr.parse_name("tex1_64x32_0123456789abcdef_14_mip1.dds"))
        self.assertIsNone(tr.parse_name("readme.txt"))
        self.assertIsNone(tr.parse_name("tex1_64x32_0123_14.dds"))


class Roles(unittest.TestCase):
    def test_diffuse(self):
        self.assertEqual(tr.classify(record("t", 0, [DIFFUSE])), "diffuse")

    def test_lightmap_then_diffuse(self):
        stages = [stage(0, 0, (ZERO, TEXC, KONST, ZERO)), stage(1, 1, (ZERO, TEXC, CPREV, ZERO))]
        self.assertEqual(tr.classify(record("t", 0, stages, flags=tr.FLAG_LIGHTMAP)), "lightmap")
        self.assertEqual(tr.classify(record("t", 1, stages, flags=tr.FLAG_LIGHTMAP)), "diffuse")
        # Without the flag the first texture is a diffuse.
        self.assertEqual(tr.classify(record("t", 0, stages)), "diffuse")

    def test_glow_added_on_top(self):
        stages = [DIFFUSE, stage(1, 1, (ZERO, TEXC, KONST, CPREV))]
        self.assertEqual(tr.classify(record("t", 1, stages)), "glow")
        # The texture as the addend of a later stage is a glow too.
        stages = [DIFFUSE, stage(1, 1, (ZERO, CPREV, KONST, TEXC))]
        self.assertEqual(tr.classify(record("t", 1, stages)), "glow")

    def test_reflection_from_normals(self):
        stages = [DIFFUSE, stage(1, 1, (ZERO, TEXC, KONST, CPREV))]
        self.assertEqual(tr.classify(record("t", 1, stages, texgens=(texgen(TEX0), texgen(tr.TG_NRM)))),
                         "reflection")

    def test_alpha_mask_and_unused(self):
        stages = [DIFFUSE, stage(1, 1, (ZERO, ZERO, ZERO, CPREV), (CA_ZERO, CA_ZERO, CA_ZERO, CA_TEXA))]
        self.assertEqual(tr.classify(record("t", 1, stages)), "alpha")
        self.assertEqual(tr.classify(record("t", 2, stages)), "unused")
        self.assertEqual(tr.classify(record("t", 2, stages, flags=tr.FLAG_INDIRECT)), "indirect")


class Layers(unittest.TestCase):
    def test_reflective_and_glow_columns(self):
        # Phazon's material (CMDL 0x105600E8): lit diffuse, a glow added, a reflection added.
        glow = stage(1, 1, (ZERO, TEXC, KONST, CPREV))
        shine = stage(2, 2, (ZERO, TEXC, KONST, CPREV))
        texgens = (texgen(TEX0), texgen(TEX0), texgen(tr.TG_NRM))
        phazon = [DIFFUSE, glow, shine]
        a, b, c, d = ("tex1_8x8_00000000000000%02x_14.dds" % i for i in (1, 2, 3, 4))
        records = [record(a, 0, phazon, texgens, src="CMDL 1"), record(b, 1, phazon, texgens, src="CMDL 1"),
                   record(c, 2, phazon, texgens, src="CMDL 1"),
                   record(a, 0, [DIFFUSE], src="area 1"),  # the same texture, plain, elsewhere
                   record(d, 0, [DIFFUSE], src="area 1", )]
        records[-1]["mat"] = 1  # another material of the same source: not shiny
        rows = {r["name"]: r for r in tr.build_report(records, {})}
        self.assertEqual((rows[a]["reflective"], rows[a]["glow_layer"]), ("1/2", "1/2"))
        self.assertEqual((rows[d]["reflective"], rows[d]["glow_layer"]), ("", ""))
        self.assertEqual((rows[b]["roles"], rows[c]["roles"]), ("glow", "reflection"))
        line = tr.summary(rows.values(), {}, 0, 0).splitlines()[2]
        self.assertIn("with a reflection layer (made shinier) 0", line)  # nothing is in a pack here


class Masks(unittest.TestCase):
    def test_reflection_mask(self):
        # Phazon's shape: lit diffuse; a texture into C2; the reflection added, scaled by C2.
        C2 = 6
        stages = [DIFFUSE, stage(1, 1, (ZERO, ZERO, ZERO, TEXC), out=3),
                  stage(2, 2, (ZERO, C2, TEXC, CPREV))]
        texgens = (texgen(TEX0), texgen(TEX0 + 1), texgen(tr.TG_NRM))
        wall, mask, shine, plain = ("tex1_8x8_00000000000000%02x_14.dds" % i for i in (1, 2, 3, 4))
        records = [record(wall, 0, stages, texgens, src="CMDL 1"), record(mask, 1, stages, texgens, src="CMDL 1"),
                   record(shine, 2, stages, texgens, src="CMDL 1"),
                   record(mask, 0, [DIFFUSE], src="area 1")]  # the mask is a surface elsewhere
        self.assertEqual([tr.classify(r) for r in records[:3]], ["diffuse", "mask", "reflection"])
        index = {tr.parse_name(n): [pathlib.PurePosixPath("Agon Wastes/" + n)] for n in (wall, mask)}
        rows = {r["name"]: r for r in tr.build_report(records, index)}
        self.assertEqual((rows[wall]["reflective"], rows[wall]["reflection_masks"]), ("1/1", mask + ":c*1"))
        self.assertEqual(set(rows[mask]["roles"].split("+")), {"diffuse", "mask"})  # ties keep first-seen order
        self.assertEqual(rows[mask]["pbr_candidate"], "yes")  # its surface use still wants maps
        only_mask = [r for r in records[:3]]
        rows = {r["name"]: r for r in tr.build_report(only_mask, index)}
        self.assertEqual((rows[mask]["roles"], rows[mask]["pbr_candidate"]), ("mask", "no"))

    def test_strengths(self):
        texgens = (texgen(TEX0), texgen(tr.TG_NRM))
        unmasked = [DIFFUSE, stage(1, 1, (ZERO, TEXC, 12, CPREV))]  # scaled by ONE
        konst = [DIFFUSE, stage(1, 1, (ZERO, TEXC, KONST, CPREV))]
        strengths = lambda st: tr.reflection_strengths([tr.Stage(s) for s in st], list(texgens))
        self.assertEqual(strengths(unmasked), [("full",)])
        self.assertEqual(strengths(konst), [("unknown",)])
        self.assertEqual(strengths([DIFFUSE]), [])


class WholeRun(unittest.TestCase):
    def test_report(self):
        wall = "tex1_64x64_00000000000000aa_14.dds"
        hud = "tex1_32x32_00000000000000bb_14.dds"
        glow = "tex1_16x16_00000000000000cc_14.dds"
        pal = "tex1_8x8_00000000000000dd_00000000000000ee_9.dds"
        missing = "tex1_4x4_00000000000000ff_14.dds"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            pack = tmp / "G2M"
            for rel in ("Agon Wastes/" + wall, "HUD/" + hud, "FX/" + glow,
                        "Agon Wastes/tex1_8x8_00000000000000dd_$_9.dds",
                        "Agon Wastes/tex1_64x64_00000000000000aa_14_mip1.dds",
                        "Sanctuary/tex1_2x2_0000000000000011_14.dds"):
                path = pack / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            lines = [
                record(wall, 0, [DIFFUSE], src="area 3 world model 1"),
                record(wall, 0, [DIFFUSE], src="area 4 world model 2"),
                record(hud, 0, [DIFFUSE], src="CMDL 0x12345678"),
                record(glow, 1, [DIFFUSE, stage(1, 1, (ZERO, TEXC, KONST, CPREV))], blend=1 | 1 << 16),
                record(pal, 0, [DIFFUSE]),
                record(missing, 0, [DIFFUSE]),
            ]
            manifest = tmp / "texture_manifest.jsonl"
            manifest.write_text("\n".join(json.dumps(r) for r in lines) + "\n{cut sho", encoding="utf-8")
            out = tmp / "report.csv"
            index, skipped = tr.index_pack(pack)
            rows_in_memory = tr.build_report([r for r in lines], index)
            summary = tr.summary(rows_in_memory, index, 0, skipped)
            self.assertEqual(skipped, 0)  # the _mip1 file is a mip level, not a stray file
            # The wildcard-palette file was matched, so only the Sanctuary file is unseen.
            unseen = summary.split("no material in the manifest uses: ")[1]
            self.assertEqual(unseen.splitlines()[0], "Sanctuary 1")
            self.assertEqual(tr.main([str(manifest), str(pack), "-o", str(out)]), 0)
            with open(out, newline="", encoding="utf-8") as f:
                rows = {r["name"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows[wall]["pbr_candidate"], "yes")
        self.assertEqual(rows[wall]["category"], "Agon Wastes")
        self.assertEqual(rows[wall]["uses"], "2")
        self.assertIn("area 3 world model 1", rows[wall]["sources"])
        self.assertEqual(rows[hud]["pbr_candidate"], "no")  # a HUD folder is no surface
        self.assertFalse(tr.is_surface_folder(pathlib.PurePosixPath("Text/tex1_8x8_0000000000000001_5.dds")))
        self.assertEqual(rows[glow]["roles"], "glow")
        self.assertEqual(rows[glow]["additive"], "yes")
        self.assertTrue(rows[pal]["pack_file"].endswith("_$_9.dds"))  # wildcard TLUT entry
        self.assertEqual(rows[missing]["pack_file"], "")
        self.assertEqual(rows[missing]["pbr_candidate"], "no")


if __name__ == "__main__":
    unittest.main(verbosity=2)
