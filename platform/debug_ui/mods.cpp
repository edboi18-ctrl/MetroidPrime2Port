// F1 overlay: The Mods page: the mods folder's list and reload. The texture pack section under it
// is drawn by game_sections.cpp. State and helpers shared with the other parts are in
// debug_ui_internal.h.

#include "port_mobile.h"
#include "debug_ui_internal.h"

#include "port_material_overrides.h"
#include "port_mods.h"
#include "port_savestate.h"

#include <SDL3/SDL_misc.h>
#include <imgui.h>

#include <algorithm>
#include <string>
#include <vector>

namespace PortDebug {

// The mods folder (port_mods.h): the list is what this launch (or the last reload) loaded.
void DrawMods() {
  ImGui::SeparatorText("Mods");
  const PortMods::Status& status = PortMods::CurrentStatus();
  bool enabled = PortMods::Enabled();
  if (ImGui::Checkbox("Load mods", &enabled)) {
    PortMods::SetEnabled(enabled);
    Changed(true);
  }
  ItemHelp("Layers the folders in the mods folder over the disc. Changes apply on Reload mods "
           "(which rebuilds the room) or after a restart.");
  std::vector< std::string > disabled = PortMods::SplitDisabled(PortMods::Disabled());
  bool changed = enabled != status.active;
  for (const PortMods::ModInfo& mod : status.mods) {
    const bool on = std::find(disabled.begin(), disabled.end(), mod.name) == disabled.end();
    changed = changed || (enabled && on) != mod.enabled;
  }
  if (changed) {
    ImGui::TextColored(ThemeWarnColor(), "Reload the mods to apply.");
  }
  const bool reloading = PortSaveState::ModReloadActive();
  ImGui::BeginDisabled(reloading);
  if (ImGui::Button(reloading ? "Reloading..." : "Reload mods")) {
    PortSaveState::RequestModReload();
  }
  ImGui::EndDisabled();
  ImGui::SetItemTooltip("Reads the mods folder again and rebuilds the room, so replaced models,\n"
                        "textures and sounds show without a restart. Needs a running game.");
#if !defined(PORT_MOBILE)
  ImGui::SameLine();
  if (ImGui::Button("Open mods folder") && !status.folder.empty()) {
    const std::string url = "file://" + status.folder;
    SDL_OpenURL(url.c_str());
  }
#endif
  for (const std::string& message : status.messages) {
    ImGui::TextColored(ThemeBadColor(), "%s", message.c_str());
  }
  // The mods folder's mods; Echoes has no built-in mod sections above this one.
  ImGui::SeparatorText("Other mods");
  bool listed = false;
  for (const PortMods::ModInfo& mod : status.mods) {
    listed = true;
    const auto found = std::find(disabled.begin(), disabled.end(), mod.name);
    bool on = found == disabled.end();
    ImGui::BeginDisabled(!enabled);
    if (ImGui::Checkbox((mod.name + "##mod").c_str(), &on)) {
      if (on) {
        disabled.erase(std::remove(disabled.begin(), disabled.end(), mod.name), disabled.end());
      } else {
        disabled.push_back(mod.name);
      }
      PortMods::SetDisabled(PortMods::JoinDisabled(disabled));
      Changed(true);
    }
    ImGui::EndDisabled();
    ImGui::SameLine();
    if (mod.enabled) {
      ImGui::TextDisabled("%d file(s), %d resource(s)", mod.files, mod.resources);
    } else {
      ImGui::TextDisabled("not loaded");
    }
  }
  if (!listed) {
    ImGui::TextDisabled("No other mods.");
  }
  // The material editor's files (tools/material_editor, docs/MATERIAL_EDITOR.md), read again on change.
  if (PortMaterialOverrides::Files() != 0 || !PortMaterialOverrides::Problems().empty()) {
    ImGui::Text("Material overrides: %d material(s) from %d materials.json file(s)", PortMaterialOverrides::Count(),
                PortMaterialOverrides::Files());
    ImGui::SetItemTooltip("Set with the Material Editor (mp2_material_editor.exe beside the game). Saved\n"
                          "changes show within a couple of seconds, no Reload mods needed.");
    if (!PortMaterialOverrides::Problems().empty()) {
      ImGui::TextColored(ThemeBadColor(), "%s", PortMaterialOverrides::Problems().c_str());
    }
  }
  if (!status.folder.empty()) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
    ImGui::TextWrapped("Folder: %s", status.folder.c_str());
    ImGui::PopStyleColor();
  }
  ImGui::TextWrapped("Each folder in the mods folder is a mod; later names win. A file at a disc "
                     "path (Metroid5.pak, Audio/..., Video/...) replaces that file, and a "
                     "resource named by id and type (1A2B3C4D.TXTR) replaces it in every PAK.");
}

} // namespace PortDebug
