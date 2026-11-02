# PBR from HD texture packs: feasibility (2026-11-01)

The question: can a tool analyse the upscaled HD texture packs made for Echoes, generate PBR maps
from them, and can this port render with them? Short answer: **yes, in phases**. The renderer half
is smaller than it looks, because the vendored Aurora already carries MP1's PBR shader; the hard
half is the content (getting believable roughness, metal and normals out of upscaled GameCube
textures that have lighting painted into them) and telling which textures are surfaces at all.

Nothing below has been run; it is a reading of this tree, MP1's tree and public sources.

## What already exists

**Texture packs load today.** `extern/aurora/lib/gfx/texture_replacement.cpp` reads Dolphin-named
replacements (`tex1_{w}x{h}_{hash}_{fmt}.dds|png`, paletted `tex1_{w}x{h}_{hash}_{tluthash}_{fmt}`,
`format_replacement_filename`), so Dolphin packs for `G2ME01` work as they are (F1 > Mods > Textures,
`texture_pack`). `MP2_DUMP_TEXTURES=1` dumps every texture the game loads under the same names to
`<cache>/texture_dumps` (`platform/main.cpp`, `dump_editable_texture_dds`). The DDS reader
(`extern/aurora/lib/gfx/dds_io.cpp`) takes BC1, BC3, BC5 (two channels: right for a tangent-space
normal) and BC7, sRGB or not. Replacements stream and are evicted when idle (`sweep_idle_cache`).

**The PBR shader is already in the vendored Aurora, unused.** `GXSetPBR` and 28 related calls
(`extern/aurora/include/dolphin/gx/GXExtra.h`) came along with the Aurora vendored from MP1; no file
under `src/` or `platform/` calls them. The shader (`extern/aurora/lib/gx/shader.cpp`, the
`GX_AURORA_SET_PBR` comment) replaces the TEV colour with a metal/roughness evaluation of texture
maps 0-3:

| Map | Contents |
| --- | --- |
| 0 | base colour |
| 1 | glTF ORM: occlusion R, roughness G, metal B |
| 2 | two-channel tangent-space normal |
| 3 | emissive |

lit by channel 0's GX lights and ambient, with Remastered's lobe (GGX, alpha = roughness^2 floored
at 0.02, Schlick Fresnel on the specular). Two properties matter for Echoes: the **tangent frame
comes from screen-space derivatives**, so the game's tangent-less meshes need nothing new; and maths
is done on linearised colour and converted back, so it sits in the existing gamma pipeline. Also
present: probes and prefiltered cubes for reflections (`GXSetPBRProbe`, `GXCreatePBRCube`), HDR
lightmaps (`GXCreatePBRLightmap`, BC6H or RGBA16F), ambient (`GXSetPBRAmbient`), debug views
(`GXSetPBRDebugView`: base colour, normal, roughness, metal, occlusion...) and cost tests.

**Echoes gives the shader real lights.** World geometry is drawn per light set: each area surface
group gets up to four GX lights plus an ambient (`CCubeRenderer::ActivateLightsForModel`, called per
`mLightSets` entry in the area draw loop), and actors get `CActorLights`. So channel 0 is lit for
rooms as well as actors, unlike a purely lightmapped scene, where a PBR surface would only show its
ambient.

**MP1 shows the game-side hook.** MP1 enables PBR per surface in `CCubeModel` (`GXSetPBR(GX_TRUE)`
before `surface.CallDisplayList()`, off after) for materials it converted from Remastered's data
(`platform/port_remastered_convert_pbr.cpp`). Echoes has no such data, so the maps must come from
the pack instead, but the hook point is the same: `CCubeModel::DrawSurface` in this tree.

## What does not exist yet

1. **Which texture is what.** A pack is a flat folder of hashes. Only the game knows that a hash is
   a surface's diffuse, a lightmap, a reflection map, an incandescence (glow) layer, an indirect
   warp, a HUD element, a font page or a particle. PBR maps make sense for the first kind only. The
   port can record this at bind time: `CCubeMaterial::SetCurrent`/`HandleTev` knows each texture's
   TEV stage and the material flags (`kStateFlag_Lightmap`, `kStateFlag_Reflection`, ...).
