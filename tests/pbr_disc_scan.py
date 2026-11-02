"""tools/pbr/disc_texture_scan.py: XXH64, texture names, material/CMDL/MREA parsing, a whole scan.

Usage: pbr_disc_scan.py <repo root>. Standard library only; no disc needed: the structures are built
here to the engine's layouts (CCubeMaterial::SetCurrent, DolphinCModel.cpp, CGameArea).
"""
import json
import pathlib
import struct
import sys
import unittest

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
sys.argv = sys.argv[:1]
sys.path.insert(0, str(ROOT / "tools" / "pbr"))
sys.path.insert(0, str(ROOT / "tools"))
import disc_texture_scan as ds  # noqa: E402
import texture_report as tr  # noqa: E402


def be(*words):
    return struct.pack(">%dI" % len(words), *words)


def pattern(n):
    return bytes((i * 31 + 7) & 0xFF for i in range(n))


class Xxh64(unittest.TestCase):
    # Computed with the reference xxhash library (4.0.2) for pattern(n), seed 0.
    VECTORS = {0: 0xef46db3751d8e999, 1: 0xa96c7f0ce858bbb7, 3: 0x56e6957632a487f9,
               4: 0xc60d15b1e3ff8f04, 7: 0xafbefc3d6c6f9a8e, 8: 0x3da5c7aa269683e0,
               15: 0xae2a37eb9357caa7, 31: 0x4a74f3a1a39ad4a1, 32: 0x8d57d6a4671cc43d,
               33: 0x62c9fd21ed857664, 63: 0x5c320a0d2707057f, 64: 0x7bbabbc45729d17e,
               100: 0xefa0ad2d3e70c151, 1000: 0x99594f4828043d35}

    def test_reference_vectors(self):
        for n, want in self.VECTORS.items():
            self.assertEqual(ds.xxh64_pure(pattern(n)), want, n)
            self.assertEqual(ds.xxh64(pattern(n)), want, n)  # the C module too, when installed


class Textures(unittest.TestCase):
    def test_buffer_sizes(self):
        # GXGetTexBufferSize: whole tiles of 32 bytes (64 for RGBA8).
        self.assertEqual(ds.gx_base_size(64, 64, 0xE), 2048)  # CMPR 8x8 tiles, 4 bpp
        self.assertEqual(ds.gx_base_size(4, 4, 0xE), 32)      # a partial tile is a whole tile
        self.assertEqual(ds.gx_base_size(8, 8, 0x1), 64)      # I8 8x4 tiles
        self.assertEqual(ds.gx_base_size(8, 8, 0x4), 128)     # RGB565 4x4 tiles, 16 bpp
        self.assertEqual(ds.gx_base_size(4, 4, 0x6), 64)      # RGBA8
        self.assertEqual(ds.gx_base_size(16, 16, 0x8), 128)   # C4

    def test_plain_texture(self):
        image = pattern(128)
        txtr = struct.pack(">IHHI", 7, 8, 8, 1) + image + b"\x00" * 32  # RGB565, then other mips
        name, texel = ds.texture_name(txtr)
        self.assertEqual(texel, 7)
        self.assertEqual(name, "tex1_8x8_%016x_4.dds" % ds.xxh64_pure(image))

    def test_paletted_texture_hashes_the_used_range(self):
        image = bytes([3, 4, 5, 4] * 16)  # C8 8x8 uses entries 3..5
        palette = bytes(range(32))         # 16 big-endian entries
        txtr = struct.pack(">IHHI", 5, 8, 8, 1) + struct.pack(">IHH", 2, 16, 1) + palette + image
        name, texel = ds.texture_name(txtr)
        self.assertEqual(texel, 5)
        self.assertEqual(name, "tex1_8x8_%016x_%016x_9.dds"
                         % (ds.xxh64_pure(image), ds.xxh64_pure(palette[6:12])))
        # C4: both nibbles count.
        self.assertEqual(ds.referenced_palette_range(bytes([0x21, 0x9F]), 0x8), (1, 15))

    def test_short_or_unknown(self):
        self.assertIsNone(ds.texture_name(struct.pack(">IHHI", 7, 8, 8, 1) + pattern(10))[0])
        self.assertIsNone(ds.texture_name(struct.pack(">IHHI", 42, 8, 8, 1) + pattern(512))[0])


