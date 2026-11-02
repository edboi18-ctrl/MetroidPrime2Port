# Mods folder

Ported from MP1's first mods slice (MP1 `05cbf722a`): loose-file and PAK overrides plus a Mods list on
F1's Mods page. Suit colours and the HD texture pack were added later (sections below); downloads are
not part of it.

`mods` in the user path (`MP2_USER_PATH`, created on first start; `MP2_MODS` points elsewhere) holds one
folder per mod, applied in name order so a later name wins; folders starting with `.` are skipped.
Inside a mod:

- a file at a disc path (`Metroid5.pak`, `Audio/...`, `Video/...`; case-insensitive) replaces that disc
  file. Only existing disc files can be replaced; a `.thp` the disc lacks is added instead (the game
  opens movies by name); anything else is reported and ignored.
- a file named `<8 hex digits>.<TYPE>` (`1A2B3C4D.TXTR`, `0552A456.STRG`), anywhere in the mod, replaces
  that resource, uncompressed, in every PAK holding it. The PAK is served as a virtual file whose table
  points past the original data, where the loose file is appended (32-byte aligned).
- `.txt/.md/.json/.png/...` files are ignored silently.

`platform/port_mods.cpp` (scan, plan, register with Aurora's DVD overlays) runs from `main.cpp` right
after the disc mounts; `platform/port_mods_pak.cpp` is the unit-tested PAK table / virtual file / reader
half (`tests/port_mods.cpp`, ctest `port_mods_tests`), and `platform/include/port_mods_disc.h` lists the
disc's PAKs (`tests/port_mods_disc.cpp`, `port_mods_disc_tests`). PAK format is shared with Prime 1 (version
0x00030005, 20-byte table entries), so the parser is unchanged. Log lines start with `mods:` on stderr.
F1 > Mods lists the mods with a switch each (`mods_disabled`), the Load mods switch (`mods`) and the
problems found; changes apply on Reload mods or a restart. An absent or empty folder registers no overlay
at all.

## Suit colours

Video > Display > Suit colours recolours Samus's Varia (2 slots), Dark (1) and Light (2: Accent = gold trim, Main = silver/white body) suit
textures from the disc (`platform/port_suit_colours*.cpp`; the HSV remap is Metroid Prime's, the
texture ids are Echoes' own). They are generated at startup in `PortMods::Initialize` as loose
`.TXTR` resources, so they replace the texture in every PAK that holds it and a mod's own texture
for the same id wins. Nothing is generated while all picks are default, so rendering is unchanged.
Cache: `<user>/generated/suit-colours/<suit>/` (stamp covers picks and PAK offsets).

How the ids were found: each suit's CMDL (arm cannon, morph ball, beam/visor pieces, suit models)
was parsed for its material texture list, and the textures that carry the suit's orange, copper or
white-blue colouring were kept (tables in `port_suit_colours_disc.cpp`). The Light Suit's Main slot is
not a hue cluster: low-saturation, not-dark texels of its four body textures take the pick's hue and
saturation and keep their value (glow, lens, trim and dark-metal texels stay; Dark and Varia are
coloured enough that they need no main slot). A saved `suit_colour_light=RRGGBB` is the accent;
`-,RRGGBB` is main only. Limits: the Samus body model in cutscenes and some pause screen meshes
were not located, so they keep original colours; the first-person arm cannon is not the suit and
stays as is. The pause menu's Inventory Varia Suit model (LogBook.pak sheets) is recoloured too.
Changes show after a restart or the Apply suit colours button (`reloadmods`), also with the pause menu
open: `CMFGame` services the reload while paused and `CPauseScreen::PortReloadScanModels` reloads
the open Inventory model (the room follows when unpaused).

## Reload, folder, texture pack (MP1 sync 21)

F1 > Mods has Reload mods and Open mods folder (desktop), then the Textures section: the built-in HD set's
name, how many of the pack's textures the game has used so far (and how many failed to load), a note naming the disc's game code and the pack folder that fits it (G2M, or
R3M on Trilogy), the Use the texture pack switch (`texture_pack`), Reload texture pack and
Import texture pack .zip... (desktop). Android: Choose texture pack folder... copies the pick to
`<user>/user_textures.new`, which `PortTextures` swaps in (Remove texture pack deletes it). Console:
`reloadmods`.

**Texture roles from the disc (for PBR, step 1 of `docs/research/PBR_TEXTURES.md`).** No play needed:
`python tools/pbr/disc_texture_scan.py "<Echoes>.iso"` (an `.iso`, `.gcm` or `.ciso`; RVZ/WIA/GCZ need
converting to ISO in Dolphin first) reads every CMDL's material sets and every MREA's
world-geometry material set straight from the disc image (through `tools/echoes_disc.py`), computes each
texture's pack name as the port does (XXH64 of the base level, and of the palette range it uses), and
writes `texture_manifest_disc.jsonl` in the format below; `pip install xxhash` makes it faster. Then run
`texture_report.py` on that file. Tests: `tests/pbr_disc_scan.py`.

**Texture manifest while playing (a cross-check of the disc scan).** `MP2_TEXTURE_MANIFEST=1`
appends to `texture_manifest.jsonl` beside the log file (any other value but a false one is the file's
path): once per material and texture slot, the texture's pack file name (as `MP2_DUMP_TEXTURES` names
it) with the material's flags, blend, TEV stages and texgens (`CCubeMaterial::SetCurrent`,
`platform/port_texture_manifest.cpp`). Play through the rooms to cover, then on the PC:
`python tools/pbr/texture_report.py texture_manifest.jsonl "<Dolphin>/Load/Textures/G2M"`
(Windows: `py -3 tools\pbr\texture_report.py ...`) writes `texture_report.csv`: each texture's role
(lightmap, diffuse, glow, reflection, alpha, indirect, unused), its pack file and folder, where it is
used, and whether it is a PBR candidate, plus a summary of what the pack covers that the manifest has
not seen yet. Standard-library Python 3.8+; tests: `tests/pbr_texture_report.py`,
`tests/port_texture_manifest.cpp`.

