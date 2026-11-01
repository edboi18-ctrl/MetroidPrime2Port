#pragma once

// MP2_TEXTURE_MANIFEST (step 1 of docs/research/PBR_TEXTURES.md): what each texture a model is drawn
// with is used for. Once per material and texture slot, CCubeMaterial::SetCurrent records the
// texture's texture-pack file name with the raw facts its role follows from: the material's flags
// and blend, and every TEV stage and texgen of the material as CGX mirrors them. One JSON object a
// line, appended, so several play sessions build one manifest; tools/pbr/texture_report.py joins it
// with a pack and classifies the roles (lightmap, diffuse, glow, reflection, alpha mask), where the
// rules can change without rebuilding the game.
//
// MP2_TEXTURE_MANIFEST=1 writes texture_manifest.jsonl beside the log file (the user folder), or in
// the working directory when there is no log file; any other value but a false one ("0", "off") is
// the file's path.

#include <cstdint>
#include <cstdio>
#include <string>

namespace PortTextureManifest {

// One TEV stage of the material, as CGX mirrors it (CGX::STevState).
struct Stage {
  uint32_t orderFlags; // texcoord (bits 0-7), texmap (8-15), colour channel (16-23)
  uint32_t colorIn;    // a, b, c, d: 5 bits each from bit 0 (GXTevColorArg)
  uint32_t alphaIn;    // a, b, c, d: 5 bits each from bit 0 (GXTevAlphaArg)
  uint32_t colorOp;    // op (bits 0-3), bias (4-5), scale (6-7), clamp (8), output register (9-10)
  uint32_t alphaOp;    // as colorOp
  uint8_t kColorSel;
  uint8_t kAlphaSel;
};

struct Material {
  uint32_t flags = 0;         // CCubeMaterial's state flags (kStateFlag_*)
  uint32_t blend = 0;         // the packed blend factors the material data holds
  uint32_t materialIndex = 0; // the surface's material index in its model
  const char* source = "";    // CCubeModel::PortDescribe: "area 5 world model 3", "CMDL 0x..."
  bool tevFromGroup = false;  // the TEV is the material group's (its first material set it)
  const Stage* stages = nullptr;
  int numStages = 0;
  // CGX::STexState::mCoordGen per texcoord: type (bits 0-3), source (4-8), matrix (9-13),
  // normalize (14), post matrix (15-20).
  const uint32_t* texGens = nullptr;
  int numTexGens = 0;
};

bool Enabled();

// Whether `key` still has to be recorded (the manifest is on and has no record for it yet), so a
// caller can skip gathering what Record takes. Record still checks.
bool Wanted(uint64_t key);

// Records texture `slot` (bound to GX_TEXMAP<slot>) of `material`, once per `key` (the caller's
// identity for this material and texture). `texObj` is the texture's GXTexObj.
void Record(uint64_t key, const void* texObj, int texelFormat, int slot, const Material& material);

// The JSON line for one record (no newline). Plain arithmetic and text, so tests check it alone.
inline std::string FormatLine(const std::string& name, int texelFormat, int slot,
                              const Material& m) {
  std::string out;
  out.reserve(256 + 64 * static_cast< size_t >(m.numStages));
  char buf[128];
  out += "{\"name\":\"";
  for (const char c : name) {
    if (c == '"' || c == '\\') {
      out += '\\';
    }
    out += (static_cast< unsigned char >(c) < 0x20) ? ' ' : c;
  }
  std::snprintf(buf, sizeof(buf), "\",\"texel\":%d,\"slot\":%d,\"flags\":%u,\"blend\":%u,\"mat\":%u,",
                texelFormat, slot, m.flags, m.blend, m.materialIndex);
  out += buf;
  out += "\"src\":\"";
  for (const char* c = m.source != nullptr ? m.source : ""; *c != '\0'; ++c) {
    if (*c == '"' || *c == '\\') {
      out += '\\';
    }
    out += (static_cast< unsigned char >(*c) < 0x20) ? ' ' : *c;
  }
  out += m.tevFromGroup ? "\",\"group\":true,\"stages\":[" : "\",\"group\":false,\"stages\":[";
  for (int i = 0; i < m.numStages; ++i) {
    const Stage& s = m.stages[i];
    std::snprintf(buf, sizeof(buf), "%s[%u,%u,%u,%u,%u,%u,%u]", i == 0 ? "" : ",", s.orderFlags,
                  s.colorIn, s.alphaIn, s.colorOp, s.alphaOp, s.kColorSel, s.kAlphaSel);
    out += buf;
  }
  out += "],\"texgens\":[";
  for (int i = 0; i < m.numTexGens; ++i) {
    std::snprintf(buf, sizeof(buf), "%s%u", i == 0 ? "" : ",", m.texGens[i]);
    out += buf;
  }
  out += "]}";
  return out;
}

} // namespace PortTextureManifest
