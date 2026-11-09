// PortMaterialOverrides' header-only parts (platform/include/port_material_overrides.h): the material
// key the game and tools/material_editor both compute, and the materials.json reader.
#include "port_material_overrides.h"

#include "port_test.h"

#include <cmath>
#include <cstring>

int main() {
  using namespace PortMaterialOverrides;

  // A material of flags 0x10 listing two textures, set indices 1 and 5, then 12 more bytes; the set
  // has three texture ids, so index 5 hashes as 0xFFFFFFFF. The same value as tests/
  // material_editor.py's (FNV-1a 64 over the bytes, then 22222222 and FFFFFFFF).
  const uint8_t material[] = {0, 0, 0, 0x10, 0, 0, 0, 2, 0, 0, 0, 1, 0, 0, 0, 5,
                              0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11};
  const uint8_t ids[] = {0x11, 0x11, 0x11, 0x11, 0x22, 0x22, 0x22, 0x22, 0x33, 0x33, 0x33, 0x33};
  const uint64_t key = MaterialKey(material, sizeof(material), ids, 3);
  CHECK(KeyText(key) == "447d14554c8ab0f3");
  uint64_t parsed = 0;
  CHECK(ParseKey("447d14554c8ab0f3", parsed) && parsed == key);
  CHECK(ParseKey("447D14554C8AB0F3", parsed) && parsed == key);
  CHECK(!ParseKey("447d14554c8ab0f", parsed));
  CHECK(!ParseKey("447d14554c8ab0fg", parsed));
  // Another texture at index 1 is another material.
  const uint8_t otherIds[] = {0x11, 0x11, 0x11, 0x11, 0x44, 0x44, 0x44, 0x44, 0x33, 0x33, 0x33, 0x33};
  CHECK(MaterialKey(material, sizeof(material), otherIds, 3) != key);
  // A byte's change too.
  uint8_t changed[sizeof(material)];
  std::memcpy(changed, material, sizeof(material));
  changed[27] ^= 1;
  CHECK(MaterialKey(changed, sizeof(changed), ids, 3) != key);

  // The file: fields set, clamped, unknown ones and wrong kinds skipped; nested values skipped.
  const char* text = R"({
    "version": 1,
    "editor": {"window": [1, 2, {"x": "y"}]},
    "materials": {
      "447d14554c8ab0f3": {"pbr": false, "pbr_strength": 0.5, "roughness": 1.5, "metal": 9,
                           "normal": 0.25, "occlusion": -1, "highlight": 2, "reflection": true,
                           "reflection_strength": 1.25, "reflection_blur": 30, "note": "Torvus \"rock\"",
                           "unknown": [1, 2], "pbr_strength_typo": 3, "metal_as_text": "x"},
      "00000000000000aa": {},
      "not-a-key": {"pbr": true},
      "00000000000000bb": {"reflection": false, "pbr": "yes"},
      "00000000000000ee": {"luminance": 2.5},
      "00000000000000ef": {"luminance": 20}
    }
  })";
  std::unordered_map<uint64_t, Override> map;
  bool ok = false;
  std::string error;
  CHECK(ParseFile(text, map, ok, error) == 5);
  CHECK(ok);
  CHECK(map.size() == 5);
  const Override& o = map[key];
  CHECK(o.pbr == 0);
  CHECK(o.pbrStrength == 0.5f);
  CHECK(o.roughness == 1.5f);
  CHECK(o.metal == 4.f);
  CHECK(o.normal == 0.25f);
  CHECK(o.occlusion == 0.f);
  CHECK(o.highlight == 2.f);
  CHECK(o.reflection == 1);
  CHECK(o.reflectionStrength == 1.25f);
  CHECK(o.reflectionBlur == 30.f);
  CHECK(o.PbrParams());
  const Override& empty = map[0xaa];
  CHECK(empty.pbr == -1 && empty.reflection == -1 && empty.pbrStrength < 0.f && !empty.PbrParams());
  const Override& b = map[0xbb];
  CHECK(b.reflection == 0 && b.pbr == -1); // "yes" isn't a boolean
  // Luminance: the glow layer's brightness, 0..8; unset is -1 (the setting alone).
  CHECK(o.luminance == -1.f && empty.luminance == -1.f);
  CHECK(map[0xee].luminance == 2.5f && !map[0xee].PbrParams());
  CHECK(map[0xef].luminance == 8.f);

  // A later file's entry replaces the earlier one whole.
  CHECK(ParseFile(R"({"materials": {"447d14554c8ab0f3": {"highlight": 0.5}}})", map, ok, error) == 1);
  CHECK(ok && map[key].highlight == 0.5f && map[key].pbr == -1 && map[key].roughness == 1.f);

  // Malformed: what came before the fault is kept, and the error says so.
  std::unordered_map<uint64_t, Override> partial;
  CHECK(ParseFile(R"({"materials": {"00000000000000cc": {"pbr": true}, "00000000000000dd": {"pbr": tru}}})",
                  partial, ok, error) == 1);
  CHECK(!ok && !error.empty() && partial.count(0xcc) == 1 && partial.count(0xdd) == 0);
  CHECK(ParseFile("[1, 2]", partial, ok, error) == 0 && !ok);
  CHECK(ParseFile("{}", partial, ok, error) == 0 && ok);

  return PORT_TEST_RESULT();
}
