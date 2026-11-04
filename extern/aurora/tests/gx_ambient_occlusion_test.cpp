#include <string>

#include <gtest/gtest.h>

#include "gfx/ssao.hpp"

namespace aurora::gfx::ssao {
namespace {

// GXPortAmbientOcclusion (gfx/ssao.cpp): strings only, no GPU (SceneHdrTest.AmbientOcclusionCompiles
// compiles them where a device is).
TEST(GxAmbientOcclusion, SourceHasEveryPassForBothDepthBuffers) {
  for (const uint32_t samples : {1u, 4u}) {
    SCOPED_TRACE(samples);
    const std::string source = shader_source(samples);
    EXPECT_EQ(source.find("DEPTH_TYPE"), std::string::npos);
    EXPECT_NE(source.find(samples > 1 ? "texture_depth_multisampled_2d" : "var depthTex: texture_depth_2d;"),
              std::string::npos);
    for (const char* entry : {"fn vs_main(", "fn fs_ao(", "fn fs_blur_h(", "fn fs_blur_v(", "fn fs_composite("}) {
      EXPECT_NE(source.find(entry), std::string::npos) << entry;
    }
  }
}

TEST(GxAmbientOcclusion, SamplesTheSpiralThePaperDoes) {
  // SAO (HPG 2012) eq. 6 to 8: 9 samples on 7 turns, the XOR hash rotation.
  const std::string source = shader_source(1);
  EXPECT_NE(source.find("const S = 9;"), std::string::npos);
  EXPECT_NE(source.find("const TAU = 7.0;"), std::string::npos);
  EXPECT_NE(source.find("(3u * ux) ^ (uy + ux * uy)"), std::string::npos);
  // The angle-biased per-sample term and its normalisation.
  EXPECT_NE(source.find("sum += max(0.0, elevation - p.ao.z) * (1.0 - vv / r2);"), std::string::npos);
  EXPECT_NE(source.find("1.0 - p.ao.y * sum / (f32(S) * max(1.0 - p.ao.z, 1e-3))"), std::string::npos);
}

TEST(GxAmbientOcclusion, ParamsAreSixteenWords) { EXPECT_EQ(sizeof(Params), 64u); }

} // namespace
} // namespace aurora::gfx::ssao
