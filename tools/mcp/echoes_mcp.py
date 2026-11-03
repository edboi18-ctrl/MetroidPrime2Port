#!/usr/bin/env python3
"""echoes_mcp: an MCP server for making things with the Echoes port (docs/MCP_WORKSHOP.md).

    py -3 tools\\mcp\\echoes_mcp.py          (Windows; python3 elsewhere) - stdio, for an MCP client

It lets an assistant (Claude Desktop, Claude Code, ...) explain Echoes' script objects from Prime
World Editor's templates, read rooms off your own disc image, put cooked resources into the port's
mods folder, start the game straight into a room, and drive the running game's console.

Configuration is by environment variable (set them in the MCP client's config):
    ECHOES_DISC        your disc image (.iso/.gcm/.ciso)               - room and resource tools
    ECHOES_PORT_EXE    metroid_prime2_port(.exe)                        - echoes_game_launch
    ECHOES_MODS        the mods folder (default <exe folder>/user/mods)  - mods tools
    ECHOES_TEMPLATES   PrimeWorldEditor/templates/MP2 (found beside this repo by default)
    ECHOES_CONSOLE_PORT  the console's TCP port (4780)

Needs `pip install -r tools/mcp/requirements.txt` (the MCP SDK, 2.x). The logic is in workshop.py.
"""
from __future__ import annotations

import functools
import json
import pathlib
import sys
from typing import Annotated, Optional

from pydantic import Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import workshop as ws  # noqa: E402

INSTRUCTIONS = """\
Tools for building on the Metroid Prime 2: Echoes native port.
- Script objects (what a room's Door, Trigger, Relay, SpecialFunction ... are, and their properties)
  come from Prime World Editor's templates: echoes_list_object_types, echoes_explain_template,
  echoes_link_vocabulary.
- Rooms on the user's own disc (read-only): echoes_disc_list_worlds -> echoes_disc_list_areas ->
  echoes_disc_describe_area -> echoes_disc_describe_object. echoes_disc_find_resource says which PAKs
  hold an asset ID.
- The port's mods folder: a file named <8 hex>.<TYPE> replaces that resource in every PAK holding it;
  the port cannot add new IDs. echoes_mods_list, echoes_mods_install_resource, echoes_mods_remove_file.
- The game: echoes_game_launch boots straight into a world and area; echoes_game_status and
  echoes_game_command talk to its debug console (Linux/macOS builds; not yet on Windows).
Typical loop: edit in Prime World Editor -> cook the resource -> echoes_mods_install_resource ->
echoes_game_command(["reloadmods"]) or echoes_game_launch into that room.
Never claim a room or value from memory: read it with these tools."""

mcp = MCPServer("echoes_mcp", instructions=INSTRUCTIONS)

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
GAME = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

_state: dict = {}


def templates() -> ws.Templates:
    if "templates" not in _state:
        _state["templates"] = ws.Templates()
    return _state["templates"]


def disc(required: bool = True) -> Optional[ws.Disc]:
    if "disc" not in _state:
        if not required and not ws.disc_path():
            return None
        _state["disc"] = ws.Disc()
    return _state["disc"]


def tool_errors(fn):
    """Turn workshop failures into tool errors whose text reaches the caller."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ws.WorkshopError as e:
            raise ToolError(str(e)) from None
        except ToolError:
            raise
        except Exception as e:  # unexpected data: say what, so it can be reported
            raise ToolError(f"{type(e).__name__}: {e}") from None
    return wrapper


def page(items: list, limit: int, offset: int) -> tuple:
    chunk = items[offset:offset + limit]
    more = offset + len(chunk) < len(items)
    note = f"\n\n_{len(chunk)} of {len(items)} shown; offset={offset + len(chunk)} for more._" if more else ""
    return chunk, note


# ---------------------------------------------------------------------------- templates
@mcp.tool(name="echoes_list_object_types", annotations=READ, structured_output=False)
@tool_errors
def list_object_types(
    search: Annotated[str, Field(description="Part of a name or FourCC to filter by, e.g. 'door', 'TRGR'. Empty lists all 184.")] = "",
) -> str:
    """List Echoes script object types (FourCC and name) from Prime World Editor's templates."""
    found = templates().search(search)
    if not found:
        return f"No script object type matches {search!r}."
    return "\n".join(f"- `{cc}` {name}" for cc, name in found)


