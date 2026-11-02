#!/usr/bin/env python3
"""Make PBR maps (a normal map and an occlusion/roughness/metal map) for an HD pack's surfaces.

Step 2 of docs/research/PBR_TEXTURES.md, the fast path: image heuristics, no learned models, no GPU.
It reads texture_report.csv (tools/pbr/texture_report.py), takes the rows marked pbr_candidate=yes
and, from each one's pack file, writes two maps into a folder laid out like the pack (by default
"<pack> PBR" beside it; nothing is written into the pack):

  <pack file stem>_nrm.dds  tangent-space normal, X in R and Y in G, in glTF's convention (+X right,
                            +Y up, V running down the image), stored so that byte 128 is flat, as
                            Aurora's PBR shader decodes it (byte * 255/128 - 1). BC5.
  <pack file stem>_orm.dds  glTF occlusion (R), roughness (G) and metal (B), linear. BC7.

The port's texture loader skips these names (they don't parse as replacements), so the folder can sit
in user_textures beside the pack; step 3, the renderer, is what will read them.

    py -3 -m pip install numpy pillow
    py -3 tools\\pbr\\make_pbr_maps.py texture_report.csv "<pack folder>" --limit 20 --preview

Each map is made at --scale times the pack file's size (default 0.5, never below the GameCube size):
  height     the luminance (BT.709 weights), band-passed: blurred by `blur` original texels so the
             upscaler's sub-texel detail doesn't become bumps, minus a blur of `relief` texels so
             light and shade painted across a wall doesn't become a slope. Darker is lower (`invert`
             turns that round), and `chroma` x the colourfulness (max - min of R, G, B) is taken
             off, for art whose bright colour sits low (lava or glow in cracks). The filters wrap at
             the edges, as a tiling texture does. An atlas's empty ground (black, white or
             transparent, a few texels across) is first filled from the pieces' edges (`fill_empty`), so the
             pieces don't rise from it with a bevel along the model's seams.
  normal     the height's slope per original texel, times `normal`, lowered where needed so that
             the steepest 5% tilt no more than `max_tilt` degrees (a noisy texture stays calm).
  occlusion  1 - `occlusion` x how far the height sits below its surroundings (`ao_radius` texels).
             Kept light: the pack's art and the game's lightmaps already carry shading.
  roughness  `roughness`, rougher in hollows and smoother on ridges by up to `roughness_var`.
             Where the texture's materials have a reflection layer (the artists made it shiny, e.g.
             Phazon), it is lowered towards `reflective_roughness` by how much they shine: each such
             use counts its mask's mean brightness (the report's `reflection_masks`, read from the
             pack; the mask has its own UVs, so only its mean carries over), 1 unmasked, 0.5 when
             unknown. A texture's own roughness setting wins.
  metal      `metal`, scaled down in hollows (dirt isn't metal).
A texture takes its preset from its pack folder (Temple Grounds stone, Agon Wastes rock, Torvus Bog
wet stone, Sanctuary Fortress metal, Enemies creature, Samus suit, any other folder default).
--write-settings saves the presets and the folder table as JSON to edit, --settings reads it back
(pbr_settings.json in the current folder is read without it, and rewriting a settings file keeps its
edits), and its "textures" table sets a preset or values for single textures by name. The values are
starting points, to be tuned once the renderer shows them. Sky textures are no candidates once the
disc scan and the report have been run again (the scan marks sky models).

Needs Python 3.8+, numpy and Pillow. Compression uses texconv from Microsoft's DirectXTex
(https://github.com/microsoft/DirectXTex/releases, or `winget install Microsoft.DirectXTex.Texconv`),
found with --texconv, the TEXCONV variable, beside this script or on PATH; it is run with -dx10, and
every file it writes is checked against the formats the port's DDS reader takes. Without it the maps
are written uncompressed (RGBA8, four times the size), which the port reads as well. A second run
remakes only the textures whose source, settings or options changed (pbr_maps.json in the output).
"""

import argparse
import concurrent.futures
import copy
import csv
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

try:
    import numpy as np
    from PIL import Image
except ImportError:  # main() reports it, so --help and --write-settings work without them
    np = None
    Image = None

TOOL_VERSION = 5
INDEX_NAME = "pbr_maps.json"
SETTINGS_NAME = "pbr_settings.json"  # read from the current folder when --settings isn't given

# The metal preset is half metal: the game lights its rooms through lightmaps, a diffuse light a full
# metal drops, and without a reflection probe a metal reflects only the ambient and a part of its
# lights. To be settled when the renderer (step 3) shows it.
# Every preset sets every key. normal: slope per unit of height; roughness, roughness_var and metal:
# 0-1; occlusion: darkening per unit of hollow; blur, relief and ao_radius: original texels;
# max_tilt: degrees (0 is no cap); chroma: height taken off per unit of colourfulness.
_COMMON = {"blur": 0.5, "relief": 8.0, "ao_radius": 2.0, "invert": False, "max_tilt": 30.0,
           "chroma": 0.0, "fill_empty": True, "reflective_roughness": 0.35}