def material(flags, textures, blend, stages, texgens, konst=(), chans=(1,), ind_slot=None):
    """A material in the layout CCubeMaterial::SetCurrent reads. stages: (colorIn, alphaIn, colorOp,
    alphaOp, chan | kcsel << 8 | kasel << 16, texcoord | texmap << 8)."""
    out = be(flags, len(textures), *textures)
    out += be(0x11111111, 0, 0, 7)  # vertex description, two words, the group
    if konst:
        out += be(len(konst), *konst)
    out += be(blend)
    if ind_slot is not None:
        out += be(ind_slot)
    out += be(len(chans), *chans)
    out += be(len(stages))
    for s in stages:
        out += be(*s[:5])
    for s in stages:
        out += be(s[5])
    out += be(len(texgens), *texgens)
    out += be(8, 0)  # UV animation: size and count, which the parser doesn't need
    return out


def material_set(txtr_ids, materials):
    ends, total = [], 0
    for m in materials:
        total += len(m)
        ends.append(total)
    return be(len(txtr_ids), *txtr_ids) + be(len(materials), *ends) + b"".join(materials)


ARGS = lambda a, b, c, d: a | b << 5 | c << 10 | d << 15  # noqa: E731


class Materials(unittest.TestCase):
    def test_layout_with_konst_and_indirect(self):
        stage0 = (ARGS(15, 8, 10, 15), ARGS(7, 7, 7, 6), 0x100, 0x100, 0x04 | 12 << 8 | 28 << 16, 0 | 0 << 8)
        stage1 = (ARGS(15, 8, 0, 15), ARGS(7, 7, 7, 0), 0x100, 0x100, 0xFF | 13 << 8 | 29 << 16, 1 | 1 << 8)
        data = material(ds.FLAG_KONST | ds.FLAG_INDIRECT, [2, 5], 0x00010004, [stage0, stage1],
                        [4 << 4, 1 << 4], konst=(0xFFFFFFFF, 0x80808080), chans=(3, 0x10, 0x20),
                        ind_slot=1)
        mat = ds.parse_material(data)
        self.assertEqual(mat["flags"], ds.FLAG_KONST | ds.FLAG_INDIRECT)
        self.assertEqual(mat["textures"], [2, 5])
        self.assertEqual(mat["blend"], 0x00010004)
        self.assertEqual(mat["texgens"], [4 << 4, 1 << 4])
        # The manifest's form: [texcoord | texmap << 8 | channel << 16, colorIn, alphaIn, colorOp,
        # alphaOp, kcolor select, kalpha select].
        self.assertEqual(mat["stages"][0], [0 | 0 << 8 | 0x04 << 16, stage0[0], stage0[1], 0x100, 0x100, 12, 28])
        self.assertEqual(mat["stages"][1], [1 | 1 << 8 | 0xFF << 16, stage1[0], stage1[1], 0x100, 0x100, 13, 29])

    def test_set_and_bad_data(self):
        m0 = material(0, [0], 1, [(ARGS(15, 8, 10, 15), 0, 0x100, 0x100, 0, 0)], [4 << 4])
        m1 = material(0, [1, 0], 1, [], [])
        ids, mats = ds.parse_material_set(material_set([0xAAAA, 0xBBBB], [m0, m1]))
        self.assertEqual(ids, [0xAAAA, 0xBBBB])
        self.assertEqual(mats, [m0, m1])
        with self.assertRaises(ValueError):
            ds.parse_material(be(0, 99))  # an impossible texture count
        with self.assertRaises(struct.error):
            ds.parse_material(m0[:20])     # cut short


def cmdl(sets, geometry=b"\xEE" * 64):
    sections = list(sets) + [geometry]
    padded = [s + b"\0" * (-len(s) % 32) for s in sections]
    head = b"\xDE\xAD\xBA\xBE" + be(2, 0) + b"\0" * 24 + be(len(padded), len(sets)) + be(*map(len, padded))
    head += b"\0" * (-len(head) % 32)
    return head + b"".join(padded), padded


