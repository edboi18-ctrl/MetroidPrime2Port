// The double-tap dash detector (platform/include/port_double_tap.h, MP1 dead-space-prime
// tests/port_double_tap.cpp).
#include "port_double_tap.h"

#include "port_test.h"

namespace {

using PortDoubleTap::Detector;

// A poll with only `direction` down (-1: none) at `ms`.
int Poll(Detector& d, int direction, double ms) {
  bool held[PortDoubleTap::kDirections] = {};
  if (direction >= 0) held[direction] = true;
  return d.Update(held, ms);
}
} // namespace

int main() {
  using namespace PortDoubleTap;

  // Tap, let go, tap again within the window: a dash, on the second press.
  {
    Detector d;
    CHECK(Poll(d, kLeft, 0.0) == -1);
    CHECK(Poll(d, -1, 60.0) == -1);
    CHECK(Poll(d, kLeft, 180.0) == kLeft);
    CHECK(Poll(d, kLeft, 200.0) == -1); // held on: still the one dash
  }

  // Every direction works, forward and back included.
  for (int dir = 0; dir < kDirections; ++dir) {
    Detector d;
    Poll(d, dir, 0.0);
    Poll(d, -1, 50.0);
    CHECK(Poll(d, dir, 100.0) == dir);
  }

  // Too slow: the second press only starts a new count, which a third can finish.
  {
    Detector d;
    Poll(d, kRight, 0.0);
    Poll(d, -1, 50.0);
    CHECK(Poll(d, kRight, 300.0) == -1);
    Poll(d, -1, 350.0);
    CHECK(Poll(d, kRight, 450.0) == kRight);
  }

  // Holding a key down is no tap: walking forward never dashes.
  {
    Detector d;
    for (double ms = 0.0; ms < 2000.0; ms += 16.0) CHECK(Poll(d, kForward, ms) == -1);
    // Held past the window, let go and pressed again: measured from the first press.
    CHECK(Poll(d, -1, 2000.0) == -1);
    CHECK(Poll(d, kForward, 2050.0) == -1);
  }

  // A third quick tap after a dash starts a new count rather than dashing again.
  {
    Detector d;
    Poll(d, kBack, 0.0);
    Poll(d, -1, 40.0);
    CHECK(Poll(d, kBack, 80.0) == kBack);
    Poll(d, -1, 120.0);
    CHECK(Poll(d, kBack, 160.0) == -1);
  }

  // Another direction pressed in between starts over.
  {
    Detector d;
    Poll(d, kLeft, 0.0);
    Poll(d, -1, 40.0);
    Poll(d, kRight, 80.0);
    Poll(d, -1, 120.0);
    CHECK(Poll(d, kLeft, 160.0) == -1);
  }

  // A key held while another is double-tapped (walking forward, dashing left).
  {
    Detector d;
    bool held[kDirections] = {};
    held[kForward] = true;
    CHECK(d.Update(held, 0.0) == -1);
    held[kLeft] = true;
    CHECK(d.Update(held, 100.0) == -1);
    held[kLeft] = false;
    CHECK(d.Update(held, 150.0) == -1);
    held[kLeft] = true;
    CHECK(d.Update(held, 200.0) == kLeft);
  }

  // Reset forgets a pending tap; a clock that goes backwards never dashes.
  {
    Detector d;
    Poll(d, kLeft, 0.0);
    Poll(d, -1, 40.0);
    d.Reset();
    CHECK(Poll(d, kLeft, 80.0) == -1);
    Poll(d, -1, 100.0);
    CHECK(Poll(d, kLeft, 50.0) == -1);
  }

  return PORT_TEST_RESULT();
}
