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
sit in `user_textures`. Tests: `tests/pbr_make_maps.py`.

**Drawing the PBR maps (step 3).** Put the `"<pack> PBR"` folder inside a loaded texture pack folder
(`<user>/user_textures`, beside the pack's own folder, or pass `--out` there), then F1 > Video > Quality >
PBR maps (`pbr_maps`, `MP2_PBR_MAPS=1`). `PortPbrMaps` (`platform/port_pbr_maps.cpp`) reads every
`*_nrm`/`*_orm` (.dds or .png) under the loaded packs' folders (user pack first; logs `[pbr] N PBR map
pairs`) and keys each pair by the pack name it sits beside. `CCubeMaterial::SetCurrent` picks the first of
the material's own TEV stages that is `0 + lerp(0, L, TEXC)` (`PortPbrMaps::ChooseStage`,
`platform/include/port_pbr_rules.h`; the room's diffuse stage after its lightmap stage, a model's only
one) and asks for that texture's maps (cached per `CTexture` and rescan generation). The first ask
registers both files with Aurora under stand-in texture objects (neutral 4x4 texels until a file has
streamed in); `CCubeModel::DrawSurface` loads them into GX_TEXMAP6/7 and calls `GXPortSetSurfacePBR`
for that surface's display list only. Aurora relights just that stage (`port_surface_pbr`,
`extern/aurora/lib/gx/shader.cpp`): channel 0's lights summed again per pixel with the mapped normal
(GX's own attenuation and diffuse function) replace the vertex/pixel light in L, so a room's lightmap
term is kept; occlusion and metal scale the diffuse; a GGX lobe per light and L reflected through
Karis's EnvBRDFApprox are added in linear light (each light feeds its lobe at most what GX lets it light
the surface, and the lights together at most GX's clamped 1.0; Quality > PBR highlight strength,
`pbr_highlight`, scales the lobes). The later stages run as before, so glow and masked
reflections stay. With neutral maps the diffuse is the original exactly. Draws whose stage isn't of
that form (Aurora's `port_surface_pbr_stage` checks), that use maps 6/7, the thermal visor, the model
shadow and the black pass are drawn as before. Those surfaces are lit per pixel whatever
`per_pixel_lighting` says. Their pipelines are not in the embedded seed (`pipeline_seed.py` skips
`pbrKind` rows), so each new one compiles the first time it is drawn.

**Live reflections.** F1 > Video > Quality > Live reflections (`live_reflections`, off by default):
`CStateManager::PortCaptureProbeFace`, ported from the MP1 port, draws faces of a cube map of the
world around the camera into a corner of the EFB before each frame's world (world geometry
and sky, culled for that face) and copies it into Aurora's probe (`GXCopyProbeFace`). Quality >
Reflection refresh sets how many faces a frame (1, 2, 3 or 6; default 3, the whole probe every 2
frames) and Reflection resolution their size (32, 64 or 128; default 64). Once the probe is full and
the camera has stopped, a face every fourth frame keeps it fresh (stopped: under 0.01 units moved in a
frame; MP1's 0.5 a frame counted walking as stopped, and the reflections stepped). It
runs only while something reads it, in the normal render mode. `CPlayerGun::Render` sets
`CCubeMaterial::sPortEnvProbe`, and `CCubeModel::DrawSurface` then calls `GXPortSetEnvProbe` around
the gun's surfaces: their environment-map stages (texture coordinates from the normal; the beam guns
add `C2 x env` over the base, C2 a mask) sample the probe along the view reflected about the
per-pixel normal, through `GXSetPBRProbe`'s matrix, at the level Cannon reflection blur sets, scaled by its strength, instead
of their fixed texture, the mask and the rest of the material unchanged. How sharp and bright the cannon's
reflection is comes from Quality > Cannon reflection blur and strength (`aurora_set_env_probe_look`;
by default 70% blur, about level 3.5 of 0-5, at 60%: a matte metal sheen rather than a mirror).
Surfaces with PBR maps
reflect the probe too, at the level their roughness asks, dimmed where the game lights them little.
Until all six faces are drawn the materials keep their own look.
Samus's body, the morph ball and the cutscene Samus read it as well (Quality > Samus & morph ball
reflections, on by default under Live reflections): `CPlayer::Render` and `CScriptPlayerActor::Render`
open a `CCubeMaterial::CPortEnvProbeScope`, which sets `sPortEnvProbe` with their own blur and
strength (`GXPortSetEnvProbeLook`, ordered with the draws, so the cannon keeps its matte look and
the ball its glossier one). When any of their draws read the probe, the next capture is centred on
her bounding box rather than on the camera: in the morph ball and cutscenes the camera stands well
away from her, and a probe taken there would reflect the wrong part of the room. Only the
environment-map stages their materials already have change; a material without one (or one drawn
unlit) is drawn as before.

**Bloom.** F1 > Video > Quality > Bloom (`bloom`, off by default): `CStateManager::DrawWorld` calls
Aurora's `GXPortPostProcess`, the Remastered bloom the MP1 port draws (`extern/aurora/lib/gfx/bloom.cpp`:
a bright pass at 1/4 size, four downsamples to 1/64, four upsamples back, `b / (1 + b)` added to the
frame), after the arm cannon and before the Dark and Echo visors' pictures, the camera filters and the
HUD, so the HUD never glows. It is off in those two visors, which draw their own picture, and in split
screen, where the post would cover the other viewports. The bright pass reads the EFB through a tone
curve, undoing it to get the light back; MP2's rooms have no Remastered data, so
`platform/include/port_bloom_rules.h` supplies one: the colour as drawn up to 0.6 linear, then a
shoulder that reaches white only at infinite light, so a pixel drawn near white counts as brighter
light (up to the pass's cap of 4.0, drawn at about 98%) and blooms more. The threshold (Quality > Bloom
threshold, 50-95%, default 90%) is the brightness, as drawn, at which a pixel starts to glow. The five
levels are weighted alike, and the strength (0-200%) scales the bright pass they all come from. The
composite adds the bloom to the frame as drawn, so the curve changes nothing else. A ReShade bloom
on top adds a second glow.

**Actor shadows.** F1 > Video > Quality > Actor shadows (`actor_shadows`, off by default): where
retail's `CSimpleShadow` drops a round blob textured quad onto the floor below an actor (a ray cast
straight down), `CSimpleShadow::PortRenderSilhouette` draws the actor's own shape there with Aurora's
Remastered actor shadow (`extern/aurora/lib/gfx/actor_shadow.cpp`). `CActor::AddToRenderer` notes the
owner on the shadow; the shadow's draw then renders the owner's models (opaque, depth only, the whole
model) between `GXPortActorShadowCasters` and `GXPortActorShadowEnd` into one 256x256 layer of the
actor maps, through an orthographic view straight down over the model's bounds
(`platform/include/port_actor_shadow_rules.h`), has `GXPortActorShadowRender` draw the map, and redraws
the blob's quad, widened to cover the map, as the multiply decal (`GXPortActorShadowDecal`):
`1 - (1 - vis) opacity (1 - w)^2`, `w` the floor's depth below the actor's feet over the blob's fade
height (`CSimpleShadow`'s max object height). The decal keeps to the shadow's box and to surfaces
facing up within 60 degrees. One layer each, so 16 a frame; the rest keep the blob. The quad is flat, as
the blob is, so on uneven ground the shadow cuts into the floor as the blob does.

**Ambient occlusion.** F1 > Video > Quality > Ambient occlusion (`ambient_occlusion`, off by default):
`CStateManager::DrawWorld` calls Aurora's `GXPortAmbientOcclusion` (`extern/aurora/lib/gfx/ssao.cpp`)
after the world and the actors and before the scan visor's markers, the fog planes, the Dark World
haze, the arm cannon and the HUD. It is McGuire, Mara and Luebke's Scalable Ambient Obscurance (HPG
2012) without its depth MIP chain: at half the EFB's size, each pixel's view position from the depth
buffer (reversed Z, the world's GX z range 0.125-1; nearer is the viewmodel, the far plane the sky), its
face normal from the neighbouring positions, and the AlchemyAO estimator (the paper's equation 1) over 9
samples on a 7-turn spiral within the radius; two 7-tap bilateral blurs; then the frame multiplied by
it in linear light, the four nearest half-size texels weighted by depth. It fades out from 40 to 80
units away. A flat surface is never darkened (the estimator's `v . n` is 0 on a plane), so it adds
contact darkening in creases and under objects on top of the lightmaps' baked shading rather than
replacing it. `platform/include/port_ao_rules.h` holds the intensity (sigma 0.6 at 100%) and radius
(1 unit at 100%). It works with MSAA and any render scale, unlike a ReShade AO, which loses the depth
buffer to MSAA and draws over the HUD.

**No sun shadows.** Aurora's sun cascades (`GXPortSetShadowFrame`) are received only by Remastered's
PBR materials (`shadow_receives` in `extern/aurora/lib/gx/gx.hpp` needs `pbr != 0`), which MP2's TEV
materials are not, and MP2's rooms are lightmapped: the lightmaps already hold the static shadows, so a
second sun shadow over them would darken them twice.

Reload is a savestate-style world restart: `PortSaveState::RequestModReload` writes the current state
to the undo slot and loads it back; `CMainFlow` (`kCFS_Game`) calls `ReloadModsNow` after the old world
is gone, which closes every PAK, runs `PortMods::Reload` (rescan, overlay rebuilt through
`aurora_dvd_overlay_files`) and reopens them (`CResLoader::PortReopenPaks`). Needs a running game in a
world; from the title screen restart instead. The built-in HD texture set can win over a replaced TXTR.
