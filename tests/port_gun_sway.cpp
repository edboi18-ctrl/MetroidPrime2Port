// Gun sway (platform/include/port_gun_sway.h, MP1 dead-space-prime tests/port_gun_sway.cpp).
#include "port_gun_sway.h"

#include "port_test.h"

#include <cmath>
#include <initializer_list>

namespace {

constexpr float kPi = 3.14159265f;
constexpr float kMax = 3.f * kPi / 180.f;

bool Near(float a, float b, float tolerance) { return std::fabs(a - b) <= tolerance; }

// Turns the view at `yawRate` and `pitchRate` (radians a second) for `seconds`
// at `hz` ticks a second, starting from (`yaw`, `pitch`), and returns the state.
PortGunSway::State Turn(float yawRate, float pitchRate, float seconds, float hz, float yaw = 0.f,
                        float pitch = 0.f, float maxRadians = kMax) {
  PortGunSway::State s;
  const float dt = 1.f / hz;
  const int ticks = int(seconds * hz + 0.5f);
  for (int i = 0; i <= ticks; ++i) {
    const float y = yaw + yawRate * dt * i;
    const float p = pitch + pitchRate * dt * i;
    // Prime's forward at yaw 0 is +y; a counterclockwise yaw heads toward -x.
    s.Update(-std::sin(y) * std::cos(p), std::cos(y) * std::cos(p), std::sin(p), dt, maxRadians);
  }
  return s;
}
} // namespace

int main() {
  // Still: straight.
  {
    const auto s = Turn(0.f, 0.f, 1.f, 60.f);
    CHECK(s.Yaw() == 0.f && s.Pitch() == 0.f);
  }
  // Turning right (clockwise, yaw falling) at the full rate leans all the way right; left, left.
  {
    const auto right = Turn(-kPi, 0.f, 1.f, 60.f);
    CHECK(Near(right.Yaw(), -kMax, 1e-4f) && Near(right.Pitch(), 0.f, 1e-6f));
    const auto left = Turn(kPi, 0.f, 1.f, 60.f);
    CHECK(Near(left.Yaw(), kMax, 1e-4f));
  }
  // Faster than the full rate leans no further; half the rate, half as far.
  {
    CHECK(Near(Turn(-4.f * kPi, 0.f, 1.f, 60.f).Yaw(), -kMax, 1e-4f));
    CHECK(Near(Turn(-kPi / 2.f, 0.f, 1.f, 60.f).Yaw(), -kMax / 2.f, 1e-4f));
  }
  // Looking up leans it up, down leans it down.
  {
    CHECK(Near(Turn(0.f, kPi / 2.f, 0.6f, 60.f, 0.f, -0.45f).Pitch(), kMax / 2.f, 1e-4f));
    CHECK(Near(Turn(0.f, -kPi / 2.f, 0.6f, 60.f, 0.f, 0.45f).Pitch(), -kMax / 2.f, 1e-4f));
  }
  // Turning across the yaw's wrap at +-pi still reads as a steady turn.
  {
    const auto s = Turn(-kPi, 0.f, 0.5f, 60.f, -kPi + 0.5f);
    CHECK(Near(s.Yaw(), -kMax, 1e-3f));
  }
  // It eases in at the same pace at any tick rate: after 0.1 s, 1 - e^(-0.1 / kFollowTime) of
  // the way.
  for (const float hz : {30.f, 60.f, 120.f, 240.f}) {
    const auto s = Turn(-kPi, 0.f, 0.1f, hz);
    CHECK(Near(s.Yaw(), -kMax * (1.f - std::exp(-0.1f / PortGunSway::kFollowTime)), kMax * 0.01f));
  }
  // Stopping settles it back to straight.
  {
    auto s = Turn(-kPi, 0.f, 1.f, 60.f);
    for (int i = 0; i < 60; ++i)
      s.Update(0.f, 1.f, 0.f, 1.f / 60.f, kMax);
    CHECK(std::fabs(s.Yaw()) < kMax * 0.01f);
  }
  // A cut (the view jumping half a turn in one tick) doesn't lean it.
  {
    PortGunSway::State s;
    s.Update(0.f, 1.f, 0.f, 1.f / 60.f, kMax);
    s.Update(0.f, -1.f, 0.f, 1.f / 60.f, kMax);
    CHECK(s.Yaw() == 0.f && s.Pitch() == 0.f);
  }
  // Off (no lean allowed), a bad tick or a zero direction straighten it and start over.
  {
    auto s = Turn(-kPi, 0.f, 1.f, 60.f);
    s.Update(0.f, 1.f, 0.f, 1.f / 60.f, 0.f);
    CHECK(s.Yaw() == 0.f && s.Pitch() == 0.f);
    s = Turn(-kPi, 0.f, 1.f, 60.f);
    s.Update(0.f, 0.f, 0.f, 1.f / 60.f, kMax);
    CHECK(s.Yaw() == 0.f);
    s = Turn(-kPi, 0.f, 1.f, 60.f);
    s.Update(0.f, 1.f, 0.f, 0.f, kMax);
    CHECK(s.Yaw() == 0.f);
    s.Update(NAN, 1.f, 0.f, 1.f / 60.f, kMax);
    CHECK(s.Yaw() == 0.f);
  }
  // After a reset the first tick only takes the direction.
  {
    auto s = Turn(-kPi, 0.f, 1.f, 60.f);
    s.Reset();
    s.Update(1.f, 0.f, 0.f, 1.f / 60.f, kMax);
    CHECK(s.Yaw() == 0.f);
  }
  return PORT_TEST_RESULT();
}
