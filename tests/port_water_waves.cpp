// PortWaterWaves' rules (platform/include/port_water_waves.h): the splash rings' deep-water motion,
// their slope (as the shader computes it), and the rows GXPortSetWaterWaves takes.
#include "port_water_waves.h"

#include "port_test.h"

#include <cmath>

namespace {
bool Near(float a, float b, float eps = 1e-4f) { return std::fabs(a - b) <= eps; }
} // namespace

int main() {
  using namespace PortWaterWaves;

  // Deep water: w^2 = g k, the crests at sqrt(g / k), the ring at half that. A 0.8 m wave (size 3/7)
  // has k = 7.854, crests at 1.118 m/s and the ring at 0.559 m/s.
  const float size = 3.f / 7.f;
  CHECK(Near(Wavelength(size), 0.8f));
  const float k = Wavenumber(size);
  CHECK(Near(k, 7.854f, 1e-3f));
  CHECK(Near(PhaseSpeed(k), 1.1176f, 1e-3f));
  CHECK(Near(Radius(2.f, k), 1.1176f, 1e-3f));
  CHECK(Near(PhaseSpeed(k) * PhaseSpeed(k) * k, kGravity, 1e-3f));

  // The slope is the height's derivative along r.
  for (const float r : {0.f, 0.3f, 1.f, 1.7f, 2.5f}) {
    const float a = 0.04f, phase = 3.1f, radius = 1.2f, width = 0.6f;
    const float h = 1e-3f;
    const float fd = (Height(r + h, a, k, phase, radius, width) - Height(r - h, a, k, phase, radius, width)) / (2 * h);
    CHECK(Near(Slope(r, a, k, phase, radius, width), fd, 2e-4f));
  }

  // The amplitude: 1.5 to 6 cm at the start, scaled by the strength, dying away.
  CHECK(Near(Amplitude(0.f, 0.f, 100), 0.015f));
  CHECK(Near(Amplitude(0.f, 1.f, 100), 0.06f));
  CHECK(Near(Amplitude(0.f, 1.f, 200), 0.12f));
  CHECK(Amplitude(2.f, 1.f, 100) < Amplitude(1.f, 1.f, 100));
  CHECK(Amplitude(0.f, 1.f, 0) == 0.f);

  // Splashes replace the oldest; ageing finishes them.
  Wave waves[kMaxWaves];
  for (const Wave& w : waves) {
    CHECK(!w.Live());
  }
  Add(waves, 1.f, 2.f, 3.f, 0.5f);
  CHECK(waves[0].Live() && waves[0].x == 1.f && waves[0].size == 0.5f);
  Age(waves, 1.f);
  Add(waves, 4.f, 5.f, 3.f, 2.f);
  CHECK(waves[1].Live() && waves[1].size == 1.f);
  for (int i = 2; i < kMaxWaves; ++i) {
    Add(waves, 0.f, 0.f, 3.f, 0.1f);
  }
  Add(waves, 9.f, 9.f, 3.f, 0.1f); // all live: the oldest (slot 0, 1 s) goes
  CHECK(waves[0].x == 9.f);
  Age(waves, kLifetime);
  for (const Wave& w : waves) {
    CHECK(!w.Live());
  }

  // Packing: through the view rows, inside the surface's box only.
  Wave pool[kMaxWaves];
  Add(pool, 1.f, 1.f, 0.f, 0.2f);
  Add(pool, 50.f, 1.f, 0.f, 0.2f); // outside the box
  Age(pool, 0.5f);
  Add(pool, 2.f, 3.f, 0.2f, 1.f);
  // view: x right = world x, y up = world z, z back = -world y, eye at (0, -10, 1).
  const float view[3][4] = {{1.f, 0.f, 0.f, 0.f}, {0.f, 0.f, 1.f, -1.f}, {0.f, -1.f, 0.f, -10.f}};
  float rows[kRows][4];
  const int count = Pack(pool, view, -5.f, -5.f, 5.f, 5.f, 0.f, 100, 0.25f, rows);
  CHECK(count == 2);
  CHECK(rows[0][0] == 2.f);
  // The first (the 0.5 s one, presented 0.25 s behind): centre (1, -1, -11).
  CHECK(Near(rows[1][0], 1.f) && Near(rows[1][1], -1.f) && Near(rows[1][2], -11.f));
  CHECK(Near(rows[1][3], Amplitude(0.25f, 0.2f, 100)));
  const float k1 = Wavenumber(0.2f);
  CHECK(Near(rows[2][0], k1) && Near(rows[2][1], std::sqrt(kGravity * k1) * 0.25f) &&
        Near(rows[2][2], Radius(0.25f, k1)) && Near(rows[2][3], Width(0.25f, 0.2f)));
  // The new one: no age to take the lag off.
  CHECK(Near(rows[3][0], 2.f) && Near(rows[3][1], -0.8f) && Near(rows[3][2], -13.f));
  CHECK(Near(rows[4][2], 0.f));
  for (int r = 5; r < kRows; ++r) {
    CHECK(rows[r][0] == 0.f && rows[r][3] == 0.f);
  }
  CHECK(Pack(pool, view, -5.f, -5.f, 5.f, 5.f, 0.f, 0, 0.f, rows) == 0);
  CHECK(rows[0][0] == 0.f);
  // Another surface (2 units higher) doesn't take them.
  CHECK(Pack(pool, view, -5.f, -5.f, 5.f, 5.f, 2.5f, 100, 0.f, rows) == 0);

  return PORT_TEST_RESULT();
}
