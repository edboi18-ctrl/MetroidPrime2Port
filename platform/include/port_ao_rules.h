#pragma once

// The port's ambient occlusion (F1 > Video > Quality > Ambient occlusion; CStateManager::DrawWorld):
// what it hands Aurora's GXPortAmbientOcclusion (extern/aurora/lib/gfx/ssao.cpp, Scalable Ambient
// Obscurance: McGuire, Mara and Luebke, HPG 2012). Header-only for tests/port_ao_rules.cpp.
//
// The estimator's intensity (sigma) and radius are the port's choice: at sigma 0.6 and 1 unit (about a
// metre in MP2's world) a floor meeting a wall darkens to about 0.5 in the crease and 0.75 to 0.9 on the
// wall just above it, and a flat floor or wall not at all (the estimator's v.n is 0 on a plane).

#include <algorithm>

namespace PortAo {

constexpr int kStrengthMin = 0;
constexpr int kStrengthMax = 200;
constexpr int kStrengthDefault = 100;
constexpr int kRadiusMin = 25;  // percent of kRadius
constexpr int kRadiusMax = 300;
constexpr int kRadiusDefault = 100;

constexpr float kIntensity = 0.6f; // sigma at 100%
constexpr float kRadius = 1.f;     // world units at 100%
// eq. 1's beta: the bias grows with the view depth (z_C beta), against self-occlusion from depth error.
constexpr float kBias = 0.002f;
// The obscurance fades out over these view depths, where the depth buffer's steps grow and fog covers
// the room anyway.
constexpr float kFadeStart = 40.f;
constexpr float kFadeEnd = 80.f;

struct Params {
  float frustum[4]; // left, right, top, bottom at a view depth of 1
  float depth[4];   // near, far, the world's GX z range (min, max)
  float radius;
  float intensity;
  float bias;
  float fadeStart;
  float fadeEnd;
  float pad[3];
};

// From the world's projection (its frustum sides on the near plane, as CGraphics' projection state
// holds them), its depth range and the two settings.
inline Params Make(float left, float right, float top, float bottom, float near, float far, float depthMin,
                   float depthMax, int strengthPercent, int radiusPercent) {
  Params p{};
  const float n = near > 1e-4f ? near : 1e-4f;
  p.frustum[0] = left / n;
  p.frustum[1] = right / n;
  p.frustum[2] = top / n;
  p.frustum[3] = bottom / n;
  p.depth[0] = near;
  p.depth[1] = far;
  p.depth[2] = depthMin;
  p.depth[3] = depthMax;
  p.radius = kRadius * float(std::clamp(radiusPercent, kRadiusMin, kRadiusMax)) / 100.f;
  p.intensity = kIntensity * float(std::clamp(strengthPercent, kStrengthMin, kStrengthMax)) / 100.f;
  p.bias = kBias;
  p.fadeStart = kFadeStart;
  p.fadeEnd = kFadeEnd;
  return p;
}

} // namespace PortAo
