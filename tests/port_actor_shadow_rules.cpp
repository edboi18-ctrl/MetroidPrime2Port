// PortActorShadow's rules (platform/include/port_actor_shadow_rules.h): the view -> map matrices and
// the decal parameters the port's actor shadows hand Aurora.
#include "port_actor_shadow_rules.h"

#include "port_test.h"

#include <cmath>

namespace {
bool Near(float a, float b, float eps = 1e-4f) { return std::fabs(a - b) <= eps; }

// A camera at (3, -8, 5) looking along +y, tilted down 30 degrees: right = +x, forward = (0, cos, -sin),
// up = (0, sin, cos), back = -forward.
PortActorShadow::ViewFrame Camera() {
  const float c = std::cos(0.5235988f);
  const float s = std::sin(0.5235988f);
  return {{1.f, 0.f, 0.f}, {0.f, s, c}, {0.f, -c, s}, {3.f, -8.f, 5.f}};
}
} // namespace

int main() {
  using namespace PortActorShadow;
  const ViewFrame view = Camera();
  // An actor centred at (1, 2), its feet at z 0.4, its top at 2.4 (the box's top 0.5 above), the floor at 0.
  const Box box{1.f, 2.f, 1.5f, 2.9f, -1.f, 0.4f};
  float p[64];
  Params(view, box, 0.6f, 20.f, 3, p);

  // The rows act on view-space points as the world mapping does on world points.
  const float world[][3] = {{1.f, 2.f, 0.f}, {2.5f, 0.5f, 2.9f}, {-0.5f, 3.5f, -1.f}, {1.7f, 2.2f, 1.1f}};
  for (const auto& w : world) {
    float v[3];
    WorldToView(view, w, v);
    CHECK(Near(Eval(p + 0, v), (w[0] - box.cx) / box.radius));
    CHECK(Near(Eval(p + 4, v), (w[1] - box.cy) / box.radius));
    CHECK(Near(Eval(p + 8, v), (box.top - w[2]) / (box.top - box.bottom)));
    CHECK(Near(Eval(p + 12, v), box.base - w[2]));
  }
  // The map: the box's corners at clip -1 and 1, its top at depth 0 and bottom at 1, so a caster above
  // a receiver is nearer (smaller z): the receiver fails LessEqual against it and is shadowed.
  {
    float v[3];
    const float top[3] = {box.cx - box.radius, box.cy + box.radius, box.top};
    WorldToView(view, top, v);
    CHECK(Near(Eval(p + 0, v), -1.f));
    CHECK(Near(Eval(p + 4, v), 1.f));
    CHECK(Near(Eval(p + 8, v), 0.f));
    const float bottom[3] = {box.cx + box.radius, box.cy - box.radius, box.bottom};
    WorldToView(view, bottom, v);
    CHECK(Near(Eval(p + 0, v), 1.f));
    CHECK(Near(Eval(p + 4, v), -1.f));
    CHECK(Near(Eval(p + 8, v), 1.f));
    const float head[3] = {1.f, 2.f, 2.4f};
    const float floor[3] = {1.f, 2.f, 0.f};
    float vh[3];
    float vf[3];
    WorldToView(view, head, vh);
    WorldToView(view, floor, vf);
    CHECK(Eval(p + 8, vh) < Eval(p + 8, vf));
  }
  // The mirrored frame Aurora expects (it swaps the casters' cull): x and y keep world's handedness, z
  // runs against world z, so the world mapping's determinant is negative.
  CHECK((1.f / box.radius) * (1.f / box.radius) * (-1.f / (box.top - box.bottom)) < 0.f);

  // The planes keep the inside of the box and cut the outside.
  const auto inside = [&](const float w[3]) {
    float v[3];
    WorldToView(view, w, v);
    for (int i = 0; i < 6; ++i) {
      if (Eval(p + 16 + 4 * i, v) < -1e-4f) {
        return false;
      }
    }
    return true;
  };
  const float in[3] = {1.2f, 2.3f, 0.f};
  const float outX[3] = {2.6f, 2.f, 0.f};
  const float outY[3] = {1.f, 0.4f, 0.f};
  const float below[3] = {1.f, 2.f, -1.5f};
  const float above[3] = {1.f, 2.f, 3.f};
  CHECK(inside(in));
  CHECK(!inside(outX));
  CHECK(!inside(outY));
  CHECK(!inside(below));
  CHECK(!inside(above));

  // The opacity, fade and layer, and world up in view space (the camera's up has z cos 30, back sin 30).
  CHECK(p[40] == 0.6f);
  CHECK(p[41] == 0.f);
  CHECK(Near(p[42], 1.f / 20.f));
  CHECK(p[43] == 3.f);
  CHECK(Near(p[44], 0.f));
  CHECK(Near(p[45], std::cos(0.5235988f)));
  CHECK(Near(p[46], std::sin(0.5235988f)));
  CHECK(p[47] == kNormalMin);
  // A level floor faces straight up: its normal in view space is world up, which passes the test.
  CHECK(p[44] * p[44] + p[45] * p[45] + p[46] * p[46] > kNormalMin);
  for (int i = 48; i < 64; ++i) {
    CHECK(p[i] == 0.f);
  }

  // The decal: fully shadowed under the feet at full opacity, lighter further down, none past the range.
  CHECK(Near(DecalColour(0.f, 0.6f, 0.f, 20.f), 0.4f));
  CHECK(Near(DecalColour(0.f, 0.6f, 10.f, 20.f), 1.f - 0.6f * 0.25f));
  CHECK(Near(DecalColour(0.f, 0.6f, 25.f, 20.f), 1.f));
  CHECK(Near(DecalColour(1.f, 0.6f, 0.f, 20.f), 1.f));

  return PORT_TEST_RESULT();
}
