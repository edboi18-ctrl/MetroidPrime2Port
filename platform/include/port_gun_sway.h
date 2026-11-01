#pragma once

// Gun sway (PortOverlay::GunSway, MP1 dead-space-prime 93aff5324): as in Metroid Prime Hunters,
// the arm cannon leans a few degrees the way the view is turning, left, right, up or down, and
// settles back when it stops. The crosshair leans with it and shots go to the crosshair
// (CPlayer::PortGunLean). Plain data over each tick's view direction, header-only, so
// tests/port_gun_sway.cpp checks it without the game.

#include <algorithm>
#include <cmath>

namespace PortGunSway {

// The turn rate, in radians a second, that leans the gun all the way: 180 degrees a second.
// Slower turns lean it in proportion.
constexpr float kFullTurnRate = 3.14159265f;
// How quickly the lean follows the turn: it closes 63% (1 - 1/e) of the gap in this many seconds,
// at any tick rate.
constexpr float kFollowTime = 0.08f;
// A view that jumps further than this in one tick (radians, about 29 degrees) was moved, not
// turned: a cut or a teleport, which doesn't lean the gun.
constexpr float kCutAngle = 0.5f;

namespace detail {
constexpr float kPi = 3.14159265f;

// a - b, wrapped into [-pi, pi].
inline float AngleDelta(float a, float b) {
  float d = std::fmod(a - b, 2.f * kPi);
  if (d > kPi)
    d -= 2.f * kPi;
  if (d < -kPi)
    d += 2.f * kPi;
  return d;
}
} // namespace detail

class State {
public:
  // One tick: the view's forward direction (any length) and the tick's length. `maxRadians` is
  // the most the gun leans; 0 or less keeps it straight.
  void Update(float forwardX, float forwardY, float forwardZ, float dt, float maxRadians) {
    const float flat = std::hypot(forwardX, forwardY);
    if (!(maxRadians > 0.f) || !(dt > 0.f) || !std::isfinite(flat + forwardZ) ||
        (flat == 0.f && forwardZ == 0.f)) {
      Reset();
      return;
    }
    // The game's forward is +y and its yaw turns counterclockwise (seen from above), toward -x.
    const float yaw = std::atan2(-forwardX, forwardY);
    const float pitch = std::atan2(forwardZ, flat);
    float yawTurn = 0.f;
    float pitchTurn = 0.f;
    if (mHasLast) {
      yawTurn = detail::AngleDelta(yaw, mLastYaw);
      pitchTurn = pitch - mLastPitch;
      if (std::fabs(yawTurn) > kCutAngle || std::fabs(pitchTurn) > kCutAngle) {
        yawTurn = pitchTurn = 0.f;
      }
    }
    mLastYaw = yaw;
    mLastPitch = pitch;
    mHasLast = true;

    // Lean toward the turn, in proportion to its rate up to kFullTurnRate.
    const float yawGoal = std::clamp(yawTurn / (dt * kFullTurnRate), -1.f, 1.f) * maxRadians;
    const float pitchGoal = std::clamp(pitchTurn / (dt * kFullTurnRate), -1.f, 1.f) * maxRadians;
    // A first-order follow: over ticks dt1..dtn the gap shrinks by exp(-(dt1+..+dtn)/kFollowTime),
    // the same at any tick rate.
    const float follow = 1.f - std::exp(-dt / kFollowTime);
    mYaw += (yawGoal - mYaw) * follow;
    mPitch += (pitchGoal - mPitch) * follow;
  }

  // Straight, and the next Update only takes the view's direction.
  void Reset() {
    mYaw = mPitch = 0.f;
    mHasLast = false;
  }

  // The lean, in radians: yaw about the view's up axis, positive to the left (counterclockwise,
  // as the game's yaw), and pitch about the view's right axis, positive up.
  float Yaw() const { return mYaw; }
  float Pitch() const { return mPitch; }

private:
  float mYaw = 0.f;
  float mPitch = 0.f;
  float mLastYaw = 0.f;
  float mLastPitch = 0.f;
  bool mHasLast = false;
};

} // namespace PortGunSway
