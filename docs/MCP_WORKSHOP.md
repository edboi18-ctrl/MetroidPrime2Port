# Echoes workshop: an MCP server for making things with the port

`tools/mcp/echoes_mcp.py` is a [Model Context Protocol](https://modelcontextprotocol.io) server. Connected
to an assistant (Claude Desktop, Claude Code, or any MCP client), it lets you ask questions such as "what is in
this room and how is it wired?", "what does a SpecialFunction's Function value do?" or "put this texture
in a mod and show me the room", and get answers read from your own disc and from Prime World Editor's (PWE's)
templates, not from memory. It is the first step towards using the port as the base for a new game
(the roadmap is below).

What it reads and writes:

- **Script templates** (read-only): PWE's `templates/MP2` folder, the same data PWE's property editor shows.
- **Your disc image** (read-only): `.iso`, `.gcm` or `.ciso`, through `tools/echoes_disc.py`. Nothing on it
  is written.
- **The mods folder** (writes): copies cooked resources in as `<ID>.<TYPE>` and deletes files you ask it to,
  only inside that folder.
- **The game**: starts it straight into a room, and talks to its debug console (`docs/CONSOLE.md`).

## Set up on Windows

1. Python 3.10 or newer from python.org. In a Command Prompt, from this repository:
   `py -3 -m pip install -r tools\mcp\requirements.txt` (the MCP SDK, 2.x).
2. PWE's templates: in your PrimeWorldEditor checkout, `git submodule update --init templates`. If
   PrimeWorldEditor sits in the same parent folder as this repository it is found by itself; otherwise set
   `ECHOES_TEMPLATES` to its `templates\MP2` folder.
3. Pick the settings (all optional, but each tool group needs its own):

   | Variable | What | Needed by |
   |---|---|---|
   | `ECHOES_DISC` | your Echoes image (`.iso`, `.gcm`, `.ciso`; convert RVZ/WIA/GCZ to ISO in Dolphin) | room, resource and launch-into-room tools; checks in the mods tools |
   | `ECHOES_PORT_EXE` | `metroid_prime2_port.exe` | `echoes_game_launch` |
   | `ECHOES_MODS` | the mods folder; default `user\mods` beside the exe (`MP2_MODS`, `MP2_USER_PATH` work too) | mods tools |
   | `ECHOES_TEMPLATES` | PWE `templates\MP2` | only if not found beside this repository |
   | `ECHOES_CONSOLE_PORT` | the console's port (4780) | game console tools |

4. Connect a client. Use absolute paths. `where python` prints python.exe's.

   **Claude Desktop**: Settings > Developer > Edit Config opens `%APPDATA%\Claude\claude_desktop_config.json`
   ([MCP docs](https://modelcontextprotocol.io/docs/develop/connect-local-servers)); add, then restart
   Claude Desktop:

   ```json
   {
     "mcpServers": {
       "echoes": {
         "command": "C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
         "args": ["C:\\dev\\MetroidPrime2Port\\tools\\mcp\\echoes_mcp.py"],
         "env": {
           "ECHOES_DISC": "C:\\games\\Metroid Prime 2 - Echoes.iso",
           "ECHOES_PORT_EXE": "C:\\games\\echoes-port\\metroid_prime2_port.exe"
         }
       }
     }
   }
   ```

   **Claude Code** ([docs](https://code.claude.com/docs/en/mcp); keep an option such as `--transport`
   between `--env` and the name):

   ```
   claude mcp add --env ECHOES_DISC="C:\games\Metroid Prime 2 - Echoes.iso" --env ECHOES_PORT_EXE=C:\games\echoes-port\metroid_prime2_port.exe --transport stdio echoes -- py -3 C:\dev\MetroidPrime2Port\tools\mcp\echoes_mcp.py
   ```

   Server logs (Claude Desktop): `%APPDATA%\Claude\logs\mcp-server-echoes.log`.

## Tools

| Tool | Does |
|---|---|
| `echoes_list_object_types` | the 184 script object types (FourCC, name), filtered |
| `echoes_explain_template` | a type's or archetype's properties: names, types, IDs, defaults, enum values, asset types |
| `echoes_link_vocabulary` | the states and messages links use (when STATE, send MESSAGE to TARGET) |
| `echoes_disc_list_worlds` | the disc's worlds |
| `echoes_disc_list_areas` | a world's rooms: name, internal name, MREA ID, script layers, connections |
| `echoes_disc_describe_area` | a room's objects per layer: editor ID, type, name, active, position, links |
| `echoes_disc_describe_object` | one object's property values (only those that differ from the defaults, unless asked) |
| `echoes_disc_find_resource` | which type an asset ID is and which PAKs hold it |
| `echoes_disc_room_collision` | a room's collision as the game reads it: counts, each material's flags by name, the octree's shape, and checks of the rules PWE's collision writer follows |
| `echoes_mods_list` | each mod and what the port does with each file (replace, or ignored and why) |
| `echoes_mods_install_resource` | copy a cooked file in as `<ID>.<TYPE>`; refuses an ID no PAK holds, since the port would ignore it |
| `echoes_mods_remove_file` | delete one file inside a mod |
| `echoes_game_launch` | start the port, booting straight into a world and area (`MP2_WORLD` + `MP2_SKIP_FRONTEND`) |
| `echoes_game_status` | `status --json` from the running game |
| `echoes_game_command` | any console command (`warp`, `reloadmods`, `reloadroom`, `obj`, `layer`, `god` ...) |

The loop it supports: change a resource in PWE, cook it, then `echoes_mods_install_resource`, and either
`echoes_game_command(["reloadmods"])` in a running game or `echoes_game_launch` into the room.

**Windows limitation:** the debug console is not built on Windows yet (`platform/port_console.cpp`
prints `MP2_CONSOLE is not supported on Windows`), so `echoes_game_status` and `echoes_game_command` only
work with the Linux and macOS builds for now. `echoes_game_launch`, and every other tool, works on Windows.
Porting the console's socket code to Winsock is the first item of the roadmap.

How it was checked: `tests/mcp_workshop.py` (ctest `mcp_workshop`) builds templates, script layers, an
MLVL, an MREA, a PAK and a GameCube image to the layouts below and runs every tool group against them,
the MCP server included when the SDK is installed. It has not yet been run against a retail disc.
Layouts followed: PWE `CScriptLoader::LoadObjectMP2`/`LoadStructMP2` (objects and properties),
`CWorldLoader::LoadPrimeMLVL` (the layer table), LibCommon `CCRC32` + `NPropertyMap::CalculatePropertyID`
(a property ID is CRC32 of name + type name with no final XOR; 96% of the Echoes templates' 28,797
property names hash to their IDs, the rest are FourCC-style IDs such as `ACTV`),
`platform/port_mods.cpp`/`port_mods_pak.cpp` (the mods folder rules).

**Checking the editor's collision writer on your disc:** ask for `echoes_disc_room_collision` on a few rooms. On retail data, `edge_slots_follow_corners` and `neighbours_share_their_edge` should be all-of-all, and `edge_or_vertex_material_with_bit24` and `bytes_left_over` should be 0. If they are, the format rules PrimeWorldEditor's `CCollisionCooker` writes by are the ones Retro's own data follows. The octree figures (leaves, triangles per leaf, depth) are the numbers to compare the cooker's defaults against (PrimeWorldEditor `docs/ECHOES_ROOMS.md`).

## What a new game needs, and where things stand

The port gives you the engine's source, so a game built on it does not have to stay within what
retail file formats and PWE can express. What can be made today, with sources:

| Content | Today | Where the limit is |
|---|---|---|
| Room scripting: objects, links, layers, doors between existing rooms | **Yes**, in PWE's World Editor (it re-cooks a room's script layers) | `CAreaCooker::CookMREA` rewrites SCLY/SCGN and the dependency and module sections |
| New room geometry, collision, lighting, portals | **No** | `CookMREA` copies every other section from the original file (`mSectionDataBuffers`); nothing in PWE builds them |
| Models | **Not yet for Echoes** | the Model Editor's import (Assimp: .obj/.fbx/.dae/.3ds/.blend) gives placeholder materials (`CMaterialLoader::LoadAssimpMaterial`, a `todo` with a random colour, no textures, built as Prime 1), and Save is enabled only for Prime 1 (`CModelEditorWindow.cpp`); `CModelCooker` does have an Echoes (version 4) path |
| Textures | **Yes**, the easy way: the port's texture pack (`docs/MODS.md`) takes `.png`/`.dds` under Dolphin-style `tex1_<w>x<h>_<hash>...` names (`MP2_DUMP_TEXTURES` dumps the originals to start from); PWE only converts a DXT1 DDS to TXTR | Aurora `lib/gfx/texture_replacement.cpp`; `CTextureEncoder` (DXT1 to CMPR only) |
| Strings, scans, tweaks | **Yes**, PWE cooks STRG, SCAN, CTWK | `CResourceCooker::CookResource` |
| Streamed music | **Replace only**: a mod's `Audio/<name>.dsp` (same DSP format) replaces that disc track | port mods replace existing disc files; only `.thp` movies can be added, so a new track file cannot |
| Sound effects | **No** | no AGSC/MusyX cooker in PWE |
| Brand-new assets (new IDs) | **Only by replacing whole PAKs**: PWE's package cooker (`CPackage::Cook`) writes whole PAKs, which a mod drops in at their disc path (not yet tried with Echoes here) | the port's loose-file loader replaces only IDs a PAK holds (`port_mods.cpp`: "is in no PAK; ignored") |

### Roadmap

1. **Console on Windows**: Winsock in `platform/port_console.cpp` (`WSAStartup`, `SOCKET`, `closesocket`,
   `WSAPoll`), checked by the Windows CI build. This makes the live tools work on Windows.
2. **Loose resources with new IDs**: let `PortMods` append a mod's unknown `<ID>.<TYPE>` to a PAK's table
   (the virtual PAK writer in `port_mods_pak.cpp` already appends data past the original table). New models,
   textures and strings then need no whole-PAK replacement.
3. **An automation socket in PWE**: a localhost JSON server in the editor (Qt's `QTcpServer`) for the open
   project, so the MCP server can list and edit script objects, links and layers through PWE's own undo
   stack (every change Claude makes shows in the editor and can be undone), and cook and export to the
   mods folder in one step. That replaces PWE's Dolphin-only quick play with "play this room in the port".
4. **Models for Echoes**: real materials on import (a diffuse texture and the lightmap/lit TEV stages
   world geometry uses) and Save enabled for Echoes, tested by loading the result in the port.
5. **Room geometry**: either an MREA geometry/collision cooker in PWE, or a port-side loader (behind
   `TARGET_PC`) that builds a room's geometry and collision from a modern format such as glTF. The second
   is likely less work, since the port owns the renderer and the collision code, but it is a design choice
   to make before starting.
6. **Sound**: since the port links MusyX from source, a port-side path that loads WAV/OGG for a sound ID
   is the likely route, rather than writing MusyX sample pools.
