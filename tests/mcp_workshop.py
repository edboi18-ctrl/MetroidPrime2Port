"""tools/mcp/workshop.py (and echoes_mcp.py when the MCP SDK is installed): templates, script data,
the MLVL layer table, the mods folder rules, the console client.

Usage: mcp_workshop.py <repo root>. Standard library only; no disc or game needed: the templates and
data are built here to the layouts PWE's CScriptLoader/CWorldLoader and the port's port_mods.cpp read.
The real PWE templates are also loaded when a PrimeWorldEditor checkout sits beside the repo.
"""
import asyncio
import os
import pathlib
import socket
import struct
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1])
sys.argv = sys.argv[:1]
sys.path.insert(0, str(ROOT / "tools" / "mcp"))
sys.path.insert(0, str(ROOT / "tools"))
import workshop as ws  # noqa: E402

GAME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Game ArchiveVer="4" Game="Echoes">
  <ScriptObjects>
    <Element><Key>THNG</Key><Value Path="Script/Thing.xml"/></Element>
  </ScriptObjects>
  <PropertyArchetypes>
    <Element><Key>EditorProperties</Key><Value Path="Structs/EditorProperties.xml"/></Element>
    <Element><Key>Transform</Key><Value Path="Structs/Transform.xml"/></Element>
    <Element><Key>Mood</Key><Value Path="Enums/Mood.xml"/></Element>
  </PropertyArchetypes>
  <States><Element><Key>ENTR</Key><Value>Entered</Value></Element></States>
  <Messages><Element><Key>ACTV</Key><Value>Activate</Value></Element></Messages>
</Game>"""

EDITOR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PropertyTemplate ArchiveVer="4" Game="Echoes"><PropertyArchetype Type="Struct"><Name>EditorProperties</Name>
<SubProperties>
  <Element Type="String" ID="0x494E414D"/>
  <Element Type="Struct" ID="0x5846524D" Archetype="Transform"/>
  <Element Type="Bool" ID="0x41435456"><DefaultValue>true</DefaultValue></Element>
</SubProperties></PropertyArchetype></PropertyTemplate>"""

TRANSFORM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PropertyTemplate ArchiveVer="4" Game="Echoes"><PropertyArchetype Type="Struct"><Name>Transform</Name>
<Atomic>true</Atomic><SubProperties>
  <Element Type="Vector" ID="0x0"><Name>Position</Name></Element>
  <Element Type="Vector" ID="0x1"><Name>Rotation</Name></Element>
  <Element Type="Vector" ID="0x2"><Name>Scale</Name><DefaultValue><X>1.0</X><Y>1.0</Y><Z>1.0</Z></DefaultValue></Element>
</SubProperties></PropertyArchetype></PropertyTemplate>"""

MOOD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PropertyTemplate ArchiveVer="4" Game="Echoes"><PropertyArchetype Type="Choice"><Name>Mood</Name>
<Values><Element Name="Calm" ID="0x0"/><Element Name="Angry" ID="0x1"/></Values>
</PropertyArchetype></PropertyTemplate>"""

SPEED_ID = ws.property_id("Speed", "float")
MOOD_ID = ws.property_id("CurrentMood", "Mood")
MODEL_ID = ws.property_id("Model", "asset")

THING_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<ScriptObject ArchiveVer="4" Game="Echoes"><Properties Type="Struct"><Name>Thing</Name><SubProperties>
  <Element Type="Struct" ID="0x255A4580" Archetype="EditorProperties">
    <SubProperties><Element Type="Bool" ID="0x41435456"><DefaultValue>false</DefaultValue></Element></SubProperties>
  </Element>
  <Element Type="Float" ID="0x{SPEED_ID:08X}"><DefaultValue>1.0</DefaultValue></Element>
  <Element Type="Choice" ID="0x{MOOD_ID:08X}" Archetype="Mood"><DefaultValue>0</DefaultValue></Element>
  <Element Type="Asset" ID="0x{MODEL_ID:08X}"><TypeFilter><Element>CMDL</Element></TypeFilter></Element>
