#pragma once

// The port's actor shadows (F1 > Video > Quality > Actor shadows; CSimpleShadow::PortRenderSilhouette):
// the matrices Aurora's Remastered actor shadow takes (GXPortActorShadowCasters / GXPortActorShadowDecal,
// extern/aurora/lib/gfx/actor_shadow.cpp and the decal in lib/gx/shader.cpp). Header-only for
// tests/port_actor_shadow_rules.cpp.
//
// Retail's CSimpleShadow drops a round blob straight down from the actor onto the floor it hits. The
// port draws the actor's own models depth only into a 256x256 orthographic map looking straight down
// over the actor (the caster), then redraws the blob's quad as a multiply decal that darkens where the
// map holds something between the quad and the light: the actor's silhouette, in the blob's place.
//
// Aurora's map: depth cleared to 1, casters drawn with LESS, so the map's z must grow away from the
// light; the decal is lit where its own z is <= the map's (LessEqual compare sampler). A world point p
// maps to clip x = (p.x - cx) / r, y = (p.y - cy) / r, z = (top - p.z) / (top - bottom), w = 1: the
// mirrored frame (z against world z) Aurora expects, as it swaps the casters' cull for it.

#include <cmath>

namespace PortActorShadow {

constexpr int kLayers = 16;            // actor_shadow::MaxLayers
constexpr int kStrengthMin = 0;
constexpr int kStrengthMax = 100;
constexpr int kStrengthDefault = 60;
// The decal paints surfaces that face up at least this much (the cosine of 60 degrees from level).
constexpr float kNormalMin = 0.5f;

// GX's view space as the camera has it: its right, up and backward (towards the camera) axes in world
// space, and its position. View = rows (right, up, back) applied to (world - eye).
struct ViewFrame {
  float right[3];
  float up[3];
  float back[3];
  float eye[3];
};

// The shadow's box in world space: centred on (cx, cy), `radius` to each side, from `top` (above the
// actor) down to `bottom` (below the floor); `base` is the actor's lowest point, which the fade counts
// the receiver's depth from.
struct Box {
  float cx, cy, radius;
  float top, bottom, base;
};

inline float Dot3(const float a[3], const float b[3]) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

// The world function a . p + d as a function of the view-space position: with p = R^T v + eye (R's
// rows the view axes), a . p + d = (R a) . v + (a . eye + d).
inline void ToView(const ViewFrame& view, const float a[3], float d, float out[4]) {
  out[0] = Dot3(view.right, a);
  out[1] = Dot3(view.up, a);
  out[2] = Dot3(view.back, a);
  out[3] = Dot3(a, view.eye) + d;
}

// GXPortActorShadowDecal's 64 floats (16 vec4; the casters take the first 16): 0-2 view -> map clip
// x, y, z; 3 the height below `base` (the fade); 4-9 the box's planes (kept where >= 0); 10 (opacity,
// fade start, 1 / fade range, layer); 11 world up in view space and kNormalMin.
inline void Params(const ViewFrame& view, const Box& box, float opacity, float fadeRange, int layer,
                   float params[64]) {
  for (int i = 0; i < 64; ++i) {
    params[i] = 0.f;
  }
  const float r = box.radius > 1e-3f ? box.radius : 1e-3f;
  const float h = box.top - box.bottom > 1e-3f ? box.top - box.bottom : 1e-3f;
  const float x[3] = {1.f / r, 0.f, 0.f};
  const float y[3] = {0.f, 1.f / r, 0.f};
  const float z[3] = {0.f, 0.f, -1.f / h};
  const float down[3] = {0.f, 0.f, -1.f};
  ToView(view, x, -box.cx / r, params + 0);
  ToView(view, y, -box.cy / r, params + 4);
  ToView(view, z, box.top / h, params + 8);
  ToView(view, down, box.base, params + 12);
  // x >= cx - r, x <= cx + r, y the same, z >= bottom, z <= top.
  const float px[3] = {1.f, 0.f, 0.f};
  const float nx[3] = {-1.f, 0.f, 0.f};
  const float py[3] = {0.f, 1.f, 0.f};
  const float ny[3] = {0.f, -1.f, 0.f};
  const float pz[3] = {0.f, 0.f, 1.f};
  const float nz[3] = {0.f, 0.f, -1.f};
  ToView(view, px, -(box.cx - r), params + 16);
  ToView(view, nx, box.cx + r, params + 20);
  ToView(view, py, -(box.cy - r), params + 24);
  ToView(view, ny, box.cy + r, params + 28);
  ToView(view, pz, -box.bottom, params + 32);
  ToView(view, nz, box.top, params + 36);
  params[40] = opacity;
  params[41] = 0.f;
  params[42] = fadeRange > 1e-3f ? 1.f / fadeRange : 0.f;
  params[43] = float(layer);
  const float upWorld[3] = {0.f, 0.f, 1.f};
  params[44] = Dot3(view.right, upWorld);
  params[45] = Dot3(view.up, upWorld);
  params[46] = Dot3(view.back, upWorld);
  params[47] = kNormalMin;
}

// What a row of Params gives at a view-space point (as the shader's dot(vec4(p, 1), row)).
inline float Eval(const float row[4], const float v[3]) { return row[0] * v[0] + row[1] * v[1] + row[2] * v[2] + row[3]; }

// The view-space position of world point p.
inline void WorldToView(const ViewFrame& view, const float p[3], float out[3]) {
  const float d[3] = {p[0] - view.eye[0], p[1] - view.eye[1], p[2] - view.eye[2]};
  out[0] = Dot3(view.right, d);
  out[1] = Dot3(view.up, d);
  out[2] = Dot3(view.back, d);
}

// The decal's darkening at a receiver, as lib/gx/shader.cpp computes it: 1 - (1 - vis) opacity (1 - w)^2,
// w = clamp((height - fade start) / fade range, 0, 1).
inline float DecalColour(float vis, float opacity, float height, float fadeRange) {
  float w = fadeRange > 1e-3f ? height / fadeRange : 0.f;
  w = w < 0.f ? 0.f : w > 1.f ? 1.f : w;
  const float f = 1.f - w;
  return 1.f - (1.f - vis) * opacity * f * f;
}

} // namespace PortActorShadow
