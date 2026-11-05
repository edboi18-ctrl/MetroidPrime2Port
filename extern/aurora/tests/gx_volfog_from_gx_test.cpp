#include <cmath>
#include <cstring>

#include <gtest/gtest.h>

#include "gfx/volfog_gx.hpp"

namespace aurora::gfx::volfog {
namespace {

// GXSetFog's perspective coefficients (a / (b - z) - c over window z, before GX's register scaling).
struct GxFog {
  uint32_t type;
  float a, b, c;
};
GxFog persp(uint32_t type, float start, float end, float zNear, float zFar) {
  return {type, zFar * zNear / ((zFar - zNear) * (end - start)), zFar / (zFar - zNear), start / (end - start)};
}

Params base_params(float noise) {
  Params p{};
  p.depth[0] = 0.2f;
  p.depth[1] = 750.f;
  p.depth[2] = 0.125f; // MP2's world depth range
  p.depth[3] = 1.f;
  p.shape[1] = 1.f;
  p.shape[3] = noise;
  return p;
}

// The light the froxels let through at view depth d: volfog.cpp's slice_step integration (lut_at(t0) is
// entry k at slice k's start) and the apply's linear read of the 64 slices at w = sqrt((d - near) /
// (range - near)), with the noise at its mean (1 - s/2).
float froxel_transmit(const Params& p, float d) {
  const float zNear = p.depth[0];
  const float range = p.fog[0];
  float states[LutSize];
  float optical = 0.f;
  for (uint32_t k = 0; k < LutSize; ++k) {
    const float t0 = static_cast<float>(k) / 63.f;
    const float t1 = static_cast<float>(k + 1) / 63.f;
    const float density = (1.f - 0.5f * p.shape[3]) * p.fog[3] * p.lut[k];
    optical += (t1 * t1 - t0 * t0) * range * density * (p.fog[1] + p.fog[2]);
    states[k] = std::exp(-optical);
  }
  const float w = std::sqrt(std::clamp((d - zNear) / (range - zNear), 0.f, 1.f));
  const float pos = w * 64.f - 0.5f;
  const int i = static_cast<int>(std::floor(pos));
  const float fr = pos - static_cast<float>(i);
  const auto at = [&](int j) { return states[std::clamp(j, 0, static_cast<int>(LutSize) - 1)]; };
  return at(i) * (1.f - fr) + at(i + 1) * fr;
}

float gx_transmit(const Params& p, const GxFog& fog, float d) {
  return 1.f - gx_fog_amount(fog.type, fog.a, fog.b, fog.c, gx_window_z(d, p.depth[0], p.depth[1], p.depth[2], p.depth[3]));
}

// The volumetric fog lets through what GX's fog does, at every depth, for linear, exponential and
// squared-exponential fog, near and far, thin and thick, with and without noise.
TEST(GxVolFogFromGx, LetsThroughWhatGxFogDoes) {
  const GxFog fogs[] = {
      persp(GX_FOG_PERSP_LIN, 10.f, 60.f, 0.2f, 750.f),  persp(GX_FOG_PERSP_EXP, 0.f, 35.f, 0.2f, 750.f),
      persp(GX_FOG_PERSP_EXP2, 0.f, 20.f, 0.2f, 750.f), persp(GX_FOG_PERSP_LIN, 30.f, 120.f, 0.2f, 750.f),
      persp(GX_FOG_PERSP_LIN, 0.f, 8.f, 0.2f, 750.f),
  };
  const float color[3] = {0.25f, 0.5f, 0.75f};
  for (const GxFog& fog : fogs) {
    for (const float noise : {0.f, 0.6f}) {
      SCOPED_TRACE(testing::Message() << "type " << fog.type << " c " << fog.c << " noise " << noise);
      Params p = base_params(noise);
      from_gx_fog(p, fog.type, fog.a, fog.b, fog.c, color);
      EXPECT_GT(p.fog[0], p.depth[0]);
      EXPECT_LE(p.fog[0], p.depth[1]);
      float worst = 0.f;
      for (int i = 1; i < 2000; ++i) {
        const float d = p.depth[0] + (240.f - p.depth[0]) * static_cast<float>(i) / 2000.f;
        worst = std::max(worst, std::fabs(froxel_transmit(p, d) - gx_transmit(p, fog, d)));
      }
      EXPECT_LT(worst, 0.015f);
    }
  }
}

// Scatter only, the fog colour as the light (decoded), no cap, no added light, an identity tone.
TEST(GxVolFogFromGx, LightIsTheFogColour) {
  Params p = base_params(0.f);
  const GxFog fog = persp(GX_FOG_PERSP_LIN, 10.f, 60.f, 0.2f, 750.f);
  const float color[3] = {0.f, 0.5f, 1.f};
  p.regionCount = 3;
  p.volume = 7;
  from_gx_fog(p, fog.type, fog.a, fog.b, fog.c, color);
  EXPECT_FLOAT_EQ(p.fog[1], 1.f);
  EXPECT_FLOAT_EQ(p.fog[2], 0.f);
  EXPECT_FLOAT_EQ(p.colorB[0], 0.f);
  EXPECT_NEAR(p.colorB[1], 0.2140f, 1e-4f); // sRGB 0.5
  EXPECT_FLOAT_EQ(p.colorB[2], 1.f);
  EXPECT_FLOAT_EQ(p.colorB[3], 1.f);
  EXPECT_FLOAT_EQ(p.colorA[0] + p.colorA[1] + p.colorA[2], 0.f);
  EXPECT_FLOAT_EQ(p.colorA[3], 1.f);
  EXPECT_GE(p.noise[3], 1e8f);
  EXPECT_EQ(p.regionCount, 0u);
  EXPECT_EQ(p.volume, 0u);
  // volfog.cpp's tone: the cubic below tone[1].z, (t0.x x + t0.y) x + t0.z) x = x.
  EXPECT_FLOAT_EQ(p.tone[0][0], 0.f);
  EXPECT_FLOAT_EQ(p.tone[0][1], 0.f);
  EXPECT_FLOAT_EQ(p.tone[0][2], 1.f);
  EXPECT_GE(p.tone[1][2], 1e8f);
}

// GX_FOG_NONE: nothing to draw (the command processor records nothing).
TEST(GxVolFogFromGx, NoFogLetsEverythingThrough) {
  EXPECT_FLOAT_EQ(gx_fog_amount(GX_FOG_NONE, 1.f, 1.f, 0.f, 0.5f), 0.f);
  EXPECT_EQ(PortFlagFromGxFog, 1u);
  EXPECT_EQ(PortFlagKeepSky, 2u);
}

} // namespace
} // namespace aurora::gfx::volfog