@mcp.tool(name="echoes_explain_template", annotations=READ, structured_output=False)
@tool_errors
def explain_template(
    name: Annotated[str, Field(description="A script object name or FourCC ('Door', 'DOOR') or a struct/enum/flags archetype ('ActorParameters', 'WeaponType').")],
    depth: Annotated[int, Field(description="How many struct levels to expand (1 = top level only).", ge=1, le=6)] = 2,
) -> str:
    """Explain the properties of a script object type or archetype: names, types, IDs, defaults,
    enum values and asset types, as Prime World Editor shows them. Names marked '(name unverified)'
    do not hash to their ID and may be guesses."""
    t = templates()
    fourcc, object_name = t.resolve_object(name)
    if fourcc:
        prop, title = t.object(fourcc), f"# {object_name} (`{fourcc}`)"
    else:
        match = next((a for a in t.archetype_paths if a.lower() == name.strip().lower()), None)
        if match is None:
            raise ws.WorkshopError(f"{name!r} is neither a script object nor an archetype. "
                                   "echoes_list_object_types lists the objects.")
        prop, title = t.archetype(match), f"# {match} ({t.archetype(match).type} archetype)"
    lines = [title, ""]
    if prop.values:
        lines.append("Values: " + ", ".join(f"{k}={v}" for k, v in prop.values.items()))
    if prop.flags:
        lines.append("Flags: " + ", ".join(f"0x{k:X}={v}" for k, v in prop.flags.items()))
    lines += ws.describe_template(prop, depth)
    return "\n".join(lines)


@mcp.tool(name="echoes_link_vocabulary", annotations=READ, structured_output=False)
@tool_errors
def link_vocabulary() -> str:
    """The states and messages script links use (a link is: when <state>, send <message> to <target>)."""
    t = templates()
    states = ", ".join(f"`{k}` {v}" for k, v in sorted(t.states.items()))
    messages = ", ".join(f"`{k}` {v}" for k, v in sorted(t.messages.items()))
    return f"## States\n{states}\n\n## Messages\n{messages}"


# ---------------------------------------------------------------------------- disc (read-only)
@mcp.tool(name="echoes_disc_list_worlds", annotations=READ, structured_output=False)
@tool_errors
def disc_list_worlds() -> str:
    """List the worlds on the configured disc image (name, MLVL ID, PAK, area count)."""
    rows = [f"- **{w['name']}** - MLVL `{w['mlvl']:08X}` in {w['pak']}, {len(w['areas'])} areas"
            for w in disc().worlds()]
    return "\n".join(rows) or "No single-player worlds were read from this disc."