BOOL_KEYS = ("invert", "fill_empty")
EMPTY_LEVEL = 8.0 / 255.0  # an atlas's empty space: black or white to within this, or transparent
PRESETS = {
    "default":   {"normal": 1.0, "roughness": 0.75, "roughness_var": 0.15, "metal": 0.0, "occlusion": 2.0},
    "stone":     {"normal": 1.4, "roughness": 0.85, "roughness_var": 0.10, "metal": 0.0, "occlusion": 2.0},
    "rock":      {"normal": 1.5, "roughness": 0.90, "roughness_var": 0.08, "metal": 0.0, "occlusion": 2.5},
    "wet_stone": {"normal": 1.2, "roughness": 0.55, "roughness_var": 0.20, "metal": 0.0, "occlusion": 2.0},
    "metal":     {"normal": 0.7, "roughness": 0.45, "roughness_var": 0.20, "metal": 0.5, "occlusion": 1.5},
    "creature":  {"normal": 1.0, "roughness": 0.55, "roughness_var": 0.15, "metal": 0.0, "occlusion": 1.5},
    "suit":      {"normal": 0.5, "roughness": 0.35, "roughness_var": 0.10, "metal": 0.0, "occlusion": 1.0},
}
for _preset in PRESETS.values():
    for _key, _value in _COMMON.items():
        _preset.setdefault(_key, _value)
PARAM_KEYS = tuple(PRESETS["default"])

FOLDER_PRESETS = {
    "Temple Grounds": "stone",
    "Agon Wastes": "rock",
    "Torvus Bog": "wet_stone",
    "Sanctuary Fortress": "metal",
    "Enemies": "creature",
    "Samus": "suit",
}

# BT.709 luma weights, applied to the stored (gamma-encoded) values.
LUMA = (0.2126, 0.7152, 0.0722)

# DDS (DDS_HEADER / DDS_PIXELFORMAT). Aurora's reader (extern/aurora/lib/gfx/dds_io.cpp) wants the
# 124- and 32-byte sizes, and for a mip chain the MIPMAPCOUNT flag with the COMPLEX and MIPMAP caps.
DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PITCH = 0x1, 0x2, 0x4, 0x8
DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT = 0x1000, 0x20000
DDPF_ALPHAPIXELS, DDPF_RGB = 0x1, 0x40
DDSCAPS_COMPLEX, DDSCAPS_TEXTURE, DDSCAPS_MIPMAP = 0x8, 0x1000, 0x400000

NORMAL_FORMAT, ORM_FORMAT, RAW_FORMAT = "BC5_UNORM", "BC7_UNORM", "R8G8B8A8_UNORM"


# ------------------------------------------------------------------------------------- settings
def default_settings():
    return {"presets": copy.deepcopy(PRESETS), "folders": dict(FOLDER_PRESETS), "textures": {}}


