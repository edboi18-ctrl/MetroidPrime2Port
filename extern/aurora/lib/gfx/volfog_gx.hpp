#pragma once

#include "volfog.hpp"

#include <dolphin/gx/GXExtra.h>

#include <algorithm>
#include <cmath>
#include <cstdint>

// Port extension: GX fog drawn as Remastered's volumetric fog (GXPortFogParams' pad[0] flags). A game
// with no Remastered fog data (MP2) hands GXPortVolumetricFog its view, projection and noise and asks
// for the rest from the GX fog it has set; the draws until GXPortVolumetricFogEnd that GX would have
// fogged take the volumetric fog instead (GXState::volFogFromGx).
//
// The froxels let through T = exp(-optical) and add the in-scatter sum exp(-optical) thickness density
// scatter light (volfog.cpp's slice_step). With no absorption and one light, the in-scatter over a span
// is light (1 - T): the frame becomes scene T + light (1 - T), GX's mix(scene, fog colour, f) with
// 1 - T = f, in linear light. So the density over distance is chosen to make the light let through to
// each view depth exactly 1 - f there (f: GX's fog amount, as lib/gx/shader.cpp computes it from the
// window z the depth draws at), the light is the fog colour, decoded, and the tone is the identity
// (the display EFB holds GX's colours as drawn). The noise (strength s, about 1/2 on average) thins
// the density by 1 - s/2 on average, so it is divided out: the fog is as thick as GX's on average,
// thicker and thinner in drifting wisps.
namespace aurora::gfx::volfog {
constexpr uint32_t PortFlagFromGxFog = GX_PORT_FOG_FROM_GX; // pad[0]: the fog, its light and the tone from GX's
constexpr uint32_t PortFlagKeepSky = GX_PORT_FOG_KEEP_SKY;  // pad[0]: the apply leaves the far plane (the sky) alone
constexpr float FromGxMinTransmit = 1e-4f; // the fog is full where less than this gets through

// GX's fog amount at window z (lib/gx/shader.cpp's fog: type, and the decoded a, b, c).
inline float gx_fog_amount(uint32_t type, float a, float b, float c, float z) {
  if ((type & 7u) == 0u) {
    return 0.f; // GX_FOG_NONE
  }
  const float base = (type & 8u) != 0u ? a * z : a / (b - z);
  const float f = std::clamp(base - c, 0.f, 1.f);
  float fz = f;
  switch (type & 7u) {
  case 4u:
    fz = 1.f - std::exp2(-8.f * f);
    break;
  case 5u:
    fz = 1.f - std::exp2(-8.f * f * f);
    break;
  case 6u:
    fz = std::exp2(-8.f * (1.f - f));
    break;
  case 7u:
    fz = std::exp2(-8.f * (1.f - f) * (1.f - f));
    break;
  default:
    break;
  }
  return std::clamp(fz, 0.f, 1.f);
}

// The window z a view depth draws at: GX's perspective depth (0 at near, 1 at far) in the viewport's
// depth range (zmin..zmax), as ssao.cpp's view_depth inverts it.
inline float gx_window_z(float viewDepth, float zNear, float zFar, float zmin, float zmax) {
  const float d = zFar * (viewDepth - zNear) / (viewDepth * (zFar - zNear));
  return zmin + (zmax - zmin) * d;
}

inline float srgb_decode(float e) {
  e = std::clamp(e, 0.f, 1.f);
  return e <= 0.04045f ? e / 12.92f : std::pow((e + 0.055f) / 1.055f, 2.4f);
}

// The optical depth to view depth d: -ln of the light GX's fog lets through there.
inline float gx_optical_depth(uint32_t type, float a, float b, float c, const Params& p, float viewDepth) {
  const float zNear = p.depth[0];
  const float zFar = p.depth[1];
  const float d = std::clamp(viewDepth, zNear, zFar);
  const float f = gx_fog_amount(type, a, b, c, gx_window_z(d, zNear, zFar, p.depth[2], p.depth[3]));
  return -std::log(std::max(1.f - f, FromGxMinTransmit));
}

// Fills p's range, density over distance, scatter, light and tone from the GX fog (`color` as GX
// holds it, 0..1). p.depth (near, far, the GX z range) and p.shape (its noise strength in w) must be
// set; the regions and the ambient volume are cleared.
inline void from_gx_fog(Params& p, uint32_t type, float a, float b, float c, const float color[3]) {
  const float zNear = p.depth[0];
  const float zFar = std::max(p.depth[1], zNear + 1e-3f);
  // The slices reach to where the fog is within 0.1% of the most it gets (the rest reads the last
  // slice): exponential fog never quite covers everything (2^-8 gets through), so its 64 slices would
  // otherwise spread to the far plane and be coarse near the camera.
  const float most = gx_optical_depth(type, a, b, c, p, zFar);
  float range = zFar;
  constexpr int Steps = 512;
  for (int i = 1; i <= Steps; ++i) {
    const float t = static_cast<float>(i) / Steps;
    const float d = zNear + t * t * (zFar - zNear);
    if (gx_optical_depth(type, a, b, c, p, d) >= most - 1e-3f) {
      range = std::min(zFar, d * 1.02f);
      break;
    }
  }
  range = std::max(range, zNear + 1e-2f);
  const float noiseMean = 1.f - 0.5f * std::clamp(p.shape[3], 0.f, 1.f);
  // Slice k adds thickness density (scatter + absorb) to the optical depth, thickness = (t1^2 - t0^2)
  // range with t0 = k/63 and t1 = (k + 1)/63 (slice_step), and the froxel texel k holds the light let
  // through after it. The apply reads that texel's centre at w = (k + 0.5)/64, view depth
  // near + w^2 (range - near), and filters linearly between: so texel k is made to hold GX's light at
  // that depth (sampled at the slice's end instead, the fog came out half a slice too thick).
  float before = 0.f; // the first slice also takes whatever fog there is at the near plane
  for (uint32_t k = 0; k < LutSize; ++k) {
    const float t0 = static_cast<float>(k) / 63.f;
    const float t1 = static_cast<float>(k + 1) / 63.f;
    const float w = (static_cast<float>(k) + 0.5f) / 64.f;
    const float after = gx_optical_depth(type, a, b, c, p, zNear + w * w * (range - zNear));
    const float thickness = (t1 * t1 - t0 * t0) * range;
    p.lut[k] = std::max(after - before, 0.f) / (thickness * noiseMean);
    before = std::max(before, after);
  }
  p.fog[0] = range;
  p.fog[1] = 1.f; // scatter
  p.fog[2] = 0.f; // absorb
  p.fog[3] = 1.f; // density (the height term's scale)
  // The light: no added light, the default volume's white texel (colorB.w) times the fog colour; no cap.
  for (int i = 0; i < 3; ++i) {
    p.colorA[i] = 0.f;
    p.colorB[i] = srgb_decode(color[i]);
  }
  p.colorA[3] = 1.f; // exposure
  p.colorB[3] = 1.f;
  p.noise[3] = 1e9f;
  // The identity tone: the cubic x below a mid point no value reaches, and untone(y) = y.
  const float tone[3][4] = {{0.f, 0.f, 1.f, 0.f}, {1.f, 0.f, 1e9f, 1e9f}, {0.f, 0.f, 0.f, 1e9f}};
  std::copy(&tone[0][0], &tone[0][0] + 12, &p.tone[0][0]);
  p.regionCount = 0;
  p.volume = 0;
}
} // namespace aurora::gfx::volfog
