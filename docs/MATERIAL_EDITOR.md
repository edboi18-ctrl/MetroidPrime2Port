# Material Editor

`mp2_material_editor.exe` (beside `metroid_prime2_port.exe` in the Windows download) sets the port's
enhancements per material: whether a surface uses the PBR maps of your texture pack and how it reads
them, and whether its reflection shows the room live. It reads the materials straight from your disc
image and saves the settings to a `materials.json` in your mods folder. The disc's own data is never
changed, so removing the file (or switching the mod off) brings every surface back.

## Using it

1. Run `mp2_material_editor.exe` from the game's folder. It finds the game's settings (`user\settings.cfg`
   beside the game, or `%APPDATA%\Metroid Prime 2 Echoes\` for an older install), opens the disc image
   the game uses (`disc_path`), and saves to `user\mods\Material Editor\materials.json`. **Browse...**
   picks another image (`.iso`, `.gcm` or `.ciso`; convert RVZ/WIA/GCZ to ISO in Dolphin first), and
   **Change...** another file (any mod folder's `materials.json`).
2. On the left, pick an area (under its world) or a model. The filter box matches area names, model
   names and ids.
3. In the middle list, pick a material: its textures show on the right (marked **PBR maps** when your
   texture pack, `user\user_textures`, has normal and ORM maps for them), with its settings below.
4. Change a setting: tick it (moving a slider ticks it for you) to use the value here; untick it for
   the game's own. With **Save as I change** on, every change is saved at once, and **the running game
   picks it up within a couple of seconds**: keep the game open beside the editor and watch the
   surface change. Otherwise **Save** (Ctrl+S).

Mods must be on in the game (F1 > Mods > Load mods) and the mod folder switched on; F1 > Mods shows how
many materials were read and any problem with a file.

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
| Live reflection | game's choice / on / off | For a material with a reflection (an environment map; the list marks it *reflective*): on reflects the room live (Video > Quality > Live reflections must be on), off keeps the painted reflection. |
| Reflection strength | 0 to 4 | How bright the live reflection is. |
| Reflection blur | 0 to 100 | 0 a sharp mirror, higher softer. |

The PBR settings only change surfaces the texture pack has PBR maps for (`tools/pbr/README.md` makes
them). **Copy settings** / **Paste settings** move them between materials, and **Apply to every listed
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

Fields: `pbr`, `pbr_strength`, `roughness`, `metal`, `normal`, `occlusion`, `highlight`, `reflection`,
`reflection_strength`, `reflection_blur` and `note` (for you; the game ignores it). A field left out is
the game's choice. The game reads `materials.json` from every enabled mod folder in name order, a later
mod's entry for a material replacing an earlier one's, so a mod can ship its own. A hand-edited file
works the same; the editor keeps other top-level keys you add.

## For developers

- `tools/material_editor/material_core.py`: paths, the disc (through `tools/echoes_disc.py` and
  `tools/pbr/disc_texture_scan.py`), keys, GX texture decoding, the file. Standard library only.
  Tests: `tests/material_editor.py` (ctest `material_editor`).
- `tools/material_editor/mp2_material_editor.py`: the window (tkinter). Runs with Python 3.8+:
  `py -3 tools\material_editor\mp2_material_editor.py --game "C:\path\to\the game folder"`
  (`--user`, `--disc`, `--output` override what it finds).
- The Windows CI builds the .exe with PyInstaller (`.github/workflows/windows.yml`, x64).
- The game: `platform/port_material_overrides.cpp` (reading and re-reading the files),
  `CCubeMaterial::PortSetMaterialOverride` / `PortBeginSurfaceDraw` (applying them), and Aurora's
  `GXPortSetSurfacePBRParams` (the map scales).
