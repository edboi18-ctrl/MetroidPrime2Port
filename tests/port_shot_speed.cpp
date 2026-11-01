// Power Beam speed's lifetime scaling (platform/include/port_shot_speed.h, MP1 dead-space-prime
// tests/port_shot_speed.cpp).
#include "port_shot_speed.h"

#include "port_test.h"

#include <cmath>
#include <initializer_list>

namespace {
using PortShotSpeed::kNoLifetime;
using PortShotSpeed::ScaledLifetime;
} // namespace

int main() {
  // Twice as fast, half as long; 1.5 times, two thirds.
  CHECK(ScaledLifetime(60, 2.f) == 30);
  CHECK(ScaledLifetime(60, 1.5f) == 40);
  // Unchanged at 1.
  CHECK(ScaledLifetime(45, 1.f) == 45);
  // Rounded to whole frames, never below one.
  CHECK(ScaledLifetime(10, 3.f) == 3);
  CHECK(ScaledLifetime(11, 2.f) == 6);
  CHECK(ScaledLifetime(1, 3.f) == 1);
  // The reach (frames x speed) stays within half a frame at the new speed of the original.
  for (const int lifetime : {7, 20, 33, 90, 400}) {
    for (const float scale : {1.25f, 1.5f, 2.f, 2.5f, 3.f}) {
      const float reach = ScaledLifetime(lifetime, scale) * scale;
      CHECK(std::fabs(reach - lifetime) <= scale * 0.5f + 1e-4f);
    }
  }
  // No lifetime stays none; a bad scale changes nothing.
  CHECK(ScaledLifetime(kNoLifetime, 2.f) == kNoLifetime);
  CHECK(ScaledLifetime(60, 0.f) == 60);
  CHECK(ScaledLifetime(60, -2.f) == 60);
  CHECK(ScaledLifetime(60, NAN) == 60);
  CHECK(ScaledLifetime(60, INFINITY) == 60);
  CHECK(ScaledLifetime(0, 2.f) == 0);
  return PORT_TEST_RESULT();
}