def texture_key(name):
    """A texture's name as the settings' "textures" table matches it: lower case, no extension,
    no _m (so the pack file's name and the manifest's both work)."""
    stem = name.strip().lower()
    for ext in (".dds", ".png"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
    return re.sub(r"^(tex1_\d+x\d+)_m_", r"\1_", stem)


def _check_values(where, values, warnings):
    clean = {}
    for key, value in values.items():
        if key == "preset":
            clean[key] = str(value)
        elif key not in PARAM_KEYS:
            warnings.append("%s: unknown setting %r ignored" % (where, key))
        elif key in BOOL_KEYS:
            clean[key] = bool(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            clean[key] = float(value)
        else:
            warnings.append("%s: %s must be a number, not %r" % (where, key, value))
    return clean


def load_settings_file(path):
    """A settings file's JSON. A UTF-8 byte-order mark (some Windows editors write one) is allowed;
    a syntax error raises ValueError naming the line and, for the usual slips, what to change."""
    text = pathlib.Path(path).read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        lines = text.splitlines()
        shown = ["  line %d: %s" % (n, lines[n - 1].rstrip()) for n in (e.lineno - 1, e.lineno)
                 if 1 <= n <= len(lines)]
        hint = ""
        if "',' delimiter" in e.msg:
            before = lines[e.lineno - 1][:e.colno - 1] if e.lineno <= len(lines) else ""
            if before.strip():
                hint = "a comma is missing on line %d, before column %d" % (e.lineno, e.colno)
            else:
                prev = next((n for n in range(e.lineno - 1, 0, -1) if lines[n - 1].strip()), e.lineno)
                hint = "a comma is missing at the end of line %d (entries are separated by commas)" % prev
        elif "trailing comma" in e.msg or "property name enclosed in double quotes" in e.msg:
            hint = ("JSON allows no comma after the last entry, and names need \"double quotes\"; "
                    "look just before line %d" % e.lineno)
        raise ValueError("%s (line %d, column %d)\n%s%s" % (
            e.msg, e.lineno, e.colno, "\n".join(shown), "\n  Likely fix: " + hint if hint else ""))


def merge_settings(base, extra, warnings):
    """`base` with a settings file's tables laid over it (a preset's keys merge into the preset)."""
    out = copy.deepcopy(base)
    for name, values in (extra.get("presets") or {}).items():
        values = _check_values("preset %s" % name, values or {}, warnings)
        values.pop("preset", None)
        out["presets"].setdefault(name, dict(PRESETS["default"])).update(values)
    for folder, preset in (extra.get("folders") or {}).items():
        out["folders"][folder] = str(preset)
    for name, values in (extra.get("textures") or {}).items():
        out["textures"][texture_key(name)] = _check_values("texture %s" % name, values or {}, warnings)
    for folder, preset in out["folders"].items():
        if preset not in out["presets"]:
            warnings.append("folder %s: no preset %r, using default" % (folder, preset))
    return out


def reflective_fraction(text):
    """The report's `reflective` column ("uses with a reflection layer / diffuse uses") as 0-1."""
    try:
        done, total = (int(x) for x in text.split("/"))
        return min(1.0, max(0.0, done / total)) if total > 0 else 0.0
    except (AttributeError, ValueError):
        return 0.0


def mask_mean(path, channel):
    """A reflection mask's mean brightness (0-1): BT.709 luma of the colour, or the alpha."""
    with Image.open(str(path)) as image:
        image.load()
        image = image.convert("RGBA")
        image.thumbnail((64, 64))
        pixels = np.asarray(image, dtype=np.float32) / 255.0
    if channel == "a":
        return float(pixels[..., 3].mean())
    return float((pixels[..., 0] * LUMA[0] + pixels[..., 1] * LUMA[1] + pixels[..., 2] * LUMA[2]).mean())


def reflection_shine(row, mean_of):
    """0-1: how shiny the texture's materials make it, averaged over its diffuse uses. `mean_of`
    gives a mask's mean brightness from its manifest name and channel, or None if unknown."""
    try:
        total = int(row.get("reflective", "").split("/")[1])
    except (IndexError, ValueError):
        return 0.0
    entries = row.get("reflection_masks")
    if not entries:  # a report from before the column: each reflective use counts half
        return 0.5 * reflective_fraction(row.get("reflective", ""))
    shine = 0.0
    for part in entries.split("; "):
        kind, _, count = part.rpartition("*")
        try:
            count = int(count)
        except ValueError:
            continue
        if kind == "full":
            shine += count
        elif kind == "unknown":
            shine += 0.5 * count
        else:
            name, _, channel = kind.rpartition(":")
            mean = mean_of(name, channel)
            shine += count * (0.5 if mean is None else mean)
    return min(1.0, shine / total) if total > 0 else 0.0


def params_for(settings, rel, manifest_name, reflective=0.0):
    """(preset name, parameters) for the pack file at `rel` (relative to the pack root);
    `reflective` (0-1) is how shiny its materials make it (reflection_shine)."""
    folders = {k.lower(): v for k, v in settings["folders"].items()}
    folder = rel.parts[0].lower() if len(rel.parts) > 1 else ""
    textures = settings["textures"]
    override = textures.get(texture_key(rel.name)) or textures.get(texture_key(manifest_name)) or {}
    preset = override.get("preset") or folders.get(folder) or "default"
    if preset not in settings["presets"]:
        preset = "default"
    params = dict(PRESETS["default"])
    params.update(settings["presets"][preset])
    params.update({k: v for k, v in override.items() if k != "preset"})
    if reflective > 0.0 and "roughness" not in override:
        shiny = min(params["roughness"], params["reflective_roughness"])
        params["roughness"] = round(params["roughness"] + (shiny - params["roughness"]) * reflective, 4)
    return preset, params


# ----------------------------------------------------------------------------------------- maps
def blur_wrap(image, sigma):
    """A Gaussian blur of a 2-D array (std `sigma` pixels) that wraps at the edges, done in the
    frequency domain: the transform of a Gaussian of std s is exp(-2 pi^2 s^2 f^2)."""
    if sigma <= 0.0:
        return image
    fy = np.fft.fftfreq(image.shape[0])[:, None]
    fx = np.fft.rfftfreq(image.shape[1])[None, :]
    gain = np.exp(-2.0 * (math.pi * sigma) ** 2 * (fx * fx + fy * fy))
    return np.fft.irfft2(np.fft.rfft2(image) * gain, s=image.shape).astype(np.float32)


def empty_space(rgb, alpha, texel_px):
    """The empty space of an atlas (the black, white or transparent ground its pieces sit on) as a
    mask, or None: such areas a few texels across, and the same ground up to their edges. A thin
    line, such as a groove, is too narrow to count."""
    empty = (rgb.max(axis=-1) < EMPTY_LEVEL) | (rgb.min(axis=-1) > 1.0 - EMPTY_LEVEL)
    if alpha is not None:
        empty |= alpha < 0.5
    if not empty.any():
        return None
    core = blur_wrap(empty.astype(np.float32), 1.0 * texel_px) > 0.9
    if not core.any():
        return None
    mask = empty & (blur_wrap(core.astype(np.float32), 1.5 * texel_px) > 0.02)
    return mask if mask.mean() > 0.002 else None


def push_pull(values, known):
    """A 2-D array with its unknown pixels filled from the known ones, so a hole meets its edge
    without a step (push-pull: average known values down a pyramid, then fill each level's holes
    from the coarser level on the way back up)."""
    weight = known.astype(np.float32)
    pyramid = [(values * weight, weight)]
    while pyramid[-1][1].shape[0] > 1 or pyramid[-1][1].shape[1] > 1:
        v, w = pyramid[-1]
        pyramid.append((halve(v[..., None])[..., 0], halve(w[..., None])[..., 0]))
    v, w = pyramid[-1]
    filled = np.where(w > 0, v / np.maximum(w, 1e-12), 0.0)
    for v, w in reversed(pyramid[:-1]):
        rows = np.minimum(np.arange(w.shape[0]) // 2, filled.shape[0] - 1)
        cols = np.minimum(np.arange(w.shape[1]) // 2, filled.shape[1] - 1)
        filled = v + (1.0 - w) * filled[rows][:, cols]
    return np.where(known, values, filled).astype(np.float32)


def make_maps(rgb, texel_px, params, alpha=None):
    """(normal, orm) for an RGB image (H x W x 3, 0-1 as stored; `alpha` H x W or None) whose
    original texels are `texel_px` pixels across: unit tangent-space normals (+X right, +Y up) and
    occlusion, roughness and metal, both H x W x 3 float32."""
    lum = rgb[..., 0] * LUMA[0] + rgb[..., 1] * LUMA[1] + rgb[..., 2] * LUMA[2]
    if params["chroma"]:
        lum = lum - params["chroma"] * (rgb.max(axis=-1) - rgb.min(axis=-1))
    lum = lum.astype(np.float32)
    if params["fill_empty"]:
        # An atlas's pieces would otherwise rise from their black ground as plateaus, and the
        # bevel at their edges lands on the model's seams.
        empty = empty_space(rgb, alpha, texel_px)
        if empty is not None:
            lum = push_pull(lum, ~empty)
    height = blur_wrap(lum, params["blur"] * texel_px) - blur_wrap(lum, params["relief"] * texel_px)
    if params["invert"]:
        height = -height
    # Slope per original texel: central differences per pixel, times pixels per texel.
    gx = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) * (0.5 * texel_px)
    gy = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) * (0.5 * texel_px)
    # A surface faces away from its uphill side. Rows run down the image and +Y is up, so a height
    # rising down the rows tilts the normal towards +Y.
    strength = params["normal"]
    if params["max_tilt"] > 0.0:
        steep = float(np.percentile(np.hypot(gx, gy), 95)) * strength
        limit = math.tan(math.radians(min(params["max_tilt"], 89.0)))
        if steep > limit:
            strength *= limit / steep
    normal = np.stack([-strength * gx, strength * gy, np.ones_like(gx)], axis=-1)
    normal /= np.linalg.norm(normal, axis=-1, keepdims=True)
    hollow = np.maximum(blur_wrap(height, params["ao_radius"] * texel_px) - height, 0.0)
    occlusion = np.clip(1.0 - params["occlusion"] * hollow, 0.0, 1.0)
    spread = float(height.std())
    ridge = np.clip(height / (2.0 * spread), -1.0, 1.0) if spread > 1e-6 else np.zeros_like(height)
    roughness = np.clip(params["roughness"] - params["roughness_var"] * ridge, 0.02, 1.0)
    metal = np.clip(params["metal"] * occlusion, 0.0, 1.0)
    orm = np.stack([occlusion, roughness, metal], axis=-1).astype(np.float32)
    return normal.astype(np.float32), orm


def halve(image):
    """The next mip level of an H x W x C array: max(1, H/2) x max(1, W/2), 2x2 averages (an odd
    last row or column is dropped)."""
    h, w = image.shape[:2]
    if h > 1:
        image = image[:h - h % 2]
        image = 0.5 * (image[0::2] + image[1::2])
    if w > 1:
        image = image[:, :w - w % 2]
        image = 0.5 * (image[:, 0::2] + image[:, 1::2])
    return image


def mip_chain(image, renormalize=False):
    levels = [image]
    while levels[-1].shape[0] > 1 or levels[-1].shape[1] > 1:
        level = halve(levels[-1])
        if renormalize:
            level = level / np.maximum(np.linalg.norm(level, axis=-1, keepdims=True), 1e-8)
        levels.append(level)
    return levels


def encode_normal(normal):
    """H x W x 4 bytes: 128 + 128 x each component (clamped), so that 128 decodes to 0 the way
    Aurora reads it (x * 255/128 - 1); B holds Z for viewing, A is 255."""
    xyz = np.clip(np.rint(128.0 + 128.0 * normal), 0, 255).astype(np.uint8)
    return np.concatenate([xyz, np.full(xyz.shape[:2] + (1,), 255, np.uint8)], axis=-1)


def encode_unorm(values):
    """H x W x 4 bytes from H x W x 3 values in 0-1 (A is 255)."""
    rgb = np.clip(np.rint(values * 255.0), 0, 255).astype(np.uint8)
    return np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), 255, np.uint8)], axis=-1)


