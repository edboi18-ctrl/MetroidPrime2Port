#include "MetroidPrime/CFluidPlaneManager.hpp"

#include "Kyoto/Audio/CSfxManager.hpp"
#include "Kyoto/Math/CMatrix3f.hpp"
#include "MetroidPrime/CExplosion.hpp"
#include "MetroidPrime/CStateManager.hpp"
#include "MetroidPrime/ScriptObjects/CScriptWater.hpp"
#include "rstl/math.hpp"
#ifdef TARGET_PC
#include "MetroidPrime/CActor.hpp"
#include "MetroidPrime/CCameraManager.hpp"
#include "port_overlay.h"
#include "port_water_waves.h"
#endif

CFluidPlaneManager::CFluidProfile CFluidPlaneManager::sProfile;

void CFluidPlaneManager::CFluidProfile::Clear() {
  x10_ = 0.f;
  xc_ = 0.f;
  x8_ = 0.f;
  x4_ = 0.f;
  x0_ = 0.f;
}

CFluidPlaneManager::CFluidPlaneManager()
: mLastSplashPosition(CVector3f::Zero())
, mSplashCooldown(0.f)
, mUvTime(0.f)
, x118_(false)
, mFrameActive(false) {
  sProfile.Clear();
  for (CSplashRecord* it = mSplashes.begin(); it != mSplashes.end(); ++it) {
    it->SetTime(9999.f);
  }
}

#ifdef TARGET_PC
namespace {
float sPortLastDt = 0.f;
uint sPortUpdateGeneration = 0;
// The splashes' rings (port_water_waves.h), kept here rather than in the class (its size is retail's).
PortWaterWaves::Wave sPortWaves[PortWaterWaves::kMaxWaves];
} // namespace

int CFluidPlaneManager::PortPackWaves(const float view[3][4], float minX, float minY, float maxX, float maxY,
                                      float surfaceZ, int strengthPercent, float rows[17][4]) {
  static_assert(PortWaterWaves::kRows == 17);
  return PortWaterWaves::Pack(sPortWaves, view, minX, minY, maxX, maxY, surfaceZ, strengthPercent, PortPresentLag(),
                              rows);
}

float CFluidPlaneManager::PortPresentLag() {
  const float t = CCameraManager::GetPresentationInterpolation();
  if (t < 0.f || t >= 1.f || sPortUpdateGeneration != CActor::PortTickGeneration() ||
      !PortOverlay::ParticleInterpolation())
    return 0.f;
  return (1.f - t) * sPortLastDt;
}
#endif

void CFluidPlaneManager::Update(float dt) {
#ifdef TARGET_PC
  sPortLastDt = dt;
  sPortUpdateGeneration = CActor::PortTickGeneration();
#endif
  mUvTime = dt + mUvTime;
#ifdef TARGET_PC
  PortWaterWaves::Age(sPortWaves, dt);
#endif
  for (CSplashRecord* it = mSplashes.begin(); it != mSplashes.end(); ++it) {
    it->SetTime(dt + it->GetTime());
    if (it->GetTime() > 9999.f) {
      it->SetTime(9999.f);
    }
  }
  mSplashCooldown = rstl::max_val(0.f, mSplashCooldown - dt);
}

void CFluidPlaneManager::StartFrame(bool enabled) const {
  mFrameActive = enabled;
  sProfile.Clear();
}

void CFluidPlaneManager::EndFrame() const { mFrameActive = false; }

float CFluidPlaneManager::GetLastSplashDeltaTime(TUniqueId splasher) const {
  float newestTime = 9999.f;
  for (const CSplashRecord* it = mSplashes.begin(); it != mSplashes.end(); ++it) {
    if (splasher == it->GetUniqueId() && newestTime > it->GetTime()) {
      newestTime = it->GetTime();
    }
  }
  return newestTime;
}

void CFluidPlaneManager::CreateSplash(TUniqueId splasher, CStateManager& mgr,
                                      const CScriptWater& water, const CVector3f& pos, float factor,
                                      bool sfx) {
  if (!water.CanRippleAtPoint(pos)) {
    return;
  }

  if (sfx) {
    CSfxManager::AddEmitter(water.GetSplashSound(factor), pos, water.GetCurrentAreaId().Value(),
                            true, false, CSfxManager::kMedPriority);
  }
  if (mSplashCooldown > 0.f && (mLastSplashPosition - pos).MagSquared() < 4.f) {
    return;
  }

  mSplashCooldown = 0.4f;
  mLastSplashPosition = pos;
#ifdef TARGET_PC
  // Port: a ring of waves from the splash (F1 > Video > Quality > Water waves), past the same
  // cooldown as its effect, so wading doesn't start one every frame.
  PortWaterWaves::Add(sPortWaves, pos.GetX(), pos.GetY(), pos.GetZ(), factor);
#endif
  float oldestTime = 0.f;
  CSplashRecord* oldestRecord = nullptr;
  for (CSplashRecord* it = mSplashes.begin(); it != mSplashes.end(); ++it) {
    if (it->GetTime() > oldestTime) {
      oldestRecord = it;
      oldestTime = it->GetTime();
    }
  }

  CSplashRecord newRecord(splasher);
  if (oldestRecord) {
    *oldestRecord = newRecord;
  } else {
    mSplashes.push_back(newRecord);
  }

  const float splashScale = water.GetSplashEffectScale(factor);
  if (water.GetSplashEffect(factor)) {
    CEntity* explosion = rs_new CExplosion(
        *water.GetSplashEffect(factor), mgr.AllocateUniqueId(),
        CEntityInfo(water.GetCurrentAreaId(), CEntity::NullConnectionList, true, kInvalidEditorId),
        rstl::string_l("Splash"), CTransform4f(CMatrix3f::Identity(), pos), 0,
        CVector3f(splashScale, splashScale, splashScale), water.GetSplashColor(), -1);
    if (explosion) {
      mgr.AddObject(*explosion);
    }
  }
}
