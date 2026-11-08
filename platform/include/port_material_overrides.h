#pragma once

// The material editor's overrides (tools/material_editor, docs/MATERIAL_EDITOR.md): per material, the
// port's enhancement parameters (PBR on or off and its strength, how its maps are read, live
// reflections), kept in `materials.json` files in the mods folder. Nothing of the disc's data changes.
//
// A material is named by its key: FNV-1a 64 of the material's bytes as the disc holds them (its span of
// the material set, CCubeModel::GetMaterialByIndex), then, for each texture index the material lists
// (the words after its flags and count), the set's texture id at that index as the disc's four bytes
// (0xFFFFFFFF for an index past the set's list). The editor computes it from the disc, the game from
// the same bytes in memory, so no asset ids need carrying through the loaders, and materials alike in
// every byte and texture share one entry. Written as 16 lowercase hex digits.
//
// The file is JSON: {"version": 1, "materials": {"<key>": {"<field>": <value>, ...}, ...}}. Fields
// (all optional; one left out keeps the port's own choice): "pbr" (true/false), "pbr_strength" (0..1),
// "roughness", "metal", "normal" (0..4, 1 = the map), "occlusion" (0..1), "highlight" (0..4),
// "reflection" (true: the environment-mapped stages reflect the live probe; false: the painted map),
// "reflection_strength" (0..4), "reflection_blur" (0..100, percent), "note" (text, unused). Unknown
// fields and values of the wrong kind are skipped. Header-only parts here for tests/port_material_overrides.cpp.

#include <algorithm>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <string_view>
#include <unordered_map>

namespace PortMaterialOverrides {

struct Override {
  int pbr = -1;                  // 0 off, 1 on (the maps the texture pack has), -1 the port's choice
  float pbrStrength = -1.f;      // 0..1, -1 unset
  float roughness = 1.f;         // the PBR maps' scales; all 1 reads them as they are
  float metal = 1.f;
  float normal = 1.f;
  float occlusion = 1.f;
  float highlight = 1.f;
  int reflection = -1;           // 0 the painted environment map, 1 the live probe, -1 the port's choice
  float reflectionStrength = -1.f; // 0..4, -1 unset
  float reflectionBlur = -1.f;   // 0..100 (percent), -1 unset
  bool PbrParams() const {
    return roughness != 1.f || metal != 1.f || normal != 1.f || occlusion != 1.f || highlight != 1.f;
  }
};

constexpr uint64_t kFnvOffset = 0xcbf29ce484222325ull;
constexpr uint64_t kFnvPrime = 0x100000001b3ull;

inline uint64_t Fnv(uint64_t h, const uint8_t* data, size_t size) {
  for (size_t i = 0; i < size; ++i) {
    h ^= data[i];
    h *= kFnvPrime;
  }
  return h;
}

inline uint32_t BigWord(const uint8_t* p) {
  return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | uint32_t(p[3]);
}

// The key of a material: its bytes (`size` of them, big-endian as on the disc) and the set's texture
// ids (`textureIds`: `textureCount` big-endian words, as they follow the set's count).
inline uint64_t MaterialKey(const uint8_t* material, size_t size, const uint8_t* textureIds, uint32_t textureCount) {
  uint64_t h = Fnv(kFnvOffset, material, size);
  if (size < 8) {
    return h;
  }
  const uint32_t count = BigWord(material + 4);
  for (uint32_t i = 0; i < count && 8 + 4 * size_t(i) + 4 <= size && i < 32; ++i) {
    const uint32_t index = BigWord(material + 8 + 4 * size_t(i));
    static const uint8_t kNone[4] = {0xFF, 0xFF, 0xFF, 0xFF};
    h = Fnv(h, index < textureCount ? textureIds + 4 * size_t(index) : kNone, 4);
  }
  return h;
}

inline std::string KeyText(uint64_t key) {
  static const char* const kHex = "0123456789abcdef";
  std::string out(16, '0');
  for (int i = 15; i >= 0; --i) {
    out[size_t(i)] = kHex[key & 15];
    key >>= 4;
  }
  return out;
}

inline bool ParseKey(std::string_view text, uint64_t& key) {
  if (text.size() != 16) {
    return false;
  }
  key = 0;
  for (const char c : text) {
    int v;
    if (c >= '0' && c <= '9') {
      v = c - '0';
    } else if (c >= 'a' && c <= 'f') {
      v = c - 'a' + 10;
    } else if (c >= 'A' && c <= 'F') {
      v = c - 'A' + 10;
    } else {
      return false;
    }
    key = (key << 4) | uint64_t(v);
  }
  return true;
}

// --- A JSON reader for the file's shape: objects, strings, numbers, true/false/null, arrays skipped.
class JsonReader {
public:
  explicit JsonReader(std::string_view text) : mText(text) {}
  bool Failed() const { return mFailed; }
  size_t Position() const { return mPos; }