def output_size(pack_w, pack_h, orig_w, orig_h, scale):
    """The maps' size: `scale` times the pack file's, at least the original's (or the pack's if it
    is smaller), at most the pack's, and in whole 4x4 blocks where it is 4 or more."""
    def one(pack, orig):
        size = int(round(pack * scale))
        size = max(size, min(orig or pack, pack))
        size = min(size, pack)
        if size >= 4:
            size -= size % 4
        return max(1, size)
    return one(pack_w, orig_w), one(pack_h, orig_h)


# ------------------------------------------------------------------------------------------ DDS
def dds_rgba8(levels):
    """An uncompressed RGBA8 DDS (R in the low byte) holding a mip chain of H x W x 4 uint8 arrays."""
    h, w = levels[0].shape[:2]
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PITCH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT
    header = struct.pack("<7I44x", 124, flags, h, w, w * 4, 0, len(levels))
    pixel_format = struct.pack("<8I", 32, DDPF_RGB | DDPF_ALPHAPIXELS, 0, 32,
                               0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
    caps = DDSCAPS_TEXTURE | (DDSCAPS_COMPLEX | DDSCAPS_MIPMAP if len(levels) > 1 else 0)
    tail = struct.pack("<5I", caps, 0, 0, 0, 0)
    data = b"".join(np.ascontiguousarray(level, dtype=np.uint8).tobytes() for level in levels)
    return b"DDS " + header + pixel_format + tail + data


def texconv_command(exe, dxgi_format, out_dir, source, quick):
    # -dx10: always the DX10 header. Without it texconv writes BC5 with the legacy FourCC 'BC5U',
    # which Aurora's DDS reader doesn't take (only 'ATI2', or DX10 format 83).
    command = [str(exe), "-nologo", "-y", "-dx10", "-f", dxgi_format, "-o", str(out_dir)]
    if quick:
        command += ["-bc", "q"]  # BC7 from mode 6 alone: much faster, a little lower quality
    return command + [str(source)]


def find_texconv(explicit=None):
    """texconv's path: --texconv, the TEXCONV variable, beside this script, the current folder,
    PATH; None if it isn't found."""
    for candidate in (explicit, os.environ.get("TEXCONV")):
        if candidate:
            return candidate if os.path.isfile(candidate) else None
    here = pathlib.Path(__file__).resolve().parent
    for folder in (here, pathlib.Path.cwd()):
        for name in ("texconv.exe", "texconv"):
            if (folder / name).is_file():
                return str(folder / name)
    return shutil.which("texconv")


# What Aurora's DDS reader (lib/gfx/dds_io.cpp: resolve_dds_layout) takes: legacy FourCCs, and DX10
# DXGI formats. Legacy BC5 FourCCs are rewritten as DX10 BC5_UNORM (83), the same data.
AURORA_FOURCCS = (b"DXT1", b"DXT5", b"ATI2")
AURORA_DXGI = {28: "R8G8B8A8_UNORM", 29: "R8G8B8A8_UNORM_SRGB", 87: "B8G8R8A8_UNORM",
               71: "BC1_UNORM", 72: "BC1_UNORM_SRGB", 77: "BC3_UNORM", 78: "BC3_UNORM_SRGB",
               83: "BC5_UNORM", 98: "BC7_UNORM", 99: "BC7_UNORM_SRGB"}
DXGI_BC5_UNORM = 83


def port_readable_dds(data):
    """`data` with a legacy BC5 header ('BC5U' or 'ATI2') rewritten as DX10 BC5_UNORM; raises
    ValueError if the result is a DDS the port can't read."""
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    pf_flags, fourcc = struct.unpack_from("<2I", data, 80)
    if pf_flags & 0x4:  # DDPF_FOURCC
        code = data[84:88]
        if code in (b"BC5U", b"ATI2"):
            data = (data[:84] + b"DX10" + data[88:128] +
                    struct.pack("<5I", DXGI_BC5_UNORM, 3, 0, 1, 0) + data[128:])
            code = b"DX10"
        if code == b"DX10":
            if len(data) < 148:
                raise ValueError("DX10 header cut short")
            dxgi, dimension, _misc, array_size = struct.unpack_from("<4I", data, 128)
            if dxgi not in AURORA_DXGI or dimension != 3 or array_size != 1:
                raise ValueError("DXGI format %d (%s), which the port's DDS reader doesn't take"
                                 % (dxgi, "2-D" if dimension == 3 else "dimension %d" % dimension))
        elif code not in AURORA_FOURCCS:
            raise ValueError("FourCC %r, which the port's DDS reader doesn't take" % code)
        return data
    masks = struct.unpack_from("<5I", data, 88)
    if masks not in ((32, 0xFF, 0xFF00, 0xFF0000, 0xFF000000), (32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000)):
        raise ValueError("an uncompressed layout the port's DDS reader doesn't take")
    return data


def write_map(levels, dest, dxgi_format, texconv, quick):
    """Writes a mip chain to `dest`: compressed to `dxgi_format` by texconv, or as RGBA8 without it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = dds_rgba8(levels)
    if texconv is None:
        temp = dest.with_name(dest.name + ".part")
        temp.write_bytes(data)
        os.replace(str(temp), str(dest))
        return
    with tempfile.TemporaryDirectory(prefix="pbr_maps_") as work:
        source = pathlib.Path(work, "in", dest.name)
        source.parent.mkdir()
        source.write_bytes(data)
        out_dir = pathlib.Path(work, "out")
        out_dir.mkdir()
        result = subprocess.run(texconv_command(texconv, dxgi_format, out_dir, source, quick),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, errors="replace")
        produced = out_dir / dest.name
        if result.returncode != 0 or not produced.is_file():
            tail = " ".join((result.stdout or "").split()[-30:])
            raise RuntimeError("texconv failed (exit %d): %s" % (result.returncode, tail))
        try:
            data = port_readable_dds(produced.read_bytes())
        except ValueError as e:
            raise RuntimeError("texconv wrote %s" % e)
        temp = dest.with_name(dest.name + ".part")
        temp.write_bytes(data)
        os.replace(str(temp), str(dest))


# -------------------------------------------------------------------------------------- preview
PREVIEW_LIGHT = 0.8    # the key light's colour (white), from the top left
PREVIEW_AMBIENT = 0.2  # the ambient's
PREVIEW_LABELS = ("pack", "normal", "ORM", "lit, no maps", "lit, with maps")


def env_brdf(roughness, n_v):
    """(A, B) of the split-sum environment BRDF, by Karis's analytic fit ("Physically Based Shading
    on Mobile", Epic Games, 2014: EnvBRDFApprox)."""
    c0, c1 = (-1.0, -0.0275, -0.572, 0.022), (1.0, 0.0425, 1.04, -0.04)
    r = [roughness * c0[i] + c1[i] for i in range(4)]
    a004 = np.minimum(r[0] * r[0], np.exp2(-9.28 * n_v)) * r[0] + r[1]
    return -1.04 * a004 + r[2], 1.04 * a004 + r[3]


def shade(rgb, normal, orm):
    """A flat tile seen head on, lit roughly as Aurora's PBR shader lights a surface without a
    reflection probe: a key light with its GGX lobe (alpha = roughness^2 floored at roughness 0.02,
    k = alpha/2, Schlick Fresnel), occlusion on the direct diffuse as well as the ambient, and an
    environment of the ambient plus 0.35 x the light (pbr_envspec without a probe) through the
    split-sum BRDF. No lightmap: for judging the maps, not the final look."""
    base = np.power(rgb, 2.2)
    occlusion, roughness, metal = orm[..., 0:1], orm[..., 1:2], orm[..., 2:3]
    light = np.array([-0.5, 0.5, 0.7], np.float32)
    light /= np.linalg.norm(light)
    view = np.array([0.0, 0.0, 1.0], np.float32)
    half = (light + view) / np.linalg.norm(light + view)
    n_l = np.clip(normal @ light, 0.0, 1.0)[..., None]
    n_v = np.clip(normal[..., 2:3], 1e-4, 1.0)
    n_h = np.clip(normal @ half, 0.0, 1.0)[..., None]
    v_h = float(np.clip(view @ half, 0.0, 1.0))
    alpha = np.maximum(roughness, 0.02) ** 2
    k = alpha / 2.0
    distribution = alpha ** 2 / (math.pi * (n_h ** 2 * (alpha ** 2 - 1.0) + 1.0) ** 2)
    visibility = 1.0 / (4.0 * (n_l * (1.0 - k) + k) * (n_v * (1.0 - k) + k))
    f0 = 0.04 * (1.0 - metal) + base * metal
    fresnel = f0 + (1.0 - f0) * (1.0 - v_h) ** 5
    diffuse = base * (1.0 - metal)
    direct = (diffuse * occlusion + math.pi * distribution * visibility * fresnel) * n_l * PREVIEW_LIGHT
    a, b = env_brdf(roughness, n_v)
    environment = (PREVIEW_AMBIENT + 0.35 * PREVIEW_LIGHT) * (f0 * a + b)
    ambient = (diffuse * PREVIEW_AMBIENT + environment) * occlusion
    return np.power(np.clip(direct + ambient, 0.0, 1.0), 1.0 / 2.2)


def preview_image(rgb, normal, orm):
    """Pack texture, normal map, ORM map, lit flat (no maps), lit with the maps, each labelled: side
    by side at up to 256 px high, or stacked at up to 1024 px wide for a texture twice as wide as
    it is high."""
    from PIL import ImageDraw, ImageFont
    flat = np.zeros_like(normal)
    flat[..., 2] = 1.0
    plain = np.zeros_like(orm)
    plain[..., 0] = 1.0
    plain[..., 1] = PRESETS["default"]["roughness"]
    panels = [rgb, normal * 0.5 + 0.5, orm, shade(rgb, flat, plain), shade(rgb, normal, orm)]
    h, w = rgb.shape[:2]
    stacked = w >= 2 * h
    if stacked:
        pw = min(w, 1024)
        ph = max(1, int(round(pw * h / w)))
    else:
        ph = min(h, 256 if h <= w else 512)
        pw = max(1, int(round(ph * w / h)))
    label, gap, count = 14, 4, len(panels)
    size = (pw, count * (ph + label) + (count - 1) * gap) if stacked else \
        (count * pw + (count - 1) * gap, ph + label)
    sheet = Image.new("RGB", size, (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    resample = getattr(Image, "Resampling", Image).LANCZOS
    for i, (panel, name) in enumerate(zip(panels, PREVIEW_LABELS)):
        x, y = (0, i * (ph + label + gap)) if stacked else (i * (pw + gap), 0)
        draw.text((x + 3, y + 1), name, fill=(225, 225, 225), font=font)
        tile = Image.fromarray(np.clip(np.rint(panel * 255.0), 0, 255).astype(np.uint8))
        sheet.paste(tile.resize((pw, ph), resample), (x, y + label))
    return sheet


# ------------------------------------------------------------------------------------------ job
def load_rgba(path):
    with Image.open(str(path)) as image:
        image.load()
        return np.asarray(image.convert("RGBA"))


def process(job):
    """Makes one texture's maps. `job` is a dict (see build_jobs); returns a dict with "ok"."""
    try:
        rgba = load_rgba(job["source"])
        pack_h, pack_w = rgba.shape[:2]
        out_w, out_h = output_size(pack_w, pack_h, job["orig"][0], job["orig"][1], job["scale"])
        image = Image.fromarray(np.ascontiguousarray(rgba))
        if (out_w, out_h) != (pack_w, pack_h):
            image = image.resize((out_w, out_h), getattr(Image, "Resampling", Image).LANCZOS)
        pixels = np.asarray(image, dtype=np.float32) / 255.0
        rgb, alpha = pixels[..., :3], pixels[..., 3]
        alpha = alpha if alpha.min() < 0.5 else None
        orig_w = job["orig"][0] or max(1, pack_w // 4)
        orig_h = job["orig"][1] or max(1, pack_h // 4)
        texel_px = 0.5 * (out_w / orig_w + out_h / orig_h)
        normal, orm = make_maps(rgb, texel_px, job["params"], alpha)
        texconv = job["texconv"]
        write_map([encode_normal(level) for level in mip_chain(normal, renormalize=True)],
                  pathlib.Path(job["nrm"]), NORMAL_FORMAT, texconv, job["quick"])
        write_map([encode_unorm(level) for level in mip_chain(orm)],
                  pathlib.Path(job["orm"]), ORM_FORMAT, texconv, job["quick"])
        if job.get("preview"):
            preview = pathlib.Path(job["preview"])
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview_image(rgb, normal, orm).save(str(preview))
        return {"ok": True, "size": [out_w, out_h]}
    except Exception as e:  # one bad file mustn't stop a batch of a thousand
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def read_report(report):
    """(the candidate rows, {manifest name: pack file} for every row with one)."""
    with open(str(report), "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if rows and "pbr_candidate" not in rows[0]:
        raise ValueError("%s isn't a texture_report.csv (no pbr_candidate column)" % report)
    pack_files = {r["name"]: r["pack_file"] for r in rows if r.get("name") and r.get("pack_file")}
    return [r for r in rows if r.get("pbr_candidate") == "yes" and r.get("pack_file")], pack_files


def rel_path(pack_file):
    """The CSV's pack_file (written with \\ on Windows) as a relative path."""
    return pathlib.PurePosixPath(pack_file.replace("\\", "/"))


def selected(rel, patterns):
    if not patterns:
        return True
    names = [str(rel).lower(), rel.name.lower(), rel.parts[0].lower() if len(rel.parts) > 1 else ""]
    return any(fnmatch.fnmatch(name, p.lower().replace("\\", "/")) for p in patterns for name in names)


def job_hash(job, source_stat):
    data = {"version": TOOL_VERSION, "params": job["params"], "scale": job["scale"],
            "compressed": job["texconv"] is not None, "quick": job["quick"],
            "source": [source_stat.st_size, source_stat.st_mtime_ns]}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def build_jobs(rows, pack, out, settings, args, texconv, index, pack_files=None):
    """(jobs to run, rows already up to date, rows whose pack file is missing). `pack_files` maps
    every manifest name in the report to its pack file, for the reflection masks."""
    jobs, current, missing = [], 0, []
    pack_files = pack_files or {}
    means = {}

    def mean_of(name, channel):
        key = (name, channel)
        if key not in means:
            rel = pack_files.get(name)
            path = pack.joinpath(*rel_path(rel).parts) if rel else None
            try:
                means[key] = mask_mean(path, channel) if path and path.is_file() else None
            except (OSError, ValueError):
                means[key] = None
        return means[key]
    for row in rows:
        rel = rel_path(row["pack_file"])
        source = pack.joinpath(*rel.parts)
        if not source.is_file():
            missing.append(str(rel))
            continue
        preset, params = params_for(settings, rel, row.get("name", ""), reflection_shine(row, mean_of))
        stem = rel.name.rsplit(".", 1)[0]
        folder = out.joinpath(*rel.parts[:-1])
        job = {
            "key": str(rel), "source": str(source), "preset": preset, "params": params,
            "orig": (int(row.get("width") or 0), int(row.get("height") or 0)),
            "scale": args.scale, "texconv": texconv, "quick": args.quick,
            "nrm": str(folder / (stem + "_nrm.dds")), "orm": str(folder / (stem + "_orm.dds")),
            "preview": str(out.joinpath("_preview", *rel.parts[:-1]) / (stem + ".png"))
                       if args.preview else None,
        }
        job["hash"] = job_hash(job, source.stat())
        done = index.get(job["key"])
        if (not args.force and done and done.get("hash") == job["hash"] and
                os.path.isfile(job["nrm"]) and os.path.isfile(job["orm"]) and
                (not job["preview"] or os.path.isfile(job["preview"]))):
            current += 1
            continue
        jobs.append(job)
    return jobs, current, missing


def load_index(path):
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            return json.load(f).get("maps", {})
    except (OSError, ValueError, AttributeError):
        return {}


def save_index(path, pack, maps):
    data = {"tool": "make_pbr_maps.py", "version": TOOL_VERSION, "pack": str(pack),
            "normal": {"format": NORMAL_FORMAT, "channels": "RG = XY, glTF +X right +Y up, 128 is 0"},
            "orm": {"format": ORM_FORMAT, "channels": "R occlusion, G roughness, B metal (glTF)"},
            "maps": maps}
    temp = path.with_name(path.name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(temp), "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(str(temp), str(path))


def run_jobs(jobs, workers, on_done):
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            on_done(job, process(job))
        return
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process, job): job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            on_done(futures[future], future.result())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", nargs="?", type=pathlib.Path, help="texture_report.csv")
    parser.add_argument("pack", nargs="?", type=pathlib.Path, help="the pack folder the report was made with")
    parser.add_argument("-o", "--out", type=pathlib.Path,
                        help='where the maps go (default: "<pack> PBR" beside the pack)')
    parser.add_argument("--settings", type=pathlib.Path, help="a settings JSON (see --write-settings)")
    parser.add_argument("--write-settings", type=pathlib.Path, metavar="JSON",
                        help="write the built-in presets and folder table to edit, and stop")
    parser.add_argument("--only", action="append", metavar="PATTERN",
                        help='only pack files, folders or names matching this (e.g. "Temple Grounds"); repeatable')
    parser.add_argument("--limit", type=int, help="at most this many textures (after --only)")
    parser.add_argument("--scale", type=float, default=0.5,
                        help="map size against the pack file's (default 0.5; never below the GameCube size)")
    parser.add_argument("--texconv", help="texconv.exe's path (default: TEXCONV, beside this script, or PATH)")
    parser.add_argument("--no-compress", action="store_true", help="write RGBA8 even when texconv is there")
    parser.add_argument("--quick", action="store_true", help="faster, slightly lower-quality BC7 (texconv -bc q)")
    parser.add_argument("--preview", action="store_true",
                        help="also write a PNG per texture to _preview: pack, normal, ORM, lit flat, lit with the maps")
    parser.add_argument("--force", action="store_true", help="remake maps that are up to date")
    parser.add_argument("-j", "--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)),
                        help="textures made at once (default: up to 4)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every texture")
    args = parser.parse_args(argv)

    warnings = []
    settings = default_settings()
    if args.settings is None:
        # An existing settings file is read first, so --write-settings over it keeps its edits.
        for candidate in (args.write_settings, pathlib.Path(SETTINGS_NAME)):
            if candidate is not None and candidate.is_file():
                args.settings = candidate
                print("Using the settings in %s (--settings picks another file)" % candidate)
                break
    if args.settings:
        try:
            settings = merge_settings(settings, load_settings_file(args.settings), warnings)
        except (OSError, ValueError, AttributeError) as e:
            print("error: can't read %s: %s" % (args.settings, e), file=sys.stderr)
            return 2
    if args.write_settings:
        with open(str(args.write_settings), "w", encoding="utf-8", newline="\n") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        print("Wrote %s" % args.write_settings)
        return 0
    if args.report is None or args.pack is None:
        parser.error("give texture_report.csv and the pack folder")
    if np is None:
        parser.error("this needs numpy and Pillow: py -3 -m pip install numpy pillow")
    if not args.report.is_file():
        parser.error("no report at %s" % args.report)
    if not args.pack.is_dir():
        parser.error("no pack folder at %s" % args.pack)
    if not 0.0 < args.scale <= 1.0:
        parser.error("--scale must be above 0 and at most 1")
    pack = args.pack.resolve()
    out = args.out.resolve() if args.out else pack.parent / (pack.name + " PBR")
    if out == pack or pack in out.parents:
        parser.error("the output folder can't be inside the pack (%s)" % out)
    for warning in warnings:
        print("warning: " + warning)

    try:
        rows, pack_files = read_report(args.report)
    except (OSError, ValueError) as e:
        parser.error(str(e))
    rows = [r for r in rows if selected(rel_path(r["pack_file"]), args.only)]
    if args.limit is not None:
        rows = rows[:max(0, args.limit)]
    texconv = None if args.no_compress else find_texconv(args.texconv)
    if args.texconv and texconv is None:
        parser.error("no texconv at %s" % args.texconv)
    if texconv is None and not args.no_compress:
        print("note: texconv wasn't found, so the maps are written uncompressed (RGBA8, four times the "
              "size). Get texconv.exe from https://github.com/microsoft/DirectXTex/releases (or "
              "`winget install Microsoft.DirectXTex.Texconv`) and put it beside this script.")

    index_path = out / INDEX_NAME
    index = load_index(index_path)
    jobs, current, missing = build_jobs(rows, pack, out, settings, args, texconv, index, pack_files)
    print("%d candidate textures: %d to make, %d up to date, %d missing from the pack folder. Output: %s"
          % (len(rows), len(jobs), current, len(missing), out))
    for rel in missing[:5]:
        print("  missing: %s" % rel)

    made, failed = [], []
    started = time.time()

    def on_done(job, result):
        if result["ok"]:
            made.append(job["key"])
            index[job["key"]] = {
                "hash": job["hash"], "preset": job["preset"], "params": job["params"],
                "size": result["size"],
                "nrm": os.path.relpath(job["nrm"], str(out)).replace("\\", "/"),
                "orm": os.path.relpath(job["orm"], str(out)).replace("\\", "/"),
                "format": RAW_FORMAT if job["texconv"] is None else "compressed",
            }
        else:
            failed.append((job["key"], result["error"]))
            print("  failed: %s: %s" % (job["key"], result["error"]))
        done = len(made) + len(failed)
        if args.verbose and result["ok"]:
            print("  [%d/%d] %s (%s)" % (done, len(jobs), job["key"], job["preset"]))
        elif done % 25 == 0 or done == len(jobs):
            print("  %d of %d done (%.0f s)" % (done, len(jobs), time.time() - started))
        if done % 50 == 0:
            save_index(index_path, pack, index)

    try:
        run_jobs(jobs, args.jobs, on_done)
    finally:
        if made:
            save_index(index_path, pack, index)
    print("Made %d, %d up to date, %d failed, %d missing." % (len(made), current, len(failed), len(missing)))
    if made:
        print("Wrote %s" % out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
