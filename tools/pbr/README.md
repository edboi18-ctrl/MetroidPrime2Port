# PBR texture tools

Steps 1 and 2 of `docs/research/PBR_TEXTURES.md`: find out what each texture in an HD texture pack is
used for (wall and floor colour, baked lighting, glow, reflection, mask), then make PBR maps for the
real surfaces. Python 3.8 or later. Run the commands from the folder that holds `tools`.

1. Optional, makes the scan several times faster:

       py -3 -m pip install xxhash

2. Read every model and area material from your disc image (an `.iso`, `.gcm` or `.ciso`; convert
   RVZ/WIA/GCZ to ISO in Dolphin first). Writes `texture_manifest_disc.jsonl`:

       py -3 tools\pbr\disc_texture_scan.py "C:\path\to\Metroid Prime 2 - Echoes.ciso"

3. Join that with your pack. Prints a summary and writes `texture_report.csv`:

       py -3 tools\pbr\texture_report.py texture_manifest_disc.jsonl "C:\path\to\pack folder"

4. Make the PBR maps: a normal map and an occlusion/roughness/metal map for each texture the report
   marks `pbr_candidate=yes`. This needs numpy and Pillow:

       py -3 -m pip install numpy pillow

   and, to compress the maps (else they are four times the size), Microsoft's `texconv.exe` from
   https://github.com/microsoft/DirectXTex/releases (or `winget install Microsoft.DirectXTex.Texconv`),
   put beside `make_pbr_maps.py`. Try 20 textures first, with previews:

       py -3 tools\pbr\make_pbr_maps.py texture_report.csv "C:\path\to\pack folder" --limit 20 --preview

   The maps go to `"<pack folder> PBR"` beside the pack, laid out like it; the pack itself isn't
   touched. Each PNG in its `_preview` folder shows the pack texture, the normal map, the ORM map, the
   texture lit flat and lit with the maps (light from the top left). Then run it without `--limit`
   for all of them. It prints its progress; texconv's BC7 step is the slow part (`--quick` speeds
   it up), and a rerun only remakes what changed. To tune: `--write-settings
   pbr_settings.json`, edit the presets (or add single textures under `"textures"`), and run again
   (a `pbr_settings.json` in the folder you run it from is read without `--settings`, and writing it
   again keeps your edits). For example, a rock whose bright lava cracks come out raised
   (the maps take brightness as height) is fixed with, under `"textures"`:
   `"tex1_128x128_m_042ffe03bbd4ae68_14": {"chroma": 1.0}` (the name is the preview's file name).
   `-h` lists the rest.

   The game doesn't draw these maps yet: that is step 3, the renderer.

   Is your `texture_report.csv` from a scan that didn't print a "sky models" line, or does it lack the
   `reflective` column? Run steps 2 and 3 again: the scan now marks sky models (sky textures are no
   longer candidates), and the report counts the materials that make a texture shiny (a reflection
   layer, mostly scaled by a mask texture: masks get the role `mask` and are no candidates), which the
   maps take as lower roughness, as much as the masks are bright.

(`py -3` is the Windows Python launcher; elsewhere use `python3`, with `/` in paths.)

The game can write the same kind of list as step 2 while you play, as a cross-check: start it with
`MP2_TEXTURE_MANIFEST=1` (PowerShell: `$env:MP2_TEXTURE_MANIFEST=1; .\metroid_prime2_port.exe`) and
it appends `texture_manifest.jsonl` beside its log file; `texture_report.py` reads that file the same
way. More detail: `MODS.md`, "Texture roles from the disc" and "PBR maps".
