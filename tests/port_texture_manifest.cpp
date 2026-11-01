// The texture manifest's line format (platform/include/port_texture_manifest.h), which
// tools/pbr/texture_report.py reads: the exact text of a record, quoting, and empty lists.
#include "port_texture_manifest.h"

#include "port_test.h"

#include <string>

int main() {
  using PortTextureManifest::FormatLine;
  using PortTextureManifest::Material;
  using PortTextureManifest::Stage;

  // A two-stage material: texmap 0 through texcoord 0 into stage 0, texmap 1 into stage 1.
  const Stage stages[2] = {
      {0x00FF0000u | (0u << 8) | 0u, 0x0007A1E0u, 0x000E00E7u, 0x00000100u, 0x00000100u, 12, 28},
      {0x00FF0000u | (1u << 8) | 1u, 0x000041E0u, 0x0001E0E7u, 0x00000100u, 0x00000100u, 12, 28},
  };
  const uint32_t texGens[2] = {0x000040u, 0x000050u};
  Material m;
  m.flags = 0x880u;
  m.blend = 0x00000001u;
  m.materialIndex = 7;
  m.source = "area 5 world model 3";
  m.stages = stages;
  m.numStages = 2;
  m.texGens = texGens;
  m.numTexGens = 2;
  const std::string line = FormatLine("tex1_128x128_0123456789abcdef_14.dds", 1, 1, m);
  CHECK_EQ(line, std::string(
                     "{\"name\":\"tex1_128x128_0123456789abcdef_14.dds\",\"texel\":1,\"slot\":1,"
                     "\"flags\":2176,\"blend\":1,\"mat\":7,\"src\":\"area 5 world model 3\","
                     "\"group\":false,\"stages\":[[16711680,500192,917735,256,256,12,28],"
                     "[16711937,16864,123111,256,256,12,28]],\"texgens\":[64,80]}"));

  // Quotes and backslashes are escaped, control characters become spaces; no stages or texgens
  // give empty lists; a group record says so.
  Material empty;
  empty.source = "CMDL \"q\\\x01";
  empty.tevFromGroup = true;
  CHECK_EQ(FormatLine("a\"b", 0, 0, empty),
           std::string("{\"name\":\"a\\\"b\",\"texel\":0,\"slot\":0,\"flags\":0,\"blend\":0,\"mat\":0,"
                       "\"src\":\"CMDL \\\"q\\\\ \",\"group\":true,\"stages\":[],\"texgens\":[]}"));

  // A null source reads as empty.
  Material nullSource;
  nullSource.source = nullptr;
  CHECK(FormatLine("x", 0, 0, nullSource).find("\"src\":\"\"") != std::string::npos);
  return PORT_TEST_RESULT();
}
