#pragma once

// The double-tap dash (PortOverlay::DoubleTapDash, MP1 dead-space-prime a10ee2274): spots a
// movement key pressed, let go and pressed again within a short window. Plain data over each
// poll's key states, header-only, so tests/port_double_tap.cpp checks it without the game.

#include <cmath>

namespace PortDoubleTap {

enum Direction : int { kForward, kBack, kLeft, kRight, kDirections };

// The most time from the first press to the second that still counts.
constexpr double kWindowMs = 250.0;

class Detector {
public:
  // One poll: whether a key for each direction is down, and the poll's clock in milliseconds.
  // Gives the direction pressed for the second time within `windowMs` of its first press, or -1.
  // A press of another direction in between, or a first press held past the window, starts over.
  int Update(const bool held[kDirections], double nowMs, double windowMs = kWindowMs) {
    int fired = -1;
    for (int d = 0; d < kDirections; ++d) {
      const bool pressed = held[d] && !mHeld[d];
      mHeld[d] = held[d];
      if (!pressed)
        continue;
      const bool second = mPending[d] && std::isfinite(nowMs) && nowMs - mFirstPressMs[d] >= 0.0 &&
                          nowMs - mFirstPressMs[d] <= windowMs;
      // Any press starts over every other direction's count.
      for (int other = 0; other < kDirections; ++other) {
        if (other != d)
          mPending[other] = false;
      }
      if (second) {
        mPending[d] = false;
        if (fired < 0)
          fired = d;
      } else {
        mPending[d] = true;
        mFirstPressMs[d] = nowMs;
      }
    }
    return fired;
  }

  void Reset() { *this = Detector(); }

private:
  bool mHeld[kDirections] = {};
  bool mPending[kDirections] = {};
  double mFirstPressMs[kDirections] = {};
};

} // namespace PortDoubleTap