class Containers(unittest.TestCase):
    def test_cmdl_sets_and_prefix(self):
        set_a = material_set([1], [material(0, [0], 1, [], [])])
        data, padded = cmdl([set_a, set_a])
        sets = ds.cmdl_material_sets(data)
        self.assertEqual(len(sets), 2)
        self.assertEqual(sets[0][:len(set_a)], set_a)
        need = ds.cmdl_prefix_needed(data)
        self.assertEqual(need, len(data) - len(padded[2]))  # everything but the geometry
        self.assertIsNone(ds.cmdl_prefix_needed(data[:0x20]))

    def test_inflate_prefix_stops_early(self):
        data, _ = cmdl([material_set([1], [material(0, [0], 1, [], [])])], geometry=b"\xEE" * 4096)
        # Stored chunks (a negative size), 64 bytes each.
        chunks = b"".join(struct.pack(">h", -len(data[i:i + 64])) + data[i:i + 64]
                          for i in range(0, len(data), 64))
        out = ds.inflate_prefix(chunks, len(data), 0, ds.cmdl_prefix_needed)
        self.assertLess(len(out), len(data))
        self.assertEqual(ds.cmdl_material_sets(out)[0], ds.cmdl_material_sets(data)[0])

    def test_mrea_material_set(self):
        mset = material_set([7], [material(0, [0], 1, [], [])])
        mset += b"\0" * (-len(mset) % 32)
        sections = [b"\x01" * 32, mset, b"\x02" * 64]  # geometry starts at section 1
        h = [0] * 32
        h[0], h[1], h[16], h[17], h[28] = 0xDEADBEEF, 0x19, len(sections), 1, 2
        body = be(*h) + be(*map(len, sections))
        body += b"\0" * (-len(body) % 32)
        blocks = [(32, 32, 0, 1), (sum(map(len, sections[1:])), sum(map(len, sections[1:])), 0, 2)]
        body += b"".join(be(*b) for b in blocks)
        body += b"\0" * (-len(body) % 32)
        body += b"".join(sections)
        self.assertEqual(ds.mrea_material_set(body), mset)


class FakePak:
    def __init__(self, path, resources):
        self.path = path
        self.res = {rid: (t, 0, len(data), 0) for rid, (t, data) in resources.items()}
        self.data = {rid: data for rid, (t, data) in resources.items()}

    def get(self, rid):
        return self.data[rid]


class WholeScan(unittest.TestCase):
    def test_scan_and_report(self):
        wall = struct.pack(">IHHI", 7, 8, 8, 1) + pattern(128)
        lightmap = struct.pack(">IHHI", 7, 8, 8, 1) + pattern(129)[1:]
        lit = (ARGS(15, 8, 14, 15), ARGS(7, 7, 7, 6), 0x100, 0x100, 0, 0 | 0 << 8)
        times_prev = (ARGS(15, 8, 0, 15), ARGS(7, 7, 7, 6), 0x100, 0x100, 0, 1 | 1 << 8)
        world = material(1 << 11, [1, 0], 1, [lit, times_prev], [4 << 4, 5 << 4])
        mset = material_set([0x100, 0x200], [world])
        data, _ = cmdl([mset])
        pak = FakePak("Metroid1.pak", {0x100: ("TXTR", wall), 0x200: ("TXTR", lightmap),
                                       0x300: ("CMDL", data), 0x400: ("TXTR", b"\0" * 4)})
        scanner = ds.Scanner([pak])
        records = list(scanner.scan())
        self.assertEqual(len(records), 2)
        roles = {r["slot"]: tr.classify(json.loads(json.dumps(r))) for r in records}
        self.assertEqual(roles, {0: "lightmap", 1: "diffuse"})
        self.assertEqual(records[1]["name"], ds.texture_name(wall)[0])
        self.assertEqual(records[0]["src"], "CMDL 0x00000300")
        self.assertEqual(scanner.stats["cmdl"], 1)


def mlvl(skybox, mrea_ids, version=0x17):
    """An MLVL in the layout CWorld::Update and echoes_disc.parse_mlvl read: no relays (version
    0x17 has them only at 17), areas with no attached objects, docks or modules."""
    body = be(0xDEAFBABE, version, 0x11, 0x12, 0x13, 0x14, skybox)  # STRG, dark STRG, temple, SAVW
    body += be(len(mrea_ids))
    for i, mrea in enumerate(mrea_ids):
        body += be(0x20 + i) + b"\0" * (48 + 24) + be(mrea, i)
        body += be(0) + be(0) + be(0) + be(0)  # attached areas, two 8-byte lists, a 4-byte list
        body += be(0) + be(0) + be(0) + ("area_%d" % i).encode() + b"\0"  # docks, modules, offsets
    return body