2. **Companion maps in the pack.** The loader knows one file per hash. It needs sibling files
   (proposed: `<name>_orm.dds`, `<name>_nrm.dds`, `<name>_emi.dds` beside `<name>.dds`) and a way for
   a draw to ask "does my diffuse have a PBR set?".
3. **A PBR draw built from a GameCube material.** The shader reads each map "from the TEV stage that
   samples it, so the material's fallback TEV must reference all four". A GameCube material does
   not. Either synthesise those stages in the hook, or (simpler, and better for MP1 too) give Aurora
   a way to bind the maps by texmap slot. Aurora is vendored from MP1: make that change in MP1's
   Aurora and take it here, so the resync path (`docs/MP1_SYNC.md` package 2) stays open.
4. **The tool.** Nothing generates the maps yet.

## Proposed design

```
 game (port)                      tool (Windows desktop)                    game (port)
 -----------                      ----------------------                    -----------
 MP2_TEXTURE_MANIFEST=1  ──JSON──▶ 1. read pack + manifest                  F1 > Video > Renderer:
 hash → role, material,           2. classify: surface / skip              Original (TEV) | PBR
 flags, model, area, UV scale     3. per surface texture:                  per draw: diffuse has
                                     de-light, normal, rough,              a PBR set? → PBR draw,
 MP2_DUMP_TEXTURES=1 (exists) ─▶     metal, AO, emissive                   else TEV as today
 originals for comparison         4. preview with the port's BRDF
                                  5. write _orm/_nrm/_emi DDS  ──pack──▶   Aurora loads siblings
```

**In the port:**

- `MP2_TEXTURE_MANIFEST=1`: each texture bound by `CCubeMaterial::SetCurrent` is written once to a
  JSON manifest with its replacement name, its role (from the TEV stage and the material flags), the
  model asset and area, the material's blend and alpha test, and its UV scale. Reuse shows up here:
  a texture on many surfaces gets one roughness, so the tool should know.
- Aurora: load the sibling maps with the base replacement; `GXPortPBRMapsFor(texobj)`; a slot-based
  PBR binding (above).
- `CCubeModel::DrawSurface` (TARGET_PC): with the PBR renderer chosen and the surface's diffuse
  having a set, bind maps 1-3 to free texmaps, `GXSetPBR(GX_TRUE)`, and keep the GameCube lightmap
  as baked diffuse (`GXSetPBRLightmap`, the lightmap converted once to linear RGBA16F). Everything
  without a set draws with its TEV as now, so partial packs work and coverage can grow.
- F1 > Video: Renderer (Original / PBR), the debug views behind a Debug row, `pbr` console command.

**The tool** (Windows first; Python 3.12 + PySide6 for the UI, a CLI underneath so it batches):

