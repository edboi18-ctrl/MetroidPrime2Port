# PBR texture tools

Step 1 of `docs/research/PBR_TEXTURES.md`: find out what each texture in an HD texture pack is used
for (wall and floor colour, baked lighting, glow, reflection, mask), so that PBR maps are made only
for real surfaces. Python 3.8 or later, standard library only.

1. Optional, makes the scan several times faster:

       py -3 -m pip install xxhash

2. Read every model and area material from your disc image (an `.iso`, `.gcm` or `.ciso`; convert
   RVZ/WIA/GCZ to ISO in Dolphin first). Writes `texture_manifest_disc.jsonl`:

       py -3 tools\pbr\disc_texture_scan.py "C:\path\to\Metroid Prime 2 - Echoes.ciso"

3. Join that with your pack. Prints a summary and writes `texture_report.csv`:

       py -3 tools\pbr\texture_report.py texture_manifest_disc.jsonl "C:\path\to\Dolphin\Load\Textures\G2M"

(`py -3` is the Windows Python launcher; elsewhere use `python3`, with `/` in paths.)

The game can write the same kind of list while you play, as a cross-check: start it with
`MP2_TEXTURE_MANIFEST=1` (PowerShell: `$env:MP2_TEXTURE_MANIFEST=1; .\metroid_prime2_port.exe`) and
it appends `texture_manifest.jsonl` beside its log file; `texture_report.py` reads that file the same
way. More detail: `MODS.md`, "Texture roles from the disc".
