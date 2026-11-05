// PortBloom's rules (platform/include/port_bloom_rules.h): the tone curve the bloom's bright pass
// undoes, its threshold and its tints.
#include "port_bloom_rules.h"

#include "port_test.h"

#include <cmath>

namespace {
bool Near(float a, float b, float eps = 1e-4f) { return std::fabs(a - b) <= eps; }
} // namespace

int main() {
  using namespace PortBloom;
  float rows[3][4];
  ToneRows(rows);

  // Up to the knee the curve is the colour as drawn: the bright pass reads it back unchanged.
  for (const float x : {0.f, 0.1f, 0.25f, 0.5f, 0.599f}) {
    CHECK(Near(Tone(rows, x), x));
    CHECK(Near(Untone(rows, x), x));
  }
  // The shoulder meets the line at the knee in value and slope, and stays below white.
  CHECK(Near(Tone(rows, kKnee), kKnee));
  const float h = 2e-4f; // the shoulder bends away at once: 1 / (1 + h / 0.4)^2 over this step
  CHECK(Near((Tone(rows, kKnee + h) - Tone(rows, kKnee)) / h, 1.f, 3e-3f));
  CHECK(Tone(rows, 100.f) < 1.f);
  CHECK(Tone(rows, 100.f) > 0.99f);
  // Untone undoes Tone on the shoulder.
  for (const float x : {0.7f, 1.f, 2.f, 3.9f}) {
    CHECK(Near(Untone(rows, Tone(rows, x)), x, 1e-3f));
  }
  // (1 - knee) t / (1 + t) + knee, t = (x - knee) / (1 - knee): x = 1 gives t = 1, so 0.8.
  CHECK(Near(Tone(rows, 1.f), 0.8f));

  // The threshold is the light of a grey drawn at that brightness. 80%: sRGB 0.8 is 0.6038 linear,
  // just past the knee (u = 0.0095, so 0.4 u / (1 - u) + 0.6 = 0.6038).
  CHECK(Near(SrgbToLinear(0.8f), 0.6038f, 1e-4f));
  CHECK(Near(Threshold(80), 0.6038f, 1e-3f));
  // 70% is below the knee: the decode itself.
  CHECK(Near(Threshold(70), SrgbToLinear(0.7f)));
  // The default, 90%: sRGB 0.9 is 0.7874 linear, u = 0.4685, 0.4 u / (1 - u) + 0.6 = 0.9526.
  CHECK(Near(Threshold(kThresholdDefault), 0.9526f, 2e-3f));
  // Brighter thresholds are higher, and stay under the bright pass's cap (else nothing blooms).
  for (int p = kThresholdMin; p < kThresholdMax; ++p) {
    CHECK(Threshold(p) < Threshold(p + 1));
  }
  CHECK(Threshold(kThresholdMax) < kMaxExposed);
  CHECK(Threshold(1000) == Threshold(kThresholdMax));
  CHECK(Threshold(-5) == Threshold(kThresholdMin));

  // The tints: the levels alike, the bright pass scaled by the strength.
  float tints[5][3];
  Tints(kStrengthDefault, tints);
  for (int level = 0; level < 4; ++level) {
    CHECK(tints[level][0] == 1.f && tints[level][1] == 1.f && tints[level][2] == 1.f);
  }
  CHECK(Near(tints[4][0], kBrightTint));
  Tints(200, tints);
  CHECK(Near(tints[4][1], 2.f * kBrightTint));
  Tints(0, tints);
  CHECK(tints[4][2] == 0.f);
  Tints(999, tints);
  CHECK(Near(tints[4][0], 2.f * kBrightTint));

  return PORT_TEST_RESULT();
}
