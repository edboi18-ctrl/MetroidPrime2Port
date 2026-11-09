"""What the Material Editor's 3D preview draws (viewer.py draws it): a model's or an area's triangles
by material (geometry.py), each material's textures (the texture pack's when it has them, else the
disc's), and each material's settings as the game applies them (params).

Pillow, when present, reads the texture pack (PNG, and DDS including BC5 and BC7) and so the PBR maps;
without it the preview shows the disc's textures and no PBR maps.
"""

import struct

import geometry
import material_core as mc

try:
    from PIL import Image
except ImportError:  # the preview still works, without the pack's textures and PBR maps
    Image = None

DISC_TEXTURE_SIZE = 128  # the disc decoder is pure Python: the mip nearest this
PACK_TEXTURE_SIZE = 512


class Picture:
    def __init__(self, width, height, rgba, source):
        self.width = width
        self.height = height
        self.rgba = rgba
        self.source = source


def pack_picture(path, max_size=PACK_TEXTURE_SIZE, problems=None):
    """A texture pack file as a Picture (None without Pillow or if it can't be read; why goes in
    `problems`, {path: reason}, when given)."""
    if Image is None or not path:
        return None
    try:
        with Image.open(path) as img:
            img.load()
            img = img.convert("RGBA")
            if max(img.size) > max_size:
                img.thumbnail((max_size, max_size))
            return Picture(img.width, img.height, img.tobytes(), "pack")
    except Exception as e:  # noqa: BLE001 - Pillow raises many kinds for a file it can't read
        if problems is not None:
            problems[path] = "%s: %s" % (type(e).__name__, e)
        return None


def disc_picture(txtr, max_size=DISC_TEXTURE_SIZE):
    try:
        w, h, rgba = mc.decode_txtr(txtr, max_size)
    except (ValueError, struct.error, IndexError):
        return None
    return Picture(w, h, bytes(rgba), "disc")


class Scene:
    """A model or an area to draw: batches and looks by material index (of `materials`, the drawn
    set), pictures by TXTR id, PBR maps (normal, ORM) by TXTR id, and notes on what's missing."""

    def __init__(self, kind, rid, set_index, materials, batches, looks):
        self.kind = kind
        self.rid = rid
        self.set_index = set_index
        self.materials = materials
        self.batches = batches
        self.looks = looks
        self.pictures = {}
        self.maps = {}
        self.notes = []
        self.pack_found = 0     # base textures the pack replaces
        self.maps_found = 0     # ... and has PBR maps for
        self.problems = {}      # pack files that couldn't be read: {path: why}

    def texture_of(self, index, slot):
        m = self.materials[index] if index < len(self.materials) else None
        if m is None or slot is None or slot >= len(m.textures):
            return None
        return m.textures[slot]

    def base_texture(self, index):
        look = self.looks.get(index)
        return self.texture_of(index, look.base_slot) if look else None

    def env_texture(self, index):
        look = self.looks.get(index)
        return self.texture_of(index, look.env_slot) if look else None

    def glow_texture(self, index):
        look = self.looks.get(index)
        return self.texture_of(index, look.glow_slot) if look else None

    def has_maps(self, index):
        return self.base_texture(index) in self.maps

    @property
    def triangles(self):
        return sum(b.count for b in self.batches.values()) // 3


def build_scene(library, kind, rid, set_index=0, pack=None):
    """The Scene of an area ("MREA") or a model ("CMDL"), its textures read (`pack` a
    material_core.TexturePack, or None for the disc's)."""
    data = library.resource(rid)
    if kind == "MREA":
        _section, models = geometry.area_geometry(data)
        materials = library.area_materials(rid)
        set_index = 0
    else:
        _sets, model = geometry.cmdl_geometry(data)
        sets = library.model_materials(rid)
        set_index = min(set_index, len(sets) - 1) if sets else 0
        materials = sets[set_index] if sets else []
        models = [model]
    batches, looks = geometry.build_batches(models, materials)
    scene = Scene(kind, rid, set_index, materials, batches, looks)
    load_textures(scene, library, pack)
    if not batches:
        scene.notes.append("No triangles to show.")
    return scene