  void Space() {
    while (mPos < mText.size() && std::isspace(static_cast<unsigned char>(mText[mPos]))) {
      ++mPos;
    }
  }
  bool Peek(char c) {
    Space();
    return mPos < mText.size() && mText[mPos] == c;
  }
  bool Take(char c) {
    if (!Peek(c)) {
      return false;
    }
    ++mPos;
    return true;
  }
  bool Expect(char c) {
    if (!Take(c)) {
      mFailed = true;
    }
    return !mFailed;
  }
  bool String(std::string& out) {
    out.clear();
    if (!Expect('"')) {
      return false;
    }
    while (mPos < mText.size()) {
      const char c = mText[mPos++];
      if (c == '"') {
        return true;
      }
      if (c == '\\' && mPos < mText.size()) {
        const char e = mText[mPos++];
        switch (e) {
        case 'n': out += '\n'; break;
        case 't': out += '\t'; break;
        case 'r': out += '\r'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'u':
          // Kept as '?': notes are the only free text, and the game doesn't show them.
          mPos = mPos + 4 <= mText.size() ? mPos + 4 : mText.size();
          out += '?';
          break;
        default: out += e; break;
        }
      } else {
        out += c;
      }
    }
    mFailed = true;
    return false;
  }
  // A value: kind 'n' number (in `number`), 'b' boolean (in `boolean`), 's' string, 'z' null, 'x'
  // anything else (an object or array, skipped).
  char Value(double& number, bool& boolean, std::string& text) {
    Space();
    if (mPos >= mText.size()) {
      mFailed = true;
      return 'x';
    }
    const char c = mText[mPos];
    if (c == '"') {
      String(text);
      return 's';
    }
    if (c == '{' || c == '[') {
      Skip();
      return 'x';
    }
    if (Word("true")) {
      boolean = true;
      return 'b';
    }
    if (Word("false")) {
      boolean = false;
      return 'b';
    }
    if (Word("null")) {
      return 'z';
    }
    const char* begin = mText.data() + mPos;
    char* end = nullptr;
    const std::string copy(begin, std::min<size_t>(mText.size() - mPos, 64));
    number = std::strtod(copy.c_str(), &end);
    const size_t used = size_t(end - copy.c_str());
    if (used == 0) {
      mFailed = true;
      return 'x';
    }
    mPos += used;
    return 'n';
  }
  // Skips one value of any kind.
  void Skip() {
    Space();
    if (mPos >= mText.size()) {
      mFailed = true;
      return;
    }
    const char c = mText[mPos];
    if (c == '{' || c == '[') {
      const char close = c == '{' ? '}' : ']';
      ++mPos;
      if (Take(close)) {
        return;
      }
      for (;;) {
        if (c == '{') {
          std::string name;
          if (!String(name) || !Expect(':')) {
            return;
          }
        }
        Skip();
        if (mFailed || Take(close)) {
          return;
        }
        if (!Expect(',')) {
          return;
        }
      }
    }
    double n;
    bool b;
    std::string s;
    Value(n, b, s);
  }

private:
  bool Word(std::string_view word) {
    if (mText.substr(mPos, word.size()) == word) {
      mPos += word.size();
      return true;
    }
    return false;
  }
  std::string_view mText;
  size_t mPos = 0;
  bool mFailed = false;
};

inline float Clamp(double v, float lo, float hi) {
  return v < lo ? lo : v > hi ? hi : static_cast<float>(v); // NaN -> hi
}

// Reads one material's fields into `o`, a fresh Override (a later file's entry replaces an earlier
// one whole, so fields it leaves out go back to the port's choice).
inline void ParseFields(JsonReader& in, Override& o) {
  if (!in.Expect('{') || in.Take('}')) {
    return;
  }
  for (;;) {
    std::string name;
    if (!in.String(name) || !in.Expect(':')) {
      return;
    }
    double n = 0.0;
    bool b = false;
    std::string s;
    const char kind = in.Value(n, b, s);
    if (in.Failed()) {
      return;
    }
    if (kind == 'b' && name == "pbr") {
      o.pbr = b ? 1 : 0;
    } else if (kind == 'b' && name == "reflection") {
      o.reflection = b ? 1 : 0;
    } else if (kind == 'n') {
      if (name == "pbr_strength") {
        o.pbrStrength = Clamp(n, 0.f, 1.f);
      } else if (name == "roughness") {
        o.roughness = Clamp(n, 0.f, 4.f);
      } else if (name == "metal") {
        o.metal = Clamp(n, 0.f, 4.f);
      } else if (name == "normal") {
        o.normal = Clamp(n, 0.f, 4.f);
      } else if (name == "occlusion") {
        o.occlusion = Clamp(n, 0.f, 1.f);
      } else if (name == "highlight") {
        o.highlight = Clamp(n, 0.f, 4.f);
      } else if (name == "reflection_strength") {
        o.reflectionStrength = Clamp(n, 0.f, 4.f);
      } else if (name == "reflection_blur") {
        o.reflectionBlur = Clamp(n, 0.f, 100.f);
      }
    }
    if (in.Take('}')) {
      return;
    }
    if (!in.Expect(',')) {
      return;
    }
  }
}

// Reads a materials.json into `out` (its entries replacing any already there for the same keys).
// Returns the number of materials read; false `ok` (and `error` set) when the file isn't the shape
// above, keeping what was read before the fault.
inline int ParseFile(std::string_view text, std::unordered_map<uint64_t, Override>& out, bool& ok,
                     std::string& error) {
  JsonReader in(text);
  int count = 0;
  ok = false;
  if (!in.Expect('{')) {
    error = "not a JSON object";
    return 0;
  }
  if (in.Take('}')) {
    ok = true;
    return 0;
  }
  for (;;) {
    std::string name;
    if (!in.String(name) || !in.Expect(':')) {
      break;
    }
    if (name == "materials" && in.Peek('{')) {
      in.Take('{');
      if (!in.Take('}')) {
        for (;;) {
          std::string keyText;
          if (!in.String(keyText) || !in.Expect(':')) {
            break;
          }
          uint64_t key = 0;
          if (ParseKey(keyText, key) && in.Peek('{')) {
            Override o;
            ParseFields(in, o);
            if (in.Failed()) {
              break;
            }
            out[key] = o;
            ++count;
          } else {
            in.Skip();
          }
          if (in.Failed() || in.Take('}')) {
            break;
          }
          if (!in.Expect(',')) {
            break;
          }
        }
      }
    } else {
      in.Skip();
    }
    if (in.Failed() || in.Take('}')) {
      break;
    }
    if (!in.Expect(',')) {
      break;
    }
  }
  if (in.Failed()) {
    error = "malformed JSON near byte " + std::to_string(in.Position());
    return count;
  }
  ok = true;
  return count;
}

// --- The game's side (platform/port_material_overrides.cpp) ---

// Reads every enabled mod's materials.json, in the mods' order (a later mod's entry for a material
// replaces an earlier one's). Called by PortMods::Initialize (start and Reload mods).
void Load();
// Reads the files again when one changed on disk (checked every couple of seconds), so the editor's
// saves show in the running game. Once a frame.
void Poll();
// Whether any override is loaded (else the game skips computing keys).
bool Any();
const Override* Find(uint64_t key);
// What was loaded, for F1: the materials, the files, and the problems.
int Count();
int Files();
const std::string& Problems();

} // namespace PortMaterialOverrides
