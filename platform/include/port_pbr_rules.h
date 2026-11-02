#pragma once

// The pure parts of PortPbrMaps (port_pbr_maps.h), header-only for tests/port_pbr_rules.cpp: which
// file names are PBR maps, and which TEV stage of a material lights its base texture.

#include <cctype>
#include <cstdint>
#include <string>

namespace PortPbrMaps {

// "<base>_nrm.dds" or "<base>_orm.dds" (also .png; suffix and extension in any case): true, with
// the pack file name the map belongs to ("<base>.dds", as Aurora parses it) and whether it is the
// normal map. make_pbr_maps.py names a pack texture's maps so.
inline bool SplitMapName(const std::string& fileName, std::string& baseName, bool& isNormal) {
  const auto endsWith = [&fileName](const std::string& suffix) {
    if (fileName.size() <= suffix.size()) {
      return false;
    }
    const size_t start = fileName.size() - suffix.size();
    for (size_t i = 0; i < suffix.size(); ++i) {
      if (std::tolower(static_cast< unsigned char >(fileName[start + i])) != suffix[i]) {
        return false;
      }
    }
    return true;
  };
  for (const char* ext : {".dds", ".png"}) {
    for (const bool normal : {true, false}) {
      const std::string suffix = std::string(normal ? "_nrm" : "_orm") + ext;
      if (endsWith(suffix)) {
        baseName = fileName.substr(0, fileName.size() - suffix.size()) + ".dds";
        isNormal = normal;
        return true;
      }
    }
  }
  return false;
}

// GX's colour arguments that matter here (GXTevColorArg).
enum : uint32_t {
  kPbrCC_C0 = 2,
  kPbrCC_C1 = 4,
  kPbrCC_C2 = 6,
  kPbrCC_TEXC = 8,
  kPbrCC_RASC = 10,
  kPbrCC_ZERO = 15,
};

// The stage that lights a material's base texture, from CGX's cached TEV state (colour inputs as
// CGX::SetTevColorIn packs them: a, b, c, d in 5 bits each from bit 0; order flags as
// CGX::SetTevOrder packs them: texcoord, texmap, channel a byte each): the first stage that is
// 0 + lerp(0, L, TEXC) or 0 + lerp(0, TEXC, L), L the lit colour (RASC) or a colour register (a
// room's lightmap stage writes RASC + C1 x lightmap to C0 first). Its texmap goes to `texMap`.
// -1 when no stage is. Aurora checks the rest (port_surface_pbr_stage) and leaves a draw it can't
// relight as it was.
inline int ChooseStage(const uint32_t* colorIn, const uint32_t* order, unsigned count, unsigned& texMap) {
  for (unsigned i = 0; i < count; ++i) {
    const uint32_t a = colorIn[i] & 0x1F;
    const uint32_t b = (colorIn[i] >> 5) & 0x1F;
    const uint32_t c = (colorIn[i] >> 10) & 0x1F;
    const uint32_t d = (colorIn[i] >> 15) & 0x1F;
    if (a != kPbrCC_ZERO || d != kPbrCC_ZERO) {
      continue;
    }
    const uint32_t light = b == kPbrCC_TEXC ? c : c == kPbrCC_TEXC ? b : kPbrCC_ZERO;
    if (light != kPbrCC_RASC && light != kPbrCC_C0 && light != kPbrCC_C1 && light != kPbrCC_C2) {
      continue;
    }
    texMap = (order[i] >> 8) & 0xFF;
    return static_cast< int >(i);
  }
  return -1;
}
} // namespace PortPbrMaps