def load_textures(scene, library, pack):
    wanted = set()
    for index in scene.batches:
        for tid in (scene.base_texture(index), scene.env_texture(index), scene.glow_texture(index)):
            if tid is not None:
                wanted.add(tid)
    bases = {scene.base_texture(index) for index in scene.batches} - {None}
    maps_missing_pillow = False
    for tid in sorted(wanted):
        tex = nrm = orm = None
        if pack is not None:
            tex, nrm, orm = pack.find(library.texture_pack_name(tid))
        picture = pack_picture(tex, problems=scene.problems) if tex else None
        if picture is None:
            data = library.texture(tid)
            picture = disc_picture(data) if data else None
        elif tid in bases:
            scene.pack_found += 1
        if picture is not None:
            scene.pictures[tid] = picture
        if nrm and orm and tid in bases:
            if Image is None:
                maps_missing_pillow = True
                continue
            n = pack_picture(nrm, problems=scene.problems)
            o = pack_picture(orm, problems=scene.problems)
            if n is not None and o is not None:
                scene.maps[tid] = (n, o)
                scene.maps_found += 1
    if pack is not None and bases:
        scene.notes.append("Texture pack: %d of %d base textures, %d with PBR maps"
                           % (scene.pack_found, len(bases), scene.maps_found))
    if scene.problems:
        scene.notes.append("%d pack file(s) couldn't be read (first: %s)"
                           % (len(scene.problems), next(iter(scene.problems.values()))))
    if maps_missing_pillow:
        scene.notes.append("PBR maps not shown: this build can't read the texture pack")


# ------------------------------------------------------------------------------------- settings
def setting_percent(settings, key, default):
    try:
        return max(0.0, float(settings.get(key, default))) / 100.0
    except (TypeError, ValueError):
        return default / 100.0


def params(fields, look, has_maps, game):
    """How the game draws the material with `fields` (its materials.json entry): a dict of
    pbr (bool), roughness, metal, normal, occlusion, highlight, strength, env ("none", "painted" or
    "live"), env_strength, env_blur (0..1). `game` holds the game's own settings: pbr_maps,
    live_reflections, pbr_highlight (CCubeMaterial::PortBeginSurfaceDraw and Aurora's shader)."""
    def num(name):
        v = fields.get(name)
        spec = next(f for f in mc.FIELDS if f[0] == name)
        return spec[5] if v is None else mc.clamp_field(name, float(v))

    pbr = bool(game.get("pbr_maps")) and has_maps and fields.get("pbr", True) is not False
    out = {
        "pbr": pbr,
        "roughness": num("roughness"),
        "metal": num("metal"),
        "normal": num("normal"),
        "occlusion": num("occlusion"),
        "highlight": num("highlight") * game.get("pbr_highlight", 1.0),
        "strength": num("pbr_strength"),
        "env": "none",
        "env_strength": 1.0,
        "env_blur": 0.0,
        "live_probe": bool(game.get("live_reflections")),
        # The glow layer's brightness: the game's Luminance times the material's (Aurora clamps the
        # product to 0..32; CCubeMaterial::PortBeginSurfaceDraw).
        "glow": min(32.0, num("luminance") * game.get("luminance", 1.0)),
    }
    if look is not None and look.env_slot is not None:
        live = fields.get("reflection") is True and bool(game.get("live_reflections"))
        out["env"] = "live" if live else "painted"
        if live:
            out["env_strength"] = num("reflection_strength")
            # PortOverlay::kReflectionLevelPerBlurPercent (0.05) over the probe's last mip (5).
            out["env_blur"] = num("reflection_blur") / 100.0
    return out
