# Material Editor

`mp2_material_editor.exe` (beside `metroid_prime2_port.exe` in the Windows download) sets the port's
enhancements per material: whether a surface uses the PBR maps of your texture pack and how it reads
them, and whether its reflection shows the room live. It reads the materials straight from your disc
image and saves the settings to a `materials.json` in your mods folder. The disc's own data is never
changed, so removing the file (or switching the mod off) brings every surface back. A 3D preview shows
the model or area with your settings as you change them.

## Using it

1. Run `mp2_material_editor.exe` from the game's folder. It finds the game's settings (`user\settings.cfg`
   beside the game, or `%APPDATA%\Metroid Prime 2 Echoes\` for an older install), opens the disc image
   the game uses (`disc_path`), and saves to `user\mods\Material Editor\materials.json`. **Browse...**
   picks another image (`.iso`, `.gcm` or `.ciso`; convert RVZ/WIA/GCZ to ISO in Dolphin first), and
   **Change...** another file (any mod folder's `materials.json`).
2. On the left, pick an area (under its world) or a model. The filter box matches area names, model
   names and ids. It shows in the 3D preview, its materials in the list below it.
3. Pick a material, in the list or by clicking it in the preview: its textures show on the right
   (marked **PBR maps** when your texture pack, `user\user_textures`, has normal and ORM maps for
   them), with its settings below.
4. Change a setting: tick it (moving a slider ticks it for you) to use the value here; untick it for
   the game's own. With **Save as I change** on, every change is saved at once, and **the running game
   picks it up within a couple of seconds**: keep the game open beside the editor and watch the
   surface change. Otherwise **Save** (Ctrl+S).

Mods must be on in the game (F1 > Mods > Load mods) and the mod folder switched on; F1 > Mods shows how
many materials were read and any problem with a file.

## The 3D preview

The area or model you picked, drawn with your texture pack's textures (the disc's where the pack has
none) and with each material's settings, redrawn as you move a slider. The picked material flashes
when you pick it.

| To | Do |
| --- | --- |
| Turn around the model | Drag |
| Move | Right-drag (or middle-drag) |
| Zoom | Mouse wheel |
| Turn the light | Shift-drag |
| Pick a material | Click it |
| Turn around a point | Double-click it |
| Fly (areas) | Click the preview, then W/A/S/D, Q/E down/up (Shift for faster) |
| Frame the picked material / everything | F / Home, or the buttons |

**Only this material** draws the picked material alone, **Outline** outlines its triangles, and
**Without my changes** draws everything with the game's own settings, to compare. **Game settings**
start as your game has them (its `settings.cfg`): with **PBR maps** off in the game (Video > Quality),
the PBR settings change nothing there, and with **Live reflections** off, the reflection settings
don't either; tick them here to preview anyway. **Luminance** is the game's own Luminance setting.

The preview finds your texture pack as the game does: `user\user_textures` (or `MP2_USER_TEXTURES`;
not when the pack is switched off in F1 > Mods > Textures), every file named
`tex1_<width>x<height>[_m]_<hash>[_<palette hash>]_<format>[_arb].dds/.png` in any subfolder (or only
in the device folder, `keyboard` first, when the pack has device folders), wildcards (`$`) included,
and the PBR maps as `<that name>_nrm` / `_orm` beside them. The line under the preview says how many
of the drawn textures the pack replaced and has maps for, and names a pack file it couldn't read.

The shading is the game's PBR (the same normal map, roughness, metal, occlusion, highlight and
strength maths as the port's shader), but the light is a studio light (an ambient light plus one key
light you turn), not the room's lights and lightmaps, so a surface can look brighter or darker than in
the room. Live reflections show a studio around the model where the game shows the room. A material's
painted reflection is its own environment map. Effects the game adds while drawing (fog, bloom,
animated textures, the TEV's other stages) aren't in the preview: check the result in the game, which
picks the changes up within a couple of seconds.

The preview needs OpenGL 2.1, which any GPU driver from the last fifteen years or so has. Without it
(over Remote Desktop, or with Windows' basic display driver) the line under the preview says so and
the editor works the same without it.

## Settings

| Setting | Range | What it does |
| --- | --- | --- |
| PBR maps | game's choice / on / off | Whether the material uses the PBR maps your texture pack has for its texture. Off keeps the game's look for a surface the maps don't suit. |
| PBR strength | 0 to 1 | How much of the PBR lighting it takes: 0 is the game's own lighting. |
| Roughness | 0 to 4 | Times the roughness map: below 1 glossier with sharper highlights, above duller. |
| Metal | 0 to 4 | Times the metal map. |
| Normal map | 0 to 4 | How strongly the normal map tilts the surface: 0 flat, 1 as made. |
| Occlusion | 0 to 1 | How much the occlusion map darkens creases. |
| Highlights | 0 to 4 | Times the PBR highlights (on top of Video > Quality > PBR highlight strength). |
| Luminance | 0 to 8 | How bright the material's glow layer is (its light-emitting texels: lights, screens, Phazon), times Video > Quality > Luminance. 0 turns the glow off. Only materials the list marks *glows* have one. |
| Live reflection | game's choice / on / off | For a material with a reflection (an environment map; the list marks it *reflective*): on reflects the room live (Video > Quality > Live reflections must be on), off keeps the painted reflection. The three reflection fields are greyed out for other materials. |
| Reflection strength | 0 to 4 | How bright the live reflection is. |
| Reflection blur | 0 to 100 | 0 a sharp mirror, higher softer. |

The PBR settings only change surfaces the texture pack has PBR maps for (`tools/pbr/README.md` makes
them).

**Luminance** scales a material's glow layer: a stage that adds an unlit texture from the model's UVs
onto the lit colour (on the disc, the glow layer is added after the lit diffuse). The game's own
Video > Quality > Luminance (0 to 400%) scales every glow; a material's Luminance multiplies that. A
glow brightens until it reaches white, and with Bloom on, glows near white bloom more. **Copy settings** / **Paste settings** move them between materials, and **Apply to every listed
material with texture** gives every material in the list that uses a texture the same settings.

## How materials are matched

A material is named in the file by a 16-digit key: a hash (FNV-1a 64) of the material's bytes as the
disc holds them, together with the ids of the textures it uses. The game computes the same key from the
same bytes when it draws (`platform/include/port_material_overrides.h`), so the file needs no ids of
areas or models, and materials that are the same in every byte and texture share one entry: changing a
rock material changes that rock wherever it's used.

## The file

```json
{
  "version": 1,
  "materials": {
    "a1e1e2425b6ab4d9": {"pbr": false, "roughness": 1.6, "note": "lagoon rock"}
  }
}
```

Fields: `pbr`, `pbr_strength`, `roughness`, `metal`, `normal`, `occlusion`, `highlight`, `luminance`, `reflection`,
`reflection_strength`, `reflection_blur` and `note` (for you; the game ignores it). A field left out is
the game's choice. The game reads `materials.json` from every enabled mod folder in name order, a later
mod's entry for a material replacing an earlier one's, so a mod can ship its own. A hand-edited file
works the same; the editor keeps other top-level keys you add.

## For developers

- `tools/material_editor/material_core.py`: paths, the disc (through `tools/echoes_disc.py` and
  `tools/pbr/disc_texture_scan.py`), keys, GX texture decoding, the file. Standard library only.
  Tests: `tests/material_editor.py` (ctest `material_editor`).
- `tools/material_editor/geometry.py`: CMDL and MREA geometry, display lists, each material's base
  texture, glow stage and environment stage, as the engine reads them (the file's comment names the
  code). The glow rule (`material_core.glow_stages`) is Aurora's `port_emissive_stages`.
  `preview.py`: a model's or area's triangles and textures for the preview, and the settings as
  `CCubeMaterial::PortBeginSurfaceDraw` applies them. Standard library, plus Pillow for the texture
  pack. Tests: `tests/material_editor.py`.
- `tools/material_editor/viewer.py`: the preview (PyOpenGL in a pyopengltk frame, GLSL 1.20). Its
  shader follows Aurora's `port_surface_pbr` (`extern/aurora/lib/gx/shader.cpp`).
- `tools/material_editor/mp2_material_editor.py`: the window (tkinter). Runs with Python 3.8+:
  `py -3 tools\material_editor\mp2_material_editor.py --game "C:\path\to\the game folder"`
  (`--user`, `--disc`, `--output` override what it finds). For the preview:
  `py -3 -m pip install PyOpenGL pyopengltk pillow`.
- The Windows CI builds the .exe with PyInstaller (`.github/workflows/windows.yml`, x64), PyOpenGL,
  pyopengltk and Pillow inside; their licences go to `licenses\` (`bundle_licenses.py`).
- The game: `platform/port_material_overrides.cpp` (reading and re-reading the files),
  `CCubeMaterial::PortSetMaterialOverride` / `PortBeginSurfaceDraw` (applying them), and Aurora's
  `GXPortSetSurfacePBRParams` (the map scales) and `GXPortSetEmissive` (Luminance: the glow stages,
  `port_emissive_stages`).