| Step | Fast path (no GPU, permissive) | Learned path (NVIDIA GPU) |
| --- | --- | --- |
| De-light the albedo | none (keep the pack's art) | Marigold IID Appearance v1.1 albedo |
| Normal | height from luminance, Sobel, frequency-separated so ESRGAN noise doesn't become bumps | Marigold Normals, or DeepBump as an external tool (GPL-3) |
| Roughness | inverse local contrast, per-preset curve | Marigold IID Appearance v1.1 roughness |
| Metal | per material preset (Luminoth metal, Dark Aether organic, stone, glass) | Marigold IID Appearance v1.1 metallicity, clamped by the preset |
| Occlusion | from the height map | same |
| Emissive | the game's own incandescence layer (the manifest finds it), not guessed | same |
| Encode | DirectXTex `texconv`: BC7 sRGB base, BC7 linear ORM, BC5 normal | same |

Marigold IID works at about 768 px on the long side (its model card), so large textures are run
tiled with overlap or at 768 px with the maps upsampled under the full-size albedo. A preview pane
lights a sphere and a flat tile with the shader's own lobe, so what the tool shows is what the port
draws; presets and per-texture overrides go in a JSON sidecar that the batch re-reads.

## Status

- **Step 1, the manifest: done (2026-11-01).** `tools/pbr/disc_texture_scan.py` reads every
  model and area material straight from the disc image, so nothing has to be played; the in-game
  `MP2_TEXTURE_MANIFEST=1` writes the same records for what is drawn, as a cross-check.
  `tools/pbr/texture_report.py` joins either with the pack (usage in `docs/MODS.md`). The port records raw facts and the
  report classifies, so the role rules can be tuned on real data without a rebuild; they are
  heuristics until checked against a play-through. The pack in use: Venomalia's "Another" Metroid
  Prime 1, 2, 3 or Trilogy HD Texture Pack (4x, HDCube ESRGAN, BC7 DDS, folders by area and by
  enemies/FX/HUD/Samus; marked CC0 on the Dolphin forum:
  https://forums.dolphin-emu.org/Thread-another-metroid-prime-1-2-3-or-trilogy-hd-texture-pack-ai).
  The loader reads its sub-folders (recursive scan), and the report treats HUD/FX/font/text/UI/menu
  folders as non-surfaces.
- **First disc scan (2026-11-01, `G2ME01` from a CISO, that pack).** 4,781 textures in model and
  area materials; 1,566 have a pack file (exact XXH64 names, so the scan's naming is right for CMPR,
  RGB5A3 and the I4/I8/IA8 it met). Roles: diffuse 2,672, lightmap 1,161, glow 655, diffuse+glow 161,
  reflection 108 (49 of the 51 in the pack sit in its FX folder). 1,231 PBR candidates: Temple
  Grounds 397, Agon Wastes 290, Enemies 135, Sanctuary Fortress 133, Torvus Bog 118, Genn 109, Samus
  49. The pack replaces no lightmap (only 15 pack files in surface folders went unmatched), so the
  renderer keeps the game's own lightmaps as baked light. The rest it leaves out: tiny textures (9 of
  227 at 16 px or less) and 1,351 other diffuse textures, which keep drawing with TEV. Pack files
  no material uses: Text 380, FX 198, HUD 62, Trilogy 37 (fonts, particles, HUD frames and another
  disc's hashes).
- **Step 2, the maps (fast path): done (2026-11-01).** `tools/pbr/make_pbr_maps.py` writes
  `<pack file stem>_nrm.dds` and `_orm.dds` per candidate into `"<pack> PBR"`, laid out like the pack.
  Conventions, from Aurora's PBR shader (`extern/aurora/lib/gx/shader.cpp`, `pbr_func`): map 1 is
  glTF occlusion/roughness/metal in R/G/B; map 2 is the tangent-space normal's X/Y in R/G, decoded
  `x * 255/128 - 1` (byte 128 flat), +X right and +Y up with V down the image, the frame built from
  screen-space derivatives. Formats Aurora's DDS reader takes (`lib/gfx/dds_io.cpp`): BC5 and BC7
  through texconv, RGBA8 without it. The current loader skips both names (they don't parse as
  replacements), so the maps can sit in `user_textures`; step 3 finds them by stripping `_nrm`/`_orm`
  and parsing the rest. Emissive is not generated: the material's own glow layer is the emissive.
  The presets are guesses until the renderer shows them.
- **First previews (2026-11-01, 20 of the pack's textures).** The maps follow the art's shapes. What
  they showed, and the changes: the preview lit a metal with one light and nothing to reflect,
  where the shader also reflects the ambient and 0.35 x the lights (`pbr_envspec` without a probe),
  so it now does too (split sum through Karis's EnvBRDFApprox); world geometry is lit by lightmaps,
  a diffuse light a full metal drops, so the metal preset is half metal (0.5) until step 3 shows
  it; a speckled Agon texture gave a busy normal map, so the steepest 5% of normals are capped
  (`max_tilt`, 30 degrees) and rock is weaker (1.5); lava in a Genn rock's cracks is brighter than
  the stone, so it came out raised: brightness as height can't tell, and `chroma` (per texture)
  takes colourful areas down; and wide textures got 32 px previews, now stacked and labelled.
- **First maps from the pack (2026-11-01, the same 20, texconv BC5/BC7).** The data survived
  compression (normals within 0.006-0.016 of the uncompressed vectors on average, ORM within about
  one level: no colour-space conversion). Found: texconv wrote BC5 with the legacy FourCC `BC5U`,
  which `dds_io.cpp` doesn't take (it takes `ATI2` or DX10 83), so the tool now runs it with `-dx10`
  and checks every file it writes; all ten wide Temple Grounds candidates were sky panoramas, so the
  disc scan now marks sky models (MLVL default sky, `REAA` `overrideSky`) and the report keeps their
  textures out; an enemy atlas's pieces rose from their black ground with a bevel on the model's
  seams, so empty atlas space is filled (push-pull) before the height is taken; and a settings
  file is now read from the current folder, as the lava override had not been passed.
- **Second run (2026-11-02, the same disc and pack).** The scan found 14 sky models; 90 textures
  are sky (84 only, 6 shared), and all ten wide Temple Grounds panoramas left the candidates (1,231
  to 1,220, with a Sanctuary texture both a sky and a room use). The 68 wide candidates left are
  mostly trims used by rooms. The new previews showed the lava override and the atlas fill working,
  and a Temple Grounds atlas on white ground, so white ground is now empty space too. Open: a blue
  lightning texture on five models is plain diffuse; if its materials never read the lit colour,
  Aurora's PBR (which needs channel 0 sampled: `sampledColorChannels.test(0)`) can't apply to it,
  which the manifest's TEV stages can tell.
- **The disc's materials, from that scan's manifest (39,624 records).** Every lightmapped area material
  (29,393) reads the lit colour on channel 0, so Aurora's PBR condition holds for the world geometry
  and step 3 can hook it as planned; 2% of area records and 7% of model records don't (fullbright
  draws), and none of the 1,220 candidates is among them. The blue texture is Phazon (CMDL
  0x105600E8 and four more): stage 0 lit diffuse (RASC x TEXC), stage 1 a glow added (CPREV + TEXC),
  stages 2-3 a masked reflection added: lit, with its own light from the glow layer, which step 3
  takes as the emissive. 331 candidates have a reflection layer in some or all of their materials
  (the artists made them shiny) and 431 a glow layer; the report now counts both per texture and the
  generator lowers roughness for the reflective ones. Rebuilt from this manifest, the report's
  candidate decisions are unchanged.
- **Reflection masks (2026-11-02).** The next 20 previews put 12 at the full shiny roughness, so
  the reflection layers were looked at closely: all 1,981 take their coordinates from the normal
  (real environment maps, 124 textures, two of them in 862 materials), and 1,822 are scaled by a
  register an earlier stage loaded from a texture: the artists' mask of where the surface shines (55
  are unscaled, 17 scaled by the lit colour, 87 written to a register for later). The masks have their
  own UV sets (TEX1/TEX2 against the diffuse's TEX0; 0 of 1,124 pairs share texture coordinates), so
  only a mask's mean can carry over to the diffuse's roughness. The report classified 313 masks as
  diffuse; they are now `mask` (the texture written to the register a reflection stage is scaled by):
  99 candidates were only ever masks and leave (1,220 to 1,129; e.g. the lava rock, Retro's masks
  being painted art: its material's base is 406a679e, a candidate with that mask), and 8 Samus suit
  textures whose mask use had read as a glow join. The roughness drop is weighted by the masks' mean
  brightness.
- **Step 3 must keep the added layers (2026-11-02).** The lava rock's base (406a679e) is plain grey
  stone with dark cracks; the orange in its cracks comes from the reflection layer, scaled by the
  coloured mask (042ffe03) in C2. Aurora's PBR replaces the TEV colour result outright (`pbr_func`:
  "replaces the TEV colour result with a metal/roughness evaluation of texture maps 0-3"), so a plain
  switch would drop every added layer: that shimmer, Phazon's glow, every masked reflection (232
  candidates have one, 397 a glow layer). The renderer has to light the base with PBR and then add
  the material's later stages (glow, masked reflection) on top, or carry them as the emissive, rather
  than replacing the whole result.
- **Step 3, the renderer: done (2026-11-02), untested in game.** "Port surface PBR": only the TEV
  stage that lights the base texture is relit, and the stages after it run unchanged, so the added
  layers stay. `GXPortSetSurfacePBR(stage)` (`GX_AURORA_PORT_SURFACE_PBR` 0x0071) sets `pbrKind =
  stage + 1` with `pbr` 0, which keeps `ShaderConfig`'s layout and so the pipeline seed's
  (`GXPipelineConfigVersion` unchanged; the seed never holds these rows). Aurora keeps the kind only
  when `port_surface_pbr_stage` accepts the stage: `0 + lerp(0, L, TEXC)`, plain add, no indirect, L
  = RASC on channel 0 (unswapped) or a colour register whose last writer is a plain add with d = RASC
  on channel 0 (the lightmap stage), maps 6 and 7 unused by the material. Such draws are lit per
  pixel. The WGSL (`port_surface_pbr`): ORM from map 6 and the normal from map 7 on the stage's UVs,
  the tangent frame from derivatives as `pbr_func` does, channel 0's lights summed again with the
  mapped normal (GX's attenuation and diffuse function, ambient and material colour) giving `rast'`;
  `L' = max(L - rast0 + rast', 0)` keeps the unlit part (lightmap); diffuse = base (1 - metal) L'
  ao; per light Karis's GGX lobe ("Real Shading in Unreal Engine 4", SIGGRAPH 2013 notes, eq. 2-4:
  alpha = max(roughness, 0.02)^2, Schlick-Smith G with k = (roughness + 1)^2 / 8 for analytic lights,
  Schlick F with F0 = mix(0.04, base, metal)) times pi and the light colour, in linear light; L' reflected as `lin(L') (F0 A + B) ao`
  (Karis's EnvBRDFApprox); out = `gamma(lin(diffuse) + spec + env)`, clamped as the stage clamps.
  Flat normal, occlusion 1, metal 0: the diffuse is the stage's result exactly, plus the lobe and a
  few percent of reflected light. Checked: naga (30.0.1) validates the emitted WGSL for every
  attenuation/diffuse function and channel source (18 variants, the real generator built against fmt
  12.1.0); `extern/aurora/tests/gx_surface_pbr_test.cpp` checks the predicate and the emitted lines;
  `tests/port_pbr_rules.cpp` the file names and the stage choice. Not yet seen running: the first
  in-game look decides the presets (metal 0.5, `reflective_roughness`) and whether the lobe is too
  strong under the game's lights.
- **First in-game look (2026-11-02): highlights too bright near lights.** GX's lights aren't physical:
  `CLight`'s default point light falls off as 1/distance (distC 0, distL 1, distQ 0), and GX clamps
  each channel's lit sum to 1 (`lighting_func`: `mat * clamp(lighting, 0, 1)`), so the original never
  passes 1 however close a light is. The lobe took `attn` unclamped: head-on at roughness 0.35 its
  peak was 1.3x the light's colour at half a metre and 6.7x at 0.1 m (a metal 15x and 76x), and
  overlapping lights added up. Now each light feeds its lobe at most `min(attn x colour, 1)` and the
  lights together at most 1.0 per channel (GX's clamped sum): the same peaks are 0.67x (a metal 7.6x,
  a glint's centre) wherever the light is and however many overlap. `pbr_highlight` (F1 > Video,
  0-200%, `aurora_set_pbr_highlight`, the `surface_pbr` uniform) scales what is left. Ray or path
  tracing was weighed and left: WebGPU has no ray tracing (acceleration structures and ray queries
  are draft proposals, gpuweb#11054 and #11056), and a software path tracer on WebGPU takes about
  10 ms per sample at 512x512 on an RTX 3080 (arXiv 2407.19977), far from a game frame. An offline
  bake (path-traced room probes, the lightmaps as the radiance a ray finds) is the plan for
  reflections instead.

## Risks

- **Painted lighting.** GameCube diffuse textures carry baked shading and AO, and an upscale keeps
  it. Lit again by PBR it double-darkens. De-lighting fixes that but moves away from the original
  art; the tool should let each texture choose.
- **Upscaler artefacts.** ESRGAN detail is not geometry; a normal map taken straight from it is noisy.
  Low-pass the height before deriving normals, and keep normal strength per preset.
- **One texture, many materials.** A hash is shared across surfaces that should differ (metal trim
  and stone using one texture). The manifest shows where; past that it needs a material-level key,
  which the hash-based pack format can't express.
- **Disc versions.** Hashes differ between `G2ME01`, `G2MP01` and Trilogy; a pack and its PBR set
  belong to one.
- **Memory and Android.** BC7 and BC5 are 1 byte a texel, so one 2048x2048 map is 4 MiB, 5.3 MiB with
  mips, and a full four-map set about 21 MiB per texture; packs run to thousands of textures, so the
  existing streaming has to cover the siblings too. The PBR shader is heavier than most TEV setups;
  the cost tests (`GXSetPBRCostTest`) are the place to measure it on a phone.
- **Licences.** The port's own code is MIT, so nothing GPL goes in the tree: Materialize and DeepBump
  (both GPL-3) are fine as external tools the user installs. Marigold's code is Apache-2.0 and its
  weights CreativeML Open RAIL++-M: a royalty-free grant with use-based restrictions (its
  Attachment A) that must be passed on with the model, and no claim on the outputs; read the terms
  before redistributing the weights. Ubisoft's CHORD weights are research-only. The packs, and anything generated from them, are derived from Nintendo's
  textures: generate locally, never commit or ship them here.

## Phases

1. Manifest (port): record roles, check the classification against a few rooms with F1's draw-id
   view. Small, and it is useful alone (it tells pack authors what they replaced).
2. Tool, fast path: CLI, heuristics, DDS output, naming. Proves the files.
3. Renderer: Aurora sibling loading and slot binding (in MP1 first), the `CCubeModel` hook, the F1
   switch, lightmap as baked diffuse. First in-game result.
4. Tool, learned path and UI: Marigold, presets, preview.
5. Reflections (probes or cubes) and Android cost work.

## Sources

- Aurora PBR shader and API: `extern/aurora/lib/gx/shader.cpp`, `extern/aurora/include/dolphin/gx/GXExtra.h`.
- MP1's hook: `../MetroidPrimePort` `src/Kyoto/Graphics/CCubeModel.cpp` (`GXSetPBR`), `platform/port_remastered_convert_pbr.cpp`.
- Marigold IID Appearance v1.1 model card (outputs, ranges, licence, 768 px): https://huggingface.co/prs-eth/marigold-iid-appearance-v1-1
- Marigold code (Apache-2.0) and weights (RAIL++-M): https://github.com/prs-eth/Marigold
- Ubisoft CHORD (research-only licence): https://huggingface.co/spaces/ksangk/chord-demo/blob/main/README.md, https://www.ubisoft.com/en-us/studio/laforge/news/1i3YOvQX2iArLlScBPqBZs
- Materialize, GPL-3: https://gamefromscratch.com/materialize-now-open-source/
- DeepBump, GPL-3: https://github.com/HugoTini/DeepBump
- DirectXTex (texconv), MIT: https://github.com/microsoft/DirectXTex
- Echoes AI-upscaled pack coverage (2019): https://wccftech.com/metroid-prime-2-echoes-4k-ai-enhanced-texture/, https://dsogaming.com/tag/metroid-prime-2-echoes
- Dolphin forum packs covering Echoes (HD interface; AI packs for 1-3/Trilogy): https://forums.dolphin-emu.org/Thread-metroid-prime-trilogy-hd-interface-textures-v-1-7-may-01-2021?pid=511530, https://forums.dolphin-emu.org/Thread-another-metroid-prime-1-2-3-or-trilogy-hd-texture-pack-ai?pid=525780
