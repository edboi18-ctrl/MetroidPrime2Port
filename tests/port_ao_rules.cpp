// PortAo's rules (platform/include/port_ao_rules.h): the parameters the port's ambient occlusion hands
// Aurora.
#include "port_ao_rules.h"

#include "port_test.h"

#include <cmath>

namespace {
bool Near(float a, float b, float eps = 1e-5f) { return std::fabs(a - b) <= eps; }
} // namespace

int main() {
  using namespace PortAo;
  // A 60 degree vertical view at 16:9 with the near plane at 0.2: top = 0.2 tan 30.
  const float t = 0.2f * std::tan(0.5235988f);
  const float r = t * 16.f / 9.f;
  const Params p = Make(-r, r, t, -t, 0.2f, 750.f, 0.125f, 1.f, kStrengthDefault, kRadiusDefault);
  // The frustum at a view depth of 1: the sides over the near distance.
  CHECK(Near(p.frustum[0], -r / 0.2f));
  CHECK(Near(p.frustum[1], r / 0.2f));
  CHECK(Near(p.frustum[2], std::tan(0.5235988f)));
  CHECK(Near(p.frustum[3], -std::tan(0.5235988f)));
  CHECK(p.depth[0] == 0.2f && p.depth[1] == 750.f && p.depth[2] == 0.125f && p.depth[3] == 1.f);
  CHECK(Near(p.radius, kRadius));
  CHECK(Near(p.intensity, kIntensity));
  CHECK(p.bias == kBias && p.fadeStart == kFadeStart && p.fadeEnd == kFadeEnd);
  CHECK(p.fadeStart < p.fadeEnd);
  // The sliders scale sigma and the radius, clamped to their ranges.
  CHECK(Near(Make(-r, r, t, -t, 0.2f, 750.f, 0.125f, 1.f, 200, 300).intensity, 2.f * kIntensity));
  CHECK(Near(Make(-r, r, t, -t, 0.2f, 750.f, 0.125f, 1.f, 200, 300).radius, 3.f * kRadius));
  CHECK(Make(-r, r, t, -t, 0.2f, 750.f, 0.125f, 1.f, 0, 100).intensity == 0.f);
  CHECK(Near(Make(-r, r, t, -t, 0.2f, 750.f, 0.125f, 1.f, 999, 1).intensity, 2.f * kIntensity));
  CHECK(Near(Make(-r, r, t, -t, 0.2f, 750.f, 0.125f, 1.f, 100, 1).radius, 0.25f * kRadius));
  // Aurora's GXPortAoParams: 16 words, in this order.
  CHECK(sizeof(Params) == 16 * sizeof(float));
  return PORT_TEST_RESULT();
}