</SubProperties></Properties></ScriptObject>"""

PROPERTY_MAP_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<PropertyMap ArchiveVer="4"><PropertyMap>
  <Element><Key ID="0x{SPEED_ID:08X}" Type="float"/><Value Name="Speed"/></Element>
  <Element><Key ID="0x{MOOD_ID:08X}" Type="Mood"/><Value Name="CurrentMood"/></Element>
  <Element><Key ID="0x{MODEL_ID:08X}" Type="asset"/><Value Name="Model"/></Element>
  <Element><Key ID="0x494E414D" Type="string"/><Value Name="Name"/></Element>
  <Element><Key ID="0x5846524D" Type="Transform"/><Value Name="Transform"/></Element>
  <Element><Key ID="0x41435456" Type="bool"/><Value Name="Active"/></Element>
  <Element><Key ID="0x255A4580" Type="EditorProperties"/><Value Name="EditorProperties"/></Element>
</PropertyMap></PropertyMap>"""


def write_templates(base: pathlib.Path) -> pathlib.Path:
    mp2 = base / "MP2"
    files = {"Game.xml": GAME_XML, "Structs/EditorProperties.xml": EDITOR_XML,
             "Structs/Transform.xml": TRANSFORM_XML, "Enums/Mood.xml": MOOD_XML, "Script/Thing.xml": THING_XML}
    for rel, text in files.items():
        (mp2 / rel).parent.mkdir(parents=True, exist_ok=True)
        (mp2 / rel).write_text(text, encoding="utf-8")
    (base / "PropertyMap.xml").write_text(PROPERTY_MAP_XML, encoding="utf-8")
    return mp2


def prop(pid: int, data: bytes) -> bytes:
    return struct.pack(">IH", pid, len(data)) + data


def struct_body(*props: bytes) -> bytes:
    return struct.pack(">H", len(props)) + b"".join(props)


def thing_object(editor_id: int, name: str, position, speed: float, mood: int, links=(), extra=()) -> bytes:
    """One THNG as LoadObjectMP2 reads it: FourCC, u16 size, u32 id, u16 links, links, root struct."""
    transform = struct.pack(">9f", *position, 0, 0, 0, 1, 1, 1)
    editor = struct_body(prop(0x494E414D, name.encode() + b"\0"), prop(0x5846524D, transform),
                         prop(0x41435456, b"\1"))
    root = struct_body(prop(0x255A4580, editor), prop(SPEED_ID, struct.pack(">f", speed)),
                       prop(MOOD_ID, struct.pack(">i", mood)), prop(MODEL_ID, struct.pack(">I", 0xFFFFFFFF)), *extra)
    body = struct.pack(">IH", editor_id, len(links))
    for state, message, target in links:
        body += state.encode() + message.encode() + struct.pack(">I", target)
    body += struct.pack(">IH", 0xFFFFFFFF, len(root)) + root
    return b"THNG" + struct.pack(">H", len(body)) + body


def layer(*objects: bytes) -> bytes:
    """An SCLY section: 'SCLY', u8, u32 layer index, u8, u32 count (scly_objects reads count at 10)."""
    return b"SCLY" + b"\1" + struct.pack(">I", 0) + b"\1" + struct.pack(">I", len(objects)) + b"".join(objects)


