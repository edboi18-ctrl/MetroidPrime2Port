#pragma once

// The port's waves from splashes (F1 > Video > Quality > Water waves): retail's MP2 marks a shot, a
// bomb or Samus hitting the water with a splash effect only (CFluidPlaneManager::CreateSplash). The
// port also starts an expanding ring of waves there, which Aurora draws by tipping the surface the
// live reflection comes off (GXPortSetWaterWaves, lib/gx/shader.cpp's port_water_ripples_code).
// Header-only for tests/port_water_waves.cpp.
//
// Each ring is a packet of deep-water waves of one wavelength. Deep water (deeper than half a
// wavelength) has w^2 = g k, so the crests travel at the phase speed sqrt(g / k) and the packet at
// the group speed, half of it (Wikipedia, "Dispersion (water waves)"): the crests run out through
// the ring as it spreads, as a real splash's do. A ring's height at r from its centre is
// a exp(-((r - R) / s)^2) sin(k r - w t), R = t sqrt(g / k) / 2; its amplitude dies away with time
// and spreads over the ring's growing circumference (as 1 / sqrt(R)), and the packet widens.

#include <cmath>

namespace PortWaterWaves {

constexpr int kMaxWaves = 8; // GX_PORT_WATER_WAVES
constexpr int kRows = 1 + 2 * kMaxWaves;
constexpr int kStrengthMin = 0;
constexpr int kStrengthMax = 400;
constexpr int kStrengthDefault = 100;
constexpr float kGravity = 9.81f;   // m/s^2; a world unit is about a metre
constexpr float kLifetime = 4.f;    // s
constexpr float kDecay = 1.5f;      // s for the amplitude to fall to 1/e
constexpr float kSpreadRadius = 0.3f; // m: the amplitude falls as 1 / sqrt(1 + R / this)
constexpr float kPi = 3.14159265f;

// A splash: where it hit the surface (world), how long ago, and its size (CreateSplash's factor:
// about 0.1 for Samus wading, 1 for the largest).
struct Wave {
  float x = 0.f, y = 0.f, z = 0.f;
  float age = kLifetime;
  float size = 0.f;
  bool Live() const { return age < kLifetime; }
};

inline float Clamp01(float v) { return v < 0.f ? 0.f : v > 1.f ? 1.f : v; }
// 0.5 m for the smallest splash to 1.2 m for the largest.
inline float Wavelength(float size) { return 0.5f + 0.7f * Clamp01(size); }
inline float Wavenumber(float size) { return 2.f * kPi / Wavelength(size); }
inline float PhaseSpeed(float k) { return std::sqrt(kGravity / k); }
inline float Radius(float age, float k) { return 0.5f * PhaseSpeed(k) * age; }
// The packet's half width (1/e of the envelope): a wavelength at first, widening as it goes.
inline float Width(float age, float size) { return Wavelength(size) * (0.75f + 0.25f * age); }
// In world units, at 100% strength: 1.5 cm for the smallest splash to 6 cm for the largest.
inline float Amplitude(float age, float size, int strengthPercent) {
  const float k = Wavenumber(size);
  const float spread = 1.f / std::sqrt(1.f + Radius(age, k) / kSpreadRadius);
  return (0.015f + 0.045f * Clamp01(size)) * (static_cast<float>(strengthPercent) / 100.f) *
         std::exp(-age / kDecay) * spread;
}

// The ring's height and its slope along r (what the shader computes), for the tests.
inline float Height(float r, float a, float k, float phase, float radius, float width) {
  const float x = r - radius;
  return a * std::exp(-(x * x) / (width * width)) * std::sin(k * r - phase);
}
inline float Slope(float r, float a, float k, float phase, float radius, float width) {
  const float x = r - radius;
  const float s2 = width * width;
  const float env = std::exp(-(x * x) / s2);
  const float p = k * r - phase;
  return a * env * (k * std::cos(p) - 2.f * x / s2 * std::sin(p));
}

// Adds a splash in place of the oldest (or a finished) one.
inline void Add(Wave (&waves)[kMaxWaves], float x, float y, float z, float size) {
  int slot = 0;
  for (int i = 1; i < kMaxWaves; ++i) {
    if (waves[i].age > waves[slot].age) {
      slot = i;
    }
  }
  waves[slot] = Wave{x, y, z, 0.f, Clamp01(size)};
}

inline void Age(Wave (&waves)[kMaxWaves], float dt) {
  for (Wave& w : waves) {
    if (w.Live()) {
      w.age += dt;
    }
  }
}

// GXPortSetWaterWaves' rows for the waves on one surface: those inside its box (x and y, and within
// a unit of its height), their centres through `view` (rows: view = row . (world, 1)). `lag` is how
// far the presented frame is behind the game's time (taken off the ages). Returns the count (also in
// rows[0][0]); the unused rows are zero.
inline int Pack(const Wave (&waves)[kMaxWaves], const float view[3][4], float minX, float minY, float maxX,
                float maxY, float surfaceZ, int strengthPercent, float lag, float rows[kRows][4]) {
  for (int r = 0; r < kRows; ++r) {
    for (int c = 0; c < 4; ++c) {
      rows[r][c] = 0.f;
    }
  }
  if (strengthPercent <= 0) {
    return 0;
  }
  int count = 0;
  for (const Wave& w : waves) {
    if (!w.Live() || w.x < minX || w.x > maxX || w.y < minY || w.y > maxY || std::fabs(w.z - surfaceZ) > 1.f) {
      continue;
    }
    const float age = w.age > lag ? w.age - lag : 0.f;
    const float k = Wavenumber(w.size);
    float* centre = rows[1 + 2 * count];
    float* shape = rows[2 + 2 * count];
    for (int i = 0; i < 3; ++i) {
      centre[i] = view[i][0] * w.x + view[i][1] * w.y + view[i][2] * w.z + view[i][3];
    }
    centre[3] = Amplitude(age, w.size, strengthPercent);
    shape[0] = k;
    shape[1] = std::sqrt(kGravity * k) * age; // w t, w = sqrt(g k)
    shape[2] = Radius(age, k);
    shape[3] = Width(age, w.size);
    ++count;
  }
  rows[0][0] = static_cast<float>(count);
  return count;
}

} // namespace PortWaterWaves