@mcp.tool(name="echoes_disc_list_areas", annotations=READ, structured_output=False)
@tool_errors
def disc_list_areas(
    world: Annotated[str, Field(description="World name, part of it, its MLVL resource name or MLVL ID, e.g. 'Agon Wastes'.")],
    limit: Annotated[int, Field(ge=1, le=200)] = 80,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """List a world's areas (rooms): slot, display name, internal name, MREA ID, script layers and
    which areas each door leads to."""
    d = disc()
    w = d.world(world)
    rows = []
    for a in w["areas"]:
        layers = ", ".join(f"{i}:{n}{'' if on else ' (off)'}" for i, (n, on) in enumerate(a.get("layers", [])))
        to = sorted({w["areas"][ai]["index"] for dock in a["docks"] for ai, _ in dock if ai < len(w["areas"])})
        rows.append(f"- [{a['index']}] **{d.area_name(a)}** `{a['iname']}` MREA `{a['mrea']:08X}`"
                    f" - layers {layers or '?'}; connects to {to or 'nothing'}")
    chunk, note = page(rows, limit, offset)
    return f"# {w['name']} (MLVL `{w['mlvl']:08X}`)\n" + "\n".join(chunk) + note


def _object_summary(t: ws.Templates, decoder: ws.PropertyDecoder, obj: ws.ScriptObject) -> dict:
    name, position, active = "", None, None
    if obj.fourcc in t.objects:
        entries, _ = decoder.struct(t.object(obj.fourcc), obj.properties, 0)
        name = ws.find_value(entries, ws.EDITOR_PROPERTIES, ws.NAME) or ""
        transform = ws.find_value(entries, ws.EDITOR_PROPERTIES, ws.TRANSFORM)
        position = ws.find_value(transform, 0) if isinstance(transform, list) else None
        active = ws.find_value(entries, ws.EDITOR_PROPERTIES, ws.ACTIVE)
    return {"id": f"{obj.editor_id:08x}", "type": obj.fourcc, "type_name": t.names_by_fourcc.get(obj.fourcc, "?"),
            "name": name, "position": position, "active": active, "links": obj.links}


@mcp.tool(name="echoes_disc_describe_area", annotations=READ, structured_output=False)
@tool_errors
def disc_describe_area(
    world: Annotated[str, Field(description="World name or MLVL ID.")],
    area: Annotated[str, Field(description="Area slot number, display name, internal name or MREA ID.")],
    layer: Annotated[Optional[int], Field(description="Only this script layer (0 is the default layer).", ge=0)] = None,
    object_type: Annotated[Optional[str], Field(description="Only objects of this type (name or FourCC).")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 120,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """List a room's script objects per layer: editor ID, type, instance name, active, position and
    links (when STATE, send MESSAGE to TARGET). Editor IDs are what the console's `e<id>` takes."""
    d, t = disc(), templates()
    w = d.world(world)
    a = d.area(w, area)
    decoder = ws.PropertyDecoder(t)
    want = t.resolve_object(object_type)[0] if object_type else None
    if object_type and want is None:
        raise ws.WorkshopError(f"Unknown object type {object_type!r}.")
    layers = d.layers(a)
    layer_names = a.get("layers", [])
    names: dict = {}
    summaries = []
    for li, objects in enumerate(layers):
        for obj in objects:
            s = _object_summary(t, decoder, obj)
            names[obj.editor_id & 0x03FFFFFF] = f"{s['name'] or s['type_name']}"
            if (layer is None or layer == li) and (want is None or obj.fourcc == want):
                summaries.append((li, s))
    rows, last_layer = [], None
    for li, s in summaries:
        if li != last_layer:
            label = layer_names[li][0] if li < len(layer_names) else ""
            rows.append(f"## Layer {li} {label}")
            last_layer = li
        pos = "" if s["position"] is None else " at (" + ", ".join(f"{v:g}" for v in s["position"]) + ")"
        off = " (inactive)" if s["active"] is False else ""
        rows.append(f"- `{s['id']}` {s['type_name']} **{s['name']}**{pos}{off}")
        for state, message, target in s["links"]:
            rows.append(f"  - when {t.state_name(state)}: {t.message_name(message)} -> "
                        f"`{target:08x}` {names.get(target & 0x03FFFFFF, '(another area or unknown)')}")
    chunk, note = page(rows, limit, offset)
    header = (f"# {d.area_name(a)} (`{a['iname']}`, MREA `{a['mrea']:08X}`) in {w['name']}\n"
              f"{sum(len(o) for o in layers)} objects in {len(layers)} layers\n")
    return header + "\n".join(chunk) + note


@mcp.tool(name="echoes_disc_describe_object", annotations=READ, structured_output=False)
@tool_errors
def disc_describe_object(
    world: Annotated[str, Field(description="World name or MLVL ID.")],
    area: Annotated[str, Field(description="Area slot, name, internal name or MREA ID.")],
    editor_id: Annotated[str, Field(description="The object's editor ID in hex, as echoes_disc_describe_area prints it, e.g. '002801f0'.")],
    include_defaults: Annotated[bool, Field(description="Also list properties still at their template default.")] = False,
) -> str:
    """Every property value of one script object in a room, named from the templates. By default only
    values that differ from the template defaults are shown, which is what makes the object unique."""
    d, t = disc(), templates()
    w = d.world(world)
    a = d.area(w, area)
    try:
        wanted = int(editor_id.strip().lower().removeprefix("e").removeprefix("0x"), 16)
    except ValueError:
        raise ws.WorkshopError(f"{editor_id!r} is not a hex editor ID.") from None
    for li, objects in enumerate(d.layers(a)):
        for obj in objects:
            if obj.editor_id != wanted and (obj.editor_id & 0x03FFFFFF) != (wanted & 0x03FFFFFF):
                continue
            if obj.fourcc not in t.objects:
                return f"`{obj.editor_id:08x}` is a {obj.fourcc}, which has no template."
            entries, _ = ws.PropertyDecoder(t).struct(t.object(obj.fourcc), obj.properties, 0)
            shown = entries if include_defaults else ws.prune_defaults(entries)
            links = [{"when": t.state_name(s), "send": t.message_name(m), "to": f"{g:08x}"} for s, m, g in obj.links]
            result = {"id": f"{obj.editor_id:08x}", "layer": li, "type": f"{t.names_by_fourcc[obj.fourcc]} ({obj.fourcc})",
                      "links": links, "properties": _strip(shown)}
            return ws.to_json(result)
    raise ws.WorkshopError(f"No object {editor_id} in {d.area_name(a)}.")


def _strip(entries: list) -> list:
    """Drop the bookkeeping keys from decoded entries for display."""
    out = []
    for e in entries:
        value = e["value"]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            value = _strip(value)
        item = {"name": e["name"], "value": value}
        if e.get("value_name"):
            item["meaning"] = e["value_name"]
        out.append(item)
    return out


@mcp.tool(name="echoes_disc_find_resource", annotations=READ, structured_output=False)
@tool_errors
def disc_find_resource(
    asset_id: Annotated[str, Field(description="A 32-bit asset ID in hex, e.g. '1A2B3C4D'.")],
) -> str:
    """Which resource type an asset ID is and which PAKs hold it (what a loose mod file would replace)."""
    try:
        rid = int(asset_id.strip().lower().removeprefix("0x"), 16)
    except ValueError:
        raise ws.WorkshopError(f"{asset_id!r} is not hex.") from None
    found = sorted(disc().by_id.get(rid, ()))
    if not found:
        return f"No PAK on this disc holds 0x{rid:08X}; the port's mods folder cannot add it as a loose file."
    return "\n".join(f"- {rtype} in {pak} -> a mod file `{rid:08X}.{rtype}` replaces it" for rtype, pak in found)


# ---------------------------------------------------------------------------- mods folder
@mcp.tool(name="echoes_mods_list", annotations=READ, structured_output=False)
@tool_errors
def mods_list() -> str:
    """List the port's mods folder: each mod and what the port will do with each of its files
    (replace a resource, replace a disc file, or ignore it and why). Uses the disc, when set, to check."""
    folder = ws.mods_dir()
    mods = ws.scan_mods(folder, disc(required=False))
    if not mods:
        return f"No mods in {folder} (each mod is a folder there; later names win)."
    return f"Mods folder: {folder}\n" + ws.to_json(mods)


@mcp.tool(name="echoes_mods_install_resource", annotations=WRITE, structured_output=False)
@tool_errors
def mods_install_resource(
    source_path: Annotated[str, Field(description="A cooked resource file, e.g. one Prime World Editor wrote, or a file named <ID>.<TYPE>.")],
    mod: Annotated[str, Field(description="The mod folder to put it in (created if missing), e.g. 'my_first_room'.")],
    asset_id: Annotated[Optional[str], Field(description="The 8-hex-digit asset ID it replaces, when the file is not named <ID>.<TYPE>.")] = None,
    type: Annotated[Optional[str], Field(description="The resource type it replaces (TXTR, CMDL, MREA, STRG, ...), with asset_id.")] = None,
    force: Annotated[bool, Field(description="Install even when no PAK holds that type and ID (the port will then ignore it).")] = False,
) -> str:
    """Copy a cooked resource into the mods folder as <ID>.<TYPE>, so the port uses it in place of the
    disc's. Refuses (unless force) when no PAK holds that resource, since the port only replaces."""
    result = ws.install_resource(source_path, mod, asset_id, type, disc(required=False), force)
    return ws.to_json(result) + "\nApply it with echoes_game_command(['reloadmods']) in a running game, or a restart."


@mcp.tool(name="echoes_mods_remove_file", annotations=DELETE, structured_output=False)
@tool_errors
def mods_remove_file(
    mod: Annotated[str, Field(description="The mod folder name.")],
    file: Annotated[str, Field(description="The file's path inside the mod, as echoes_mods_list shows it.")],
) -> str:
    """Delete one file from a mod folder (only inside the mods folder)."""
    return f"Deleted {ws.remove_mod_file(mod, file)}."


# ---------------------------------------------------------------------------- the game
@mcp.tool(name="echoes_game_launch", annotations=GAME, structured_output=False)
@tool_errors
def game_launch(
    world: Annotated[Optional[str], Field(description="Boot into this world (name or MLVL ID). Needs a save on the card, or starts a new game.")] = None,
    area: Annotated[Optional[str], Field(description="And this area (slot, name, internal name or MREA ID); the world's first area if omitted.")] = None,
) -> str:
    """Start the port (ECHOES_PORT_EXE). With a world it boots straight into that room through
    MP2_WORLD + MP2_SKIP_FRONTEND, skipping the menus. Works on Windows."""
    result = ws.launch_game(world, area, disc(required=bool(world)))
    return ws.to_json(result)


@mcp.tool(name="echoes_game_status", annotations=READ, structured_output=False)
@tool_errors
def game_status() -> str:
    """The running game's state (world, area, position, health, visor, beam ...) from `status --json`."""
    reply = ws.console(["status --json"])
    for line in reply.splitlines():
        if line.startswith("{"):
            return ws.to_json(json.loads(line))
    return reply


@mcp.tool(name="echoes_game_command", annotations=GAME, structured_output=False)
@tool_errors
def game_command(
    commands: Annotated[list[str], Field(description="Console lines, one command each; 'a; b' runs both in one tick. Examples: 'help', 'areas', 'warp 12', 'reloadmods', 'reloadroom', 'obj e002801f0', 'layer 1', 'god 1'.", min_length=1, max_length=50)],
) -> str:
    """Run debug console commands in the running game (docs/CONSOLE.md lists them all; `help` prints
    them). Each reply ends in '=> ok' or '=> err: ...'."""
    return ws.console(commands)


if __name__ == "__main__":
    mcp.run()