**PBR maps (step 2).** `python tools/pbr/make_pbr_maps.py texture_report.csv "<pack folder>"` makes, for
each PBR candidate in the report, `<pack file stem>_nrm.dds` (tangent-space normal, X/Y in R/G, glTF's
+X right +Y up, byte 128 flat; BC5) and `<pack file stem>_orm.dds` (glTF occlusion/roughness/metal in
R/G/B; BC7), in a folder laid out like the pack (default `"<pack> PBR"` beside it; the pack is never
written to). Height is the luminance band-passed in original texels (sub-texel upscaler detail and
broad painted shading removed; `chroma` sinks bright colour, such as lava in cracks); the steepest 5% of
the normals capped at `max_tilt` (30 degrees); presets by pack folder (`--write-settings`/`--settings`
to tune them, or single textures by name); maps at half the pack's size by default (`--scale`).
`--preview` writes a labelled PNG per texture (pack, normal, ORM, lit without and with the maps, the
light roughly as Aurora's PBR shader gives it without a reflection probe); `--limit` and `--only` try
a few first; a rerun remakes only what changed (`pbr_maps.json`). texconv runs with `-dx10` (without it
BC5 comes out as the legacy FourCC `BC5U`, which Aurora's DDS reader refuses), and every file it writes
is checked against that reader's formats. An atlas's empty ground (black, white or transparent) is filled
from the pieces' edges before the height is taken (`fill_empty`), so no bevel lands on the model's
seams. `pbr_settings.json` in the current folder is read without `--settings`. The disc scan marks sky
models (each MLVL's default sky, and `overrideSky` of the areas' AreaAttributes, `REAA`), and the
report gives their textures the role `sky`, never a candidate. The report also counts, per diffuse texture, the
materials that add a reflection layer (`reflective`) or a glow layer (`glow_layer`), and lists each
reflection's mask (`reflection_masks`): 92% of the disc's reflections are scaled by a texture the
material first loads into a register (where it shines). Such a texture gets the role `mask`, and one that
is only ever a mask is no candidate (its maps would never be sampled as a base colour). The generator
lowers a reflective texture's roughness towards `reflective_roughness` (0.35) by each use's mask mean
brightness, read from the pack (1 unmasked, 0.5 when unknown or not in the pack); the mask has its own
UVs, so only its mean carries over. A texture's own roughness setting wins. Needs numpy and Pillow; texconv
(DirectXTex) compresses, else RGBA8 DDS. The port's loader skips `_nrm`/`_orm` names, so the folder can
sit in `user_textures`; nothing draws them until step 3. Tests: `tests/pbr_make_maps.py`.

Reload is a savestate-style world restart: `PortSaveState::RequestModReload` writes the current state
to the undo slot and loads it back; `CMainFlow` (`kCFS_Game`) calls `ReloadModsNow` after the old world
is gone, which closes every PAK, runs `PortMods::Reload` (rescan, overlay rebuilt through
`aurora_dvd_overlay_files`) and reopens them (`CResLoader::PortReopenPaks`). Needs a running game in a
world; from the title screen restart instead. The built-in HD texture set can win over a replaced TXTR.