def mrea(material_section, layer):
    """A version-0x19 MREA: section 0 the world geometry's material set, section 1 one script
    layer, each in a stored (uncompressed) block of its own."""
    sections = [s + b"\0" * (-len(s) % 32) for s in (material_section, layer)]
    h = [0] * 32
    h[0], h[1], h[15], h[16], h[17], h[18], h[28] = 0xDEADBEEF, 0x19, 1, 2, 0, 1, 2
    body = be(*h) + be(*map(len, sections))
    body += b"\0" * (-len(body) % 32)
    body += b"".join(be(len(sec), len(sec), 0, 1) for sec in sections)
    body += b"\0" * (-len(body) % 32)
    return body + b"".join(sections)


def scly(objects):
    """A script layer: 'SCLY', 6 bytes, a count, then per object FourCC, u16 length, editor id, no
    connections, properties (CScriptObjectLoaderHelper)."""
    out = b"SCLY" + b"\0" * 6 + be(len(objects))
    for i, (fourcc, props) in enumerate(objects):
        body = be(0x1000 + i) + struct.pack(">H", 0) + props
        out += fourcc + struct.pack(">H", len(body)) + body
    return out


def props(*entries):
    """A property struct: u16 count, then u32 id, u16 size, data per entry (SLdrAreaAttributes.inc)."""
    out = struct.pack(">H", len(entries))
    for pid, data in entries:
        out += be(pid) + struct.pack(">H", len(data)) + data
    return out


class Sky(unittest.TestCase):
    def test_world_and_area_skies(self):
        wall = struct.pack(">IHHI", 7, 8, 8, 1) + pattern(128)
        clouds = struct.pack(">IHHI", 7, 8, 8, 1) + pattern(130)[2:]  # distinct hashed bytes
        stars = struct.pack(">IHHI", 7, 8, 8, 1) + pattern(133)[5:]
        plain = (ARGS(15, 8, 10, 15), ARGS(7, 7, 7, 6), 0x100, 0x100, 0, 0)
        def model(txtr):
            return cmdl([material_set([txtr], [material(0, [0], 1, [plain], [4 << 4])])])[0]
        reaa = props((0x95D4BEE7, b"\1"), (0xD208C9FA, be(0x600)), (0xBA5F801E, be(0x3F800000)))
        layer = scly([(b"ACTR", props((0xD208C9FA, be(0x700)))),  # not an AreaAttributes: ignored
                      (b"REAA", reaa)])
        area = mrea(material_set([0x100], [material(0, [0], 1, [plain], [4 << 4])]), layer)
        pak = FakePak("Metroid1.pak", {
            0x100: ("TXTR", wall), 0x110: ("TXTR", clouds), 0x120: ("TXTR", stars),
            0x300: ("CMDL", model(0x100)), 0x500: ("CMDL", model(0x110)), 0x600: ("CMDL", model(0x120)),
            0x700: ("CMDL", model(0x100)), 0x800: ("MREA", area), 0x900: ("MLVL", mlvl(0x500, [0x800]))})
        scanner = ds.Scanner([pak])
        records = list(scanner.scan())
        self.assertEqual(scanner.sky, {0x500, 0x600})
        by_src = {r["src"]: r for r in records}
        self.assertEqual(set(by_src), {"area area_0 (MREA 0x00000800)", "CMDL 0x00000300", "CMDL 0x00000700",
                                       "CMDL 0x00000500 (sky)", "CMDL 0x00000600 (sky)"})
        self.assertTrue(by_src["CMDL 0x00000500 (sky)"]["sky"] and by_src["CMDL 0x00000600 (sky)"]["sky"])
        self.assertNotIn("sky", by_src["CMDL 0x00000300"])
        self.assertEqual(tr.classify(by_src["CMDL 0x00000500 (sky)"]), "sky")
        self.assertEqual(tr.classify(by_src["CMDL 0x00000300"]), "diffuse")
        rows = tr.build_report(records, {})
        self.assertEqual({r["name"]: r["roles"] for r in rows}[ds.texture_name(clouds)[0]], "sky")

    def test_no_sky(self):
        self.assertEqual(ds.area_sky_models(mrea(b"", scly([(b"REAA", props((0xD208C9FA, be(0xFFFFFFFF))))]))),
                         set())