class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.templates = ws.Templates(write_templates(pathlib.Path(cls.tmp.name)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_property_id_matches_pwe_names(self):
        # Two entries of the real PropertyMap.xml: CRC32(name + type), no final XOR.
        self.assertEqual(ws.property_id("TempleKey5", "int"), 0x000E4BAE)
        self.assertEqual(ws.property_id("MapMoveBackward", "RevolutionControl"), 0x0001F679)

    def test_names_archetypes_and_overrides(self):
        thing = self.templates.object("THNG")
        self.assertEqual([c.label() for c in thing.children], ["EditorProperties", "Speed", "CurrentMood", "Model"])
        self.assertTrue(thing.child(SPEED_ID).name_verified)
        self.assertEqual(thing.child(MOOD_ID).values, {0: "Calm", 1: "Angry"})
        editor = thing.child(0x255A4580)
        self.assertIs(editor.child(0x41435456).default, False)  # the object's override
        self.assertIs(self.templates.archetype("EditorProperties").child(0x41435456).default, True)
        self.assertTrue(editor.child(0x5846524D).atomic)
        self.assertEqual(self.templates.resolve_object("thing"), ("THNG", "Thing"))
        self.assertEqual(self.templates.resolve_object("thng"), ("THNG", "Thing"))
        lines = ws.describe_template(thing, max_depth=3)
        self.assertTrue(any("**Speed** (Float)" in line and "default 1.0" in line for line in lines))
        self.assertTrue(any("values: 0=Calm, 1=Angry" in line for line in lines))
        self.assertTrue(any("asset type CMDL" in line for line in lines))

    def test_decode_layer(self):
        unknown = prop(0x12345678, b"\xAA\xBB\xCC")  # no template: skipped by its size
        section = layer(
            thing_object(0x00280001, "Gate", (1.5, -2.0, 3.0), 2.5, 1, links=[("ENTR", "ACTV", 0x00280002)],
                         extra=[unknown]),
            thing_object(0x04280002, "Light", (0, 0, 0), 1.0, 0))
        objects = ws.parse_layer(section)
        self.assertEqual([o.fourcc for o in objects], ["THNG", "THNG"])
        self.assertEqual(objects[0].links, [("ENTR", "ACTV", 0x00280002)])
        self.assertEqual(objects[1].layer, 1)
        decoder = ws.PropertyDecoder(self.templates)
        entries, _ = decoder.struct(self.templates.object("THNG"), objects[0].properties, 0)
        self.assertEqual(ws.find_value(entries, ws.EDITOR_PROPERTIES, ws.NAME), "Gate")
        transform = ws.find_value(entries, ws.EDITOR_PROPERTIES, ws.TRANSFORM)
        self.assertEqual(ws.find_value(transform, 0), [1.5, -2.0, 3.0])
        mood = next(e for e in entries if e["name"] == "CurrentMood")
        self.assertEqual(mood["value_name"], "Angry")
        self.assertEqual(next(e for e in entries if e["name"] == "Model")["value"], "none")
        self.assertEqual(entries[-1]["name"], "Unknown_12345678")
        changed = [e["name"] for e in ws.prune_defaults(entries)]
        # Speed 2.5 and mood 1 differ; the model is unset; Active (true) differs from THNG's false.
        self.assertEqual(changed, ["EditorProperties", "Speed", "CurrentMood", "Unknown_12345678"])
        second, _ = decoder.struct(self.templates.object("THNG"), objects[1].properties, 0)
        kept = ws.prune_defaults(second)
        self.assertNotIn("Speed", [e["name"] for e in kept])
        self.assertEqual(self.templates.state_name("ENTR"), "Entered")
        self.assertEqual(self.templates.message_name("ACTV"), "Activate")

    def test_layer_table(self):
        tail = (struct.pack(">I", 0xABCDEF01) + b"\0" * 5 + struct.pack(">I", 2)
                + struct.pack(">IQ", 2, 0b01) + struct.pack(">IQ", 1, 0b1)
                + struct.pack(">I", 3) + b"Default\0Boss\0Default\0")
        head = b"\x99" * 7
        self.assertEqual(ws.parse_layer_table(head + tail, len(head), 2),
                         [[("Default", True), ("Boss", False)], [("Default", True)]])


@unittest.skipUnless((ROOT.parent / "PrimeWorldEditor" / "templates" / "MP2" / "Game.xml").is_file(),
                     "no PrimeWorldEditor checkout with templates beside the repo")
class RealTemplateTests(unittest.TestCase):
    def test_every_object_loads(self):
        t = ws.Templates(ROOT.parent / "PrimeWorldEditor" / "templates" / "MP2")
        self.assertGreater(len(t.names_by_fourcc), 150)
        for fourcc in t.names_by_fourcc:
            t.object(fourcc)
        door = t.object(t.resolve_object("Door")[0])
        self.assertEqual(door.child(ws.EDITOR_PROPERTIES).child(ws.TRANSFORM).children[0].name, "Position")


class FakeDisc:
    def __init__(self, resources, files):
        self.by_id = {}
        for rtype, rid, pak in resources:
            self.by_id.setdefault(rid, set()).add((rtype, pak))

        class Iso:
            pass
        self.iso = Iso()
        self.iso.files = {f: (0, 1) for f in files}


class ModsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.mods = self.base / "mods"
        self.disc = FakeDisc([("TXTR", 0x1A2B3C4D, "Metroid1.pak"), ("TXTR", 0x1A2B3C4D, "Metroid2.pak"),
                              ("CMDL", 0x0BADF00D, "Metroid1.pak")], ["Metroid1.pak", "Audio/echoes.dsp"])

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name, data=b"cooked"):
        path = self.base / name
        path.write_bytes(data)
        return str(path)

    def test_loose_names(self):
        # ParseLooseName: 13 characters, 8 hex digits, '.', four letters or digits (any case).
        self.assertEqual(ws.parse_loose_name("1a2b3c4d.txtr"), ("TXTR", 0x1A2B3C4D))
        for bad in ("1A2B3C4.TXTR", "1A2B3C4D.TXT", "1A2B3C4G.TXTR", "1A2B3C4D.TX_R", "1A2B3C4D_TXTR"):
            self.assertIsNone(ws.parse_loose_name(bad), bad)

    def test_install_checks_the_disc(self):
        out = ws.install_resource(self.source("1A2B3C4D.TXTR"), "skin", disc=self.disc, folder=self.mods)
        self.assertTrue((self.mods / "skin" / "1A2B3C4D.TXTR").is_file())
        self.assertIn("Metroid1.pak, Metroid2.pak", out["notes"][0])
        out = ws.install_resource(self.source("whatever.bin"), "skin", "0badf00d", "cmdl", disc=self.disc, folder=self.mods)
        self.assertTrue(out["installed"].endswith("0BADF00D.CMDL"))
        with self.assertRaisesRegex(ws.WorkshopError, "is a TXTR on the disc, not a CMDL"):
            ws.install_resource(self.source("1A2B3C4D.CMDL"), "skin", disc=self.disc, folder=self.mods)
        with self.assertRaisesRegex(ws.WorkshopError, "no PAK holds"):
            ws.install_resource(self.source("DEADBEEF.TXTR"), "skin", disc=self.disc, folder=self.mods)
        forced = ws.install_resource(self.source("DEADBEEF.TXTR"), "skin", disc=self.disc, force=True, folder=self.mods)
        self.assertIn("will ignore it", forced["notes"][0])
        with self.assertRaisesRegex(ws.WorkshopError, "empty"):
            ws.install_resource(self.source("1A2B3C4D.TXTR", b""), "skin", disc=self.disc, folder=self.mods)
        for bad_mod in ("../escape", ".hidden", "a/b", ""):
            with self.assertRaises(ws.WorkshopError, msg=bad_mod):
                ws.install_resource(self.source("1A2B3C4D.TXTR"), bad_mod, disc=self.disc, folder=self.mods)

    def test_scan_follows_the_port(self):
        (self.mods / "a_mod" / "sub").mkdir(parents=True)
        (self.mods / "a_mod" / "sub" / "1a2b3c4d.txtr").write_bytes(b"x")
        (self.mods / "a_mod" / "DEADBEEF.TXTR").write_bytes(b"x")
        (self.mods / "a_mod" / "0BADF00D.CMDL").write_bytes(b"")
        (self.mods / "a_mod" / "metroid1.PAK").write_bytes(b"x")
        (self.mods / "a_mod" / "audio").mkdir()
        (self.mods / "a_mod" / "audio" / "ECHOES.dsp").write_bytes(b"x")
        (self.mods / "a_mod" / "readme.md").write_text("hi")
        (self.mods / "a_mod" / "notes.bin").write_bytes(b"x")
        (self.mods / "a_mod" / ".git").mkdir()
        (self.mods / "a_mod" / ".git" / "1A2B3C4D.TXTR").write_bytes(b"x")
        (self.mods / ".skipped").mkdir()
        mods = ws.scan_mods(self.mods, self.disc)
        self.assertEqual([m["mod"] for m in mods], ["a_mod"])
        status = {f["file"]: f["status"] for f in mods[0]["files"]}
        self.assertEqual(status["sub/1a2b3c4d.txtr"], "replaces it in 2 PAK(s)")
        self.assertTrue(status["DEADBEEF.TXTR"].startswith("ignored: no PAK"))
        self.assertEqual(status["0BADF00D.CMDL"], "ignored: empty")
        self.assertEqual(status["metroid1.PAK"], "replaces disc file Metroid1.pak")
        self.assertEqual(status["audio/ECHOES.dsp"], "replaces disc file Audio/echoes.dsp")
        self.assertEqual(status["readme.md"], "ignored (documentation)")
        self.assertTrue(status["notes.bin"].startswith("ignored: not a disc file"))
        self.assertNotIn(".git/1A2B3C4D.TXTR", status)

    def test_remove_stays_inside_the_mod(self):
        ws.install_resource(self.source("1A2B3C4D.TXTR"), "skin", disc=self.disc, folder=self.mods)
        (self.base / "outside.txt").write_text("keep")
        with self.assertRaises(ws.WorkshopError):
            ws.remove_mod_file("skin", "../../outside.txt", folder=self.mods)
        self.assertTrue((self.base / "outside.txt").exists())
        ws.remove_mod_file("skin", "1A2B3C4D.TXTR", folder=self.mods)
        self.assertFalse((self.mods / "skin" / "1A2B3C4D.TXTR").exists())


class ConsoleTests(unittest.TestCase):
    def test_one_reply_per_part(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        received = []

        def serve():
            client, _ = listener.accept()
            with client:
                data = b""
                while data.count(b"\n") < 2:
                    data += client.recv(4096)
                received.append(data)
                for line in data.decode().splitlines():
                    for part in line.split(";"):
                        client.sendall(f"ran {part.strip()}\n=> ok\n".encode())
        thread = threading.Thread(target=serve)
        thread.start()
        reply = ws.console(["status", "heal; god 1", "  "], port=port, timeout=5)
        thread.join(5)
        listener.close()
        self.assertEqual(received[0], b"status\nheal; god 1\n")
        self.assertEqual(reply.count("=> ok"), 3)
        self.assertIn("ran god 1", reply)

    def test_refused_says_what_to_do(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaisesRegex(ws.WorkshopError, "Nothing is listening"):
            ws.console(["status"], port=port, timeout=2)


def be(*words):
    return struct.pack(">%dI" % len(words), *words)


def strg(text):
    """A v1 STRG of one string, as echoes_disc.strg_strings reads it (offsets from the offset table)."""
    return be(0x87654321, 1, 1, 1) + b"ENGL" + be(0, 0) + be(0, 0) + be(4) + text.encode("utf-16-be") + b"\0\0"


def mlvl(world_strg, area_strg, mrea_id, layer_names):
    """An MLVL (0x17) of one area, as echoes_disc.parse_mlvl reads it, then the layer table that
    CWorldLoader::LoadPrimeMLVL reads past the areas."""
    body = be(0xDEAFBABE, 0x17, world_strg, 0x12, 0x13, 0x14, 0x15) + be(1)
    body += be(area_strg) + b"\0" * (48 + 24) + be(mrea_id, 0)
    body += be(0) + be(0) + be(0) + be(0) + be(0) + be(0) + be(0) + b"01_test_hall\0"
    body += be(0xABCDEF01) + b"\0" * 5 + be(1) + struct.pack(">IQ", len(layer_names), 1)
    body += be(len(layer_names)) + b"".join(n.encode() + b"\0" for n in layer_names)
    return body


def mrea(*layers, collision=None):
    """A version-0x19 MREA: a stand-in geometry section, the script layers, then the collision
    section if given (header word 20 names it), each section in a stored block of its own (the layout
    echoes_disc.mrea_layers and workshop.mrea_section read)."""
    parts = (b"\xEE" * 32,) + layers + ((collision,) if collision is not None else ())
    sections = [s + b"\0" * (-len(s) % 32) for s in parts]
    h = [0] * 32
    h[0], h[1], h[15], h[16], h[18], h[28] = 0xDEADBEEF, 0x19, len(layers), len(sections), 1, len(sections)
    if collision is not None:
        h[20] = len(sections) - 1
    body = be(*h) + be(*map(len, sections))
    body += b"\0" * (-len(body) % 32)
    body += b"".join(be(len(sec), len(sec), 0, 1) for sec in sections)
    body += b"\0" * (-len(body) % 32)
    return body + b"".join(sections)


def pak(named, resources):
    """A 0x30005 PAK as echoes_disc.Pak reads it: named resources, then the table, then the data."""
    head = be(0x30005, 0, len(named))
    for name, (rtype, rid) in named.items():
        head += rtype.encode() + be(rid, len(name)) + name.encode()
    table_size = 4 + 20 * len(resources)
    offset = len(head) + table_size
    offset += -offset % 32
    table, data = be(len(resources)), b""
    for rid, (rtype, blob) in resources.items():
        table += be(0) + rtype.encode() + be(rid, len(blob), offset + len(data))
        data += blob + b"\0" * (-len(blob) % 32)
    out = head + table
    return out + b"\0" * (-len(out) % 32) + data


def gc_disc(files):
    """A GameCube image: the FST at 0x424 (echoes_disc.Iso), the files after it."""
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
    head = bytearray(fst_off)
    head[0:6] = b"G2ME01"
    struct.pack_into(">II", head, 0x424, fst_off, count * 12 + len(names))
    image = bytes(head) + entries + names
    return image + b"\0" * (data_off - len(image)) + body


def write_disc(base: pathlib.Path) -> str:
    room = mrea(layer(thing_object(0x00280001, "Gate", (1.5, -2.0, 3.0), 2.5, 1, links=[("ENTR", "ACTV", 0x00280002)]),
                      thing_object(0x00280002, "Light", (0, 0, 0), 1.0, 0)), collision=collision_section())
    resources = {0x900: ("MLVL", mlvl(0x11, 0x2000, 0x800, ["Default"])), 0x800: ("MREA", room),
                 0x11: ("STRG", strg("Test World")), 0x2000: ("STRG", strg("Test Hall")),
                 0x1A2B3C4D: ("TXTR", b"\x01" * 64)}
    image = gc_disc({"Metroid1.pak": pak({"test_world": ("MLVL", 0x900)}, resources), "Audio/a.dsp": b"x"})
    path = base / "test.iso"
    path.write_bytes(image)
    return str(path)


FLOOR_STONE = (1 << 31) | (1 << 1)  # kMT_Floor | kMT_Stone


def collision_section(neighbors=(0xFFFF, 0xFFFF, 1, 0xFFFF, 0xFFFF, 0)):
    """A unit floor quad as two triangles, in the layout CGameArea::PostConstructArea and
    CAreaOctTree::MakeFromMemory read: (0,1,2) on edges 0-1, 1-2, 2-0, and (2,3,0) on edges 2-3,
    3-0, 0-2 (stored 2->0), both unflipped; one leaf holds both."""
    vertices = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    edges = [(0, 1), (1, 2), (2, 0), (2, 3), (3, 0)]
    surface = [0, 1, 2, 3, 4, 2]
    tree = struct.pack(">6f", 0, 0, 0, 1, 1, 0) + struct.pack(">HHHH", 2, 0, 1, 0)  # count, tris, pad
    block = be(0xDEAFBABE, 4) + struct.pack(">6f", 0, 0, 0, 1, 1, 0) + be(2, len(tree)) + tree
    block += be(1) + struct.pack(">Q", FLOOR_STONE)
    block += be(4) + bytes(4) + be(5) + bytes(5) + be(2) + bytes(2)
    block += be(len(edges)) + b"".join(struct.pack(">HH", *e) for e in edges)
    block += be(len(surface)) + struct.pack(">6H", *surface)
    block += be(len(neighbors)) + struct.pack(">6H", *neighbors)
    block += be(len(vertices)) + b"".join(struct.pack(">3f", *v) for v in vertices)
    return be(0, len(block)) + block


class CollisionTests(unittest.TestCase):
    def test_report_of_a_well_formed_quad(self):
        report = ws.collision_report(ws.parse_area_collision(collision_section()))
        self.assertEqual(report["triangles"], 2)
        self.assertEqual((report["vertices"], report["edges"]), (4, 5))
        self.assertEqual(report["materials"][0]["names"], ["Stone", "Floor"])
        self.assertEqual(report["materials"][0]["triangles"], 2)
        self.assertEqual(report["edge_slots_follow_corners"], "2/2")
        self.assertEqual(report["neighbours_share_their_edge"], "2/2 (and 4 with none)")
        self.assertEqual(report["octree"]["leaves"], 1)
        self.assertEqual(report["octree"]["triangles_reached"], "2/2")
        self.assertEqual(report["bytes_left_over"], 0)
        self.assertFalse(report["any_material_has_solid_bit"])

    def test_report_catches_a_bad_neighbour(self):
        # Triangle 0's first edge (0-1) is not one of triangle 1's.
        report = ws.collision_report(ws.parse_area_collision(collision_section((1, 0xFFFF, 1, 0xFFFF, 0xFFFF, 0))))
        self.assertEqual(report["neighbours_share_their_edge"], "2/3 (and 3 with none)")

    def test_section_of_an_mrea(self):
        room = mrea(layer(), collision=collision_section())
        self.assertEqual(ws.mrea_section(room, 2)[:8], collision_section()[:8])


class GeometryTests(unittest.TestCase):
    """tests/data/pwe_cooked_room.MREA is a room PrimeWorldEditor cooked with its geometry and
    collision replaced (PrimeWorldEditor tests/AreaCookerTests.cpp, `pwe_area_cooker_tests --dump`):
    one floor model, synthetic, no game content. Reading it here checks the editor's writers against
    this independent reader of the game's layouts."""

    def setUp(self):
        self.room = (ROOT / "tests" / "data" / "pwe_cooked_room.MREA").read_bytes()

    def test_geometry_walk(self):
        r = ws.geometry_report(self.room)
        self.assertTrue(r["walk_reaches_script_layers"])
        self.assertEqual((r["world_models"], r["surfaces"], r["area_surfaces"]), (1, 1, 2))
        self.assertEqual(r["display_list_vertex_formats"], {"as expected": 1})
        self.assertEqual(r["area_surface_0"], {"model": -1, "group": -1})
        self.assertEqual(r["record_area_surface_belongs_to_its_model"], "1/1")
        self.assertTrue(r["render_octree"]["header_index_not_minus_1"])
        self.assertEqual(r["render_octree"]["mesh_count"], 1)

    def test_collision_of_the_cooked_room(self):
        sections = ws.mrea_sections(self.room)
        header = struct.unpack_from(">32I", self.room, 0)
        self.assertEqual(len(sections), header[16])
        report = ws.collision_report(ws.parse_area_collision(sections[header[20]]))
        self.assertEqual(report["edge_slots_follow_corners"], "2/2")
        self.assertEqual(report["bytes_left_over"], 0)
        self.assertEqual(report["materials"][0]["names"], ["Stone", "Floor"])


class DiscTests(unittest.TestCase):
    def test_world_area_layers_objects(self):
        with tempfile.TemporaryDirectory() as tmp, ws.Disc(write_disc(pathlib.Path(tmp))) as d:
            worlds = d.worlds()
            self.assertEqual([(w["name"], w["mlvl"]) for w in worlds], [("Test World", 0x900)])
            world = d.world("test world")
            self.assertIs(d.world("00000900"), world)
            area = d.area(world, "Test Hall")
            self.assertIs(d.area(world, "0"), area)
            self.assertIs(d.area(world, "01_test_hall"), area)
            self.assertEqual(area["layers"], [("Default", True)])
            layers = d.layers(area)
            self.assertEqual([[o.editor_id for o in objs] for objs in layers], [[0x00280001, 0x00280002]])
            self.assertEqual(sorted(d.by_id[0x1A2B3C4D]), [("TXTR", "Metroid1.pak")])
            with self.assertRaisesRegex(ws.WorkshopError, "No single world"):
                d.world("Sanctuary")

    def test_rvz_is_refused_with_a_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "game.rvz"
            path.write_bytes(b"RVZ\1")
            with self.assertRaisesRegex(ws.WorkshopError, "convert"):
                ws.Disc(str(path))


try:
    import echoes_mcp  # noqa: E402
except ImportError:
    echoes_mcp = None


@unittest.skipIf(echoes_mcp is None, "the MCP SDK (tools/mcp/requirements.txt) is not installed")
class ServerTests(unittest.TestCase):
    def test_tools_and_a_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ECHOES_TEMPLATES"] = str(write_templates(pathlib.Path(tmp)))
            echoes_mcp._state.clear()

            async def run():
                names = {tool.name for tool in await echoes_mcp.mcp.list_tools()}
                explained = await echoes_mcp.mcp.call_tool("echoes_explain_template", {"name": "THNG"})
                return names, explained
            names, explained = asyncio.run(run())
            echoes_mcp._state.clear()
        self.assertTrue({"echoes_explain_template", "echoes_disc_describe_area", "echoes_mods_install_resource",
                         "echoes_game_launch", "echoes_game_command"} <= names)
        text = explained.content[0].text
        self.assertIn("# Thing (`THNG`)", text)
        self.assertIn("**CurrentMood** (Choice: Mood)", text)

    def test_disc_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            os.environ["ECHOES_TEMPLATES"] = str(write_templates(base / "templates"))
            os.environ["ECHOES_DISC"] = write_disc(base)
            os.environ["ECHOES_MODS"] = str(base / "mods")
            (base / "1A2B3C4D.TXTR").write_bytes(b"new texture")
            echoes_mcp._state.clear()

            async def call(name, args):
                return (await echoes_mcp.mcp.call_tool(name, args)).content[0].text
            calls = [("echoes_disc_list_worlds", {}),
                     ("echoes_disc_list_areas", {"world": "Test World"}),
                     ("echoes_disc_describe_area", {"world": "Test World", "area": "Test Hall"}),
                     ("echoes_disc_describe_object", {"world": "Test World", "area": "0", "editor_id": "e00280001"}),
                     ("echoes_disc_find_resource", {"asset_id": "1a2b3c4d"}),
                     ("echoes_disc_room_collision", {"world": "Test World", "area": "Test Hall"}),
                     ("echoes_mods_install_resource", {"source_path": str(base / "1A2B3C4D.TXTR"), "mod": "first"}),
                     ("echoes_mods_list", {})]

            async def run():
                return [await call(name, args) for name, args in calls]
            worlds, areas, room, obj, found, collision, installed, listed = asyncio.run(run())
            echoes_mcp._state.pop("disc").close()
            echoes_mcp._state.clear()
            for key in ("ECHOES_DISC", "ECHOES_MODS"):
                os.environ.pop(key)
        self.assertIn("**Test World** - MLVL `00000900` in Metroid1.pak, 1 areas", worlds)
        self.assertIn("[0] **Test Hall** `01_test_hall` MREA `00000800` - layers 0:Default", areas)
        self.assertIn("- `00280001` Thing **Gate** at (1.5, -2, 3)", room)
        self.assertIn("when Entered: Activate -> `00280002` Light", room)
        self.assertIn('"meaning": "Angry"', obj)
        self.assertIn('"value": 2.5', obj)
        self.assertNotIn('"Model"', obj)  # still at its default, so not shown
        self.assertIn("TXTR in Metroid1.pak", found)
        self.assertIn("replaces TXTR 0x1A2B3C4D in Metroid1.pak", installed)
        self.assertIn("replaces it in 1 PAK(s)", listed)
        self.assertIn("# Collision of Test Hall (`01_test_hall`)", collision)
        self.assertIn('"edge_slots_follow_corners": "2/2"', collision)

    def test_errors_reach_the_caller(self):
        os.environ.pop("ECHOES_DISC", None)
        os.environ.pop("MP2_DISC", None)
        echoes_mcp._state.clear()
        with self.assertRaisesRegex(Exception, "No disc image is configured"):
            asyncio.run(echoes_mcp.mcp.call_tool("echoes_disc_list_worlds", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
