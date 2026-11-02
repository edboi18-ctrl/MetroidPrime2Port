// PortPbrMaps' pure rules (platform/include/port_pbr_rules.h): which files are PBR maps, and which
// TEV stage of a material lights its base texture.
#include "port_pbr_rules.h"

#include "port_test.h"

#include <cstdint>
#include <string>

namespace {
// CGX::SetTevColorIn's packing: a, b, c, d in 5 bits each.
uint32_t ColorIn(uint32_t a, uint32_t b, uint32_t c, uint32_t d) { return a | b << 5 | c << 10 | d << 15; }
// CGX::SetTevOrder's: texcoord, texmap, channel (GX_COLOR0A0 = 4).
uint32_t Order(uint32_t texCoord, uint32_t texMap) { return texCoord | texMap << 8 | 4u << 16; }
} // namespace

int main() {
  using namespace PortPbrMaps;
  std::string base;
  bool normal = false;

  // The generator's names: the pack file's stem, then _nrm / _orm.
  CHECK(SplitMapName("tex1_256x256_m_0123456789abcdef_14_nrm.dds", base, normal));
  CHECK_EQ(base, std::string("tex1_256x256_m_0123456789abcdef_14.dds"));
  CHECK(normal);
  CHECK(SplitMapName("tex1_64x64_fedcba9876543210_0123456789abcdef_9_orm.dds", base, normal));
  CHECK_EQ(base, std::string("tex1_64x64_fedcba9876543210_0123456789abcdef_9.dds"));
  CHECK(!normal);
  // Any case, and PNG (Aurora reads both); the base is always named .dds.
  CHECK(SplitMapName("TEX1_8x8_0123456789ABCDEF_14_NRM.PNG", base, normal));
  CHECK_EQ(base, std::string("TEX1_8x8_0123456789ABCDEF_14.dds"));
  CHECK(normal);
  // Not maps: the pack texture itself, a preview, a bare suffix, another extension.
  CHECK(!SplitMapName("tex1_256x256_m_0123456789abcdef_14.dds", base, normal));
  CHECK(!SplitMapName("tex1_256x256_m_0123456789abcdef_14_nrm.json", base, normal));
  CHECK(!SplitMapName("_nrm.dds", base, normal));
  CHECK(!SplitMapName("tex1_8x8_0123456789abcdef_14_nrmx.dds", base, normal));

  unsigned map = 99;
  // A room: C0 = RASC + C1 x lightmap (map 0); PREV = C0 x diffuse (map 1); PREV += glow (map 2).
  {
    const uint32_t in[3] = {ColorIn(kPbrCC_ZERO, kPbrCC_C1, kPbrCC_TEXC, kPbrCC_RASC),
                            ColorIn(kPbrCC_ZERO, kPbrCC_C0, kPbrCC_TEXC, kPbrCC_ZERO),
                            ColorIn(kPbrCC_ZERO, kPbrCC_TEXC, 12, 0)};
    const uint32_t order[3] = {Order(0, 0), Order(1, 1), Order(2, 2)};
    CHECK_EQ(ChooseStage(in, order, 3, map), 1);
    CHECK_EQ(map, 1u);
  }
  // A model: PREV = TEXC x RASC (texture as b).
  {
    const uint32_t in[1] = {ColorIn(kPbrCC_ZERO, kPbrCC_TEXC, kPbrCC_RASC, kPbrCC_ZERO)};
    const uint32_t order[1] = {Order(0, 3)};
    CHECK_EQ(ChooseStage(in, order, 1, map), 0);
    CHECK_EQ(map, 3u);
  }
  // Nothing lit by a light: an unlit decal (TEXC x KONST), a glow only, a mask load (C2 = TEXC).
  {
    const uint32_t in[3] = {ColorIn(kPbrCC_ZERO, kPbrCC_TEXC, 14, kPbrCC_ZERO),
                            ColorIn(kPbrCC_ZERO, kPbrCC_TEXC, 12, 0),
                            ColorIn(kPbrCC_ZERO, kPbrCC_ZERO, kPbrCC_ZERO, kPbrCC_TEXC)};
    const uint32_t order[3] = {Order(0, 0), Order(1, 1), Order(2, 2)};
    CHECK_EQ(ChooseStage(in, order, 3, map), -1);
  }
  // A stage past the count isn't read.
  {
    const uint32_t in[2] = {ColorIn(kPbrCC_ZERO, kPbrCC_ZERO, kPbrCC_ZERO, kPbrCC_TEXC),
                            ColorIn(kPbrCC_ZERO, kPbrCC_RASC, kPbrCC_TEXC, kPbrCC_ZERO)};
    const uint32_t order[2] = {Order(0, 0), Order(1, 1)};
    CHECK_EQ(ChooseStage(in, order, 1, map), -1);
    CHECK_EQ(ChooseStage(in, order, 2, map), 1);
  }
  return PORT_TEST_RESULT();
}
