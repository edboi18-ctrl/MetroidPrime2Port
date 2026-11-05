#pragma once

// The port's bloom (F1 > Video > Quality > Bloom; CStateManager::DrawWorld): what it hands Aurora's
// GXPortPostProcess (extern/aurora/lib/gfx/bloom.cpp, Remastered's bloom as MP1's port draws it).
// Header-only for tests/port_bloom_rules.cpp.
//
// MP2 has no Remastered rooms to take a tone curve, a threshold and tints from, so these are the
// port's own. Aurora's bright pass reads the EFB as drawn through a tone curve and undoes it to get
// the light back (bloom.cpp's untone), then blooms the light above the threshold. Retail draws no
// curve, so the one here is the colour as drawn up to kKnee (linear) and a shoulder from there that
// only reaches display white at infinite light: a pixel near white reads as more light than the
// screen shows, as an HDR frame's would, and blooms the more for it. The composite adds the bloom to
// the frame as it is (no curve), so the curve changes nothing else.

#include <algorithm>
#include <cmath>

namespace PortBloom {

// Linear light where the shoulder starts.
constexpr float kKnee = 0.6f;
// bloom.cpp's MaxExposed: the bright pass caps the light it reads there.
constexpr float kMaxExposed = 4.f;

constexpr int kStrengthMin = 0;
constexpr int kStrengthMax = 200;
constexpr int kStrengthDefault = 100;
constexpr int kThresholdMin = 50;
constexpr int kThresholdMax = 95;
constexpr int kThresholdDefault = 90;

// GXSetPBRTone's rows (GXExtra.h): row 0 the toe (a x + b) x^2 + c x below row 1's z, row 1 the
// line S x + y0 from there to row 1's w, row 2 the shoulder x t / (1 + t) + w with t = y x + z.
inline void ToneRows(float rows[3][4]) {
  // The toe is x itself (a = b = 0, c = 1), so it and the line are one: the colour as drawn.
  rows[0][0] = 0.f;
  rows[0][1] = 0.f;
  rows[0][2] = 1.f;
  rows[0][3] = 0.f; // Aurora's exposure slot (post_process_params); 0: no auto exposure
  rows[1][0] = 1.f;
  rows[1][1] = 0.f;
  rows[1][2] = 0.25f;
  rows[1][3] = kKnee;
  // (1 - knee) t / (1 + t) + knee with t = (x - knee) / (1 - knee): the line's value and slope (1)
  // at the knee, white as x goes to infinity.
  const float k = 1.f / (1.f - kKnee);
  rows[2][0] = 1.f - kKnee;
  rows[2][1] = k;
  rows[2][2] = -kKnee * k;
  rows[2][3] = kKnee;
}

// bloom.cpp's tone() and untone() for these rows (the toe is the line, so its Newton steps give x).
inline float Tone(const float rows[3][4], float x) {
  if (x < rows[1][3]) {
    return rows[1][0] * x + rows[1][1];
  }
  const float t = std::max(rows[2][1] * x + rows[2][2], 0.f);
  return rows[2][0] * t / (1.f + t) + rows[2][3];
}

inline float Untone(const float rows[3][4], float y) {
  const float top = rows[2][3];
  if (y < top || rows[2][1] <= 0.f) {
    return (y - rows[1][1]) / rows[1][0];
  }
  const float u = std::min((y - top) / std::max(rows[2][0], 1e-4f), 0.999f);
  return u / (1.f - u) / rows[2][1] + rows[1][3];
}

// The exact piecewise sRGB decode, as bloom.cpp's srgb_dec (the EFB holds sRGB-encoded colour).
inline float SrgbToLinear(float e) {
  e = std::clamp(e, 0.f, 1.f);
  return e <= 0.04045f ? e / 12.92f : std::pow((e + 0.055f) / 1.055f, 2.4f);
}

// The bright pass's threshold, in the light the curve gives back: that of a grey drawn at `percent`
// of display white (the brightness a screen picker shows, 0-100%), so "90" blooms what is drawn
// brighter than 90%.
inline float Threshold(int percent) {
  percent = std::clamp(percent, kThresholdMin, kThresholdMax);
  float rows[3][4];
  ToneRows(rows);
  return std::min(Untone(rows, SrgbToLinear(float(percent) / 100.f)), kMaxExposed);
}

// GXPortPostProcess's tints: 0 to 3 weight the levels from the coarsest (1/64 of the frame) to the
// finest, 4 the bright pass at 1/4 that every level is made from. The levels are weighted alike, so
// the glow is as wide as the coarsest and as tight as the finest; `percent` scales the bright pass,
// so all of it.
constexpr float kBrightTint = 0.08f;
inline void Tints(int percent, float tints[5][3]) {
  const float strength = float(std::clamp(percent, kStrengthMin, kStrengthMax)) / 100.f;
  for (int level = 0; level < 5; ++level) {
    const float tint = level == 4 ? kBrightTint * strength : 1.f;
    for (int c = 0; c < 3; ++c) {
      tints[level][c] = tint;
    }
  }
}

} // namespace PortBloom