def gc_disc(files):
    """A minimal GameCube disc image: the header's magic, the FST at 0x424 and `files` after it."""
    names = b"".join(name.encode() + b"\0" for name in files)
    count = len(files) + 1
    fst_off = 0x2440
    data_off = (fst_off + count * 12 + len(names) + 0x7FFF) & ~0x7FFF
    entries = struct.pack(">III", 1 << 24, 0, count)
    name_off, pos, body = 0, data_off, b""
    for name, data in files.items():
        entries += struct.pack(">III", name_off, pos, len(data))
        name_off += len(name) + 1
        body += data + b"\0" * (-len(data) % 0x8000)
        pos += len(data) + (-len(data) % 0x8000)
    fst = entries + names
    head = bytearray(fst_off)
    head[0:6] = b"G2ME01"
    struct.pack_into(">I", head, 0x1C, ds.GC_MAGIC)
    struct.pack_into(">II", head, 0x424, fst_off, len(fst))
    image = bytes(head) + fst
    return image + b"\0" * (data_off - len(image)) + body


def ciso(plain, block_size=0x400):
    """`plain` as a CISO (Dolphin's layout): all-zero blocks are left out."""
    blocks = [plain[i:i + block_size] for i in range(0, len(plain), block_size)]
    blocks[-1] += b"\0" * (block_size - len(blocks[-1]))
    used = [any(b) for b in blocks]
    header = bytearray(0x8000)
    header[0:4] = b"CISO"
    struct.pack_into("<I", header, 4, block_size)
    for i, u in enumerate(used):
        header[8 + i] = 1 if u else 0
    return bytes(header) + b"".join(b for b, u in zip(blocks, used) if u)


class DiscImages(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.files = {"Metroid1.pak": pattern(5000), "Strings.pak": b"\x07" * 300 + bytes(4096) + b"x"}
        self.plain = gc_disc(self.files)
        (self.dir / "g.iso").write_bytes(self.plain)
        (self.dir / "g.ciso").write_bytes(ciso(self.plain))

    def tearDown(self):
        self.tmp.cleanup()

    def test_ciso_reads_as_the_plain_disc(self):
        import echoes_disc as ed
        disc = ed.open_disc(str(self.dir / "g.ciso"))
        self.assertIsInstance(disc, ed.CisoFile)
        self.assertEqual(disc.block_size, 0x400)
        for pos, n in ((0, 0x40), (0x3F0, 0x30), (0x424, 8), (0x7FFF, 3), (0x8000, 5000),
                       (len(self.plain) - 10, 10), (0x2000, 0x900)):
            disc.seek(pos)
            self.assertEqual(disc.read(n), self.plain[pos:pos + n], (pos, n))
        disc.close()
        # Both images list the same files with the same bytes.
        plain_iso, ciso_iso = ed.Iso(str(self.dir / "g.iso")), ed.Iso(str(self.dir / "g.ciso"))
        self.addCleanup(plain_iso.f.close)
        self.addCleanup(ciso_iso.f.close)
        self.assertEqual(plain_iso.files, ciso_iso.files)
        for name, data in self.files.items():
            self.assertEqual(ciso_iso.read(name), data)
            self.assertEqual(plain_iso.read(name), data)

    def test_disc_problem(self):
        self.assertIsNone(ds.disc_problem(str(self.dir / "g.iso")))
        self.assertIsNone(ds.disc_problem(str(self.dir / "g.ciso")))
        (self.dir / "a.rvz").write_bytes(b"RVZ\x01" + bytes(64))
        self.assertIn("RVZ", ds.disc_problem(str(self.dir / "a.rvz")))
        wii = bytearray(self.plain[:0x40])
        struct.pack_into(">II", wii, 0x18, ds.WII_MAGIC, 0)
        (self.dir / "w.ciso").write_bytes(ciso(bytes(wii) + bytes(0x400)))
        self.assertIn("Wii", ds.disc_problem(str(self.dir / "w.ciso")))
        (self.dir / "bad.ciso").write_bytes(b"CISO" + bytes(16))
        self.assertIn("not a CISO", ds.disc_problem(str(self.dir / "bad.ciso")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
