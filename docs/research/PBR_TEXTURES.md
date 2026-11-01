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

- **Step 1, the manifest: done (2026-11-01).** `MP2_TEXTURE_MANIFEST=1` and
  `tools/pbr/texture_report.py` (usage in `docs/MODS.md`). The port records raw facts and the
  report classifies, so the role rules can be tuned on real data without a rebuild; they are
  heuristics until checked against a play-through. The pack in use: Venomalia's "Another" Metroid
  Prime 1, 2, 3 or Trilogy HD Texture Pack (4x, HDCube ESRGAN, BC7 DDS, folders by area and by
  enemies/FX/HUD/Samus; marked CC0 on the Dolphin forum:
  https://forums.dolphin-emu.org/Thread-another-metroid-prime-1-2-3-or-trilogy-hd-texture-pack-ai).
  The loader reads its sub-folders (recursive scan), and the report treats HUD/FX/font/UI/menu
  folders as non-surfaces.

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
