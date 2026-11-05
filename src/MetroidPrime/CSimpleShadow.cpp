#include "MetroidPrime/CSimpleShadow.hpp"

#include "Collision/CMaterialFilter.hpp"
#include "Collision/CRayCastResult.hpp"
#include "Kyoto/Basics/CCast.hpp"
#include "Kyoto/Graphics/CGraphics.hpp"
#include "Kyoto/Graphics/CTexture.hpp"
#include "Kyoto/Math/CAABox.hpp"
#include "Kyoto/Math/CMath.hpp"
#include "MetaRender/CCubeRenderer.hpp"
#include "MetroidPrime/CGameCollision.hpp"
#include "MetroidPrime/CStateManager.hpp"
#ifdef TARGET_PC
#include "MetroidPrime/CActor.hpp"
#include "MetroidPrime/CCameraManager.hpp"
#include "MetroidPrime/CModelData.hpp"
#include "port_actor_shadow_rules.h"
#include "port_overlay.h"

#include <dolphin/gx/GXExtra.h>
#endif

CSimpleShadow::CSimpleShadow(float scale, float userAlpha, float maxObjHeight, float displacement)
: mXf(CTransform4f::Identity())
, mScale(scale)
, mRadius(1.f)
, mUserAlpha(userAlpha)
, mHeightAlpha(1.f)
, mMaxObjHeight(maxObjHeight)
, mDisplacement(displacement)
, mCollision(false)
, mAlwaysCalculateRadius(true)
, mRadiusCalculated(false) {}

void CSimpleShadow::Calculate(const CAABox& bounds, const CTransform4f& xf,
                              const CStateManager& mgr) {
  mCollision = false;
  const CVector3f extent = bounds.GetMaxPoint() - bounds.GetMinPoint();
  const float halfHeight = extent.GetZ() / 2.f;
  const CVector3f position = xf.GetTranslation() + CVector3f(0.f, 0.f, halfHeight);
  const CVector3f direction(0.f, 0.f, -1.f);
  CRayCastResult result =
      mgr.RayStaticIntersection(position, direction, mMaxObjHeight,
                                CMaterialFilter::MakeExclude(CMaterialList(kMT_SeeThrough)));
  float height = mMaxObjHeight;
  if (result.IsValid()) {
    mCollision = true;
    height = result.GetTime();
  }

  CRayCastResult closestResult = result;
  if (height > 0.1f + halfHeight) {
    rstl::reserved_vector< TUniqueId, kMaxObjects > nearList;
    mgr.BuildNearList(nearList, position, direction, mMaxObjHeight,
                      CMaterialFilter::MakeInclude(CMaterialList(kMT_Platform)), nullptr);
    TUniqueId id = kInvalidUniqueId;
    CRayCastResult dynamicResult =
        CGameCollision::RayDynamicIntersection(mgr, id, position, direction, mMaxObjHeight,
                                               CMaterialFilter::GetPassEverything(), nearList);
    if (dynamicResult.IsValid() && dynamicResult.GetTime() < height) {
      closestResult = dynamicResult;
      mCollision = true;
      height = dynamicResult.GetTime();
    }
  }

  if (mCollision) {
    mHeightAlpha = 1.f - height / mMaxObjHeight;
    const CVector3f normal = closestResult.GetPlane().GetNormal();
    mXf = CTransform4f::LookAt(normal, CVector3f::Zero());
    mXf.SetTranslation(closestResult.GetPoint() + mDisplacement * normal);
    if (mAlwaysCalculateRadius || !mRadiusCalculated) {
      mRadius = sqrtf(extent.GetX() * extent.GetX() + extent.GetY() * extent.GetY()) / 2.f;
      mRadiusCalculated = true;
    }
  }
}

void CSimpleShadow::Render(const CTexture* tex) const {
  if (!mCollision)
    return;

  CGraphics::DisableAllLights();
#ifdef TARGET_PC
  gpRender->SetModelMatrix(PortPresentedXf());
#else
  gpRender->SetModelMatrix(mXf);
#endif
  tex->Load(GX_TEXMAP0, CTexture::kCM_Repeat);
  CGraphics::SetTevOp(kTS_Stage0, CGraphics::kEnvModulate);
  CGraphics::SetTevOp(kTS_Stage1, CGraphics::kEnvPassthru);
  CGraphics::SetAlphaCompare(kAF_Always, 0, kAO_And, kAF_Always, 0);
  CGraphics::SetDepthWriteMode(true, kE_LEqual, false);
  CGraphics::SetBlendMode(kBM_Blend, kBF_SrcAlpha, kBF_InvSrcAlpha, kLO_Clear);
  const float radius = mRadius * mScale;
#ifdef TARGET_PC
  PortStreamQuad(CTransform4f::Identity(), radius,
                 0xffffff00 + CCast::ToUint8(255.f * (mHeightAlpha * mUserAlpha)));
#else
  CGraphics::StreamBegin(kP_Quads);
  CGraphics::StreamColor(0xffffff00 + CCast::ToUint8(255.f * (mHeightAlpha * mUserAlpha)));
  CGraphics::StreamTexcoord(0.f, 0.f);
  CGraphics::StreamVertex(CVector3f(-radius, 0.f, -radius));
  CGraphics::StreamTexcoord(0.f, 1.f);
  CGraphics::StreamVertex(CVector3f(radius, 0.f, -radius));
  CGraphics::StreamTexcoord(1.f, 1.f);
  CGraphics::StreamVertex(CVector3f(radius, 0.f, radius));
  CGraphics::StreamTexcoord(1.f, 0.f);
  CGraphics::StreamVertex(CVector3f(-radius, 0.f, radius));
  CGraphics::StreamEnd();
#endif
}

#ifdef TARGET_PC
CTransform4f CSimpleShadow::PortPresentedXf() const {
  // Calculate places the blob once per tick from the sim transform; between ticks draw it at
  // the blend, as its actor is.
  CTransform4f xf = mXf;
  const float t = CCameraManager::GetPresentationInterpolation();
  if (mPortXf.Note(mXf, CActor::PortTickGeneration()) && t >= 0.f && t < 1.f) {
    CTransform4f blend = CTransform4f::Identity();
    CTransform4f cur = CTransform4f::Identity();
    if (CActor::PortBlendRigid(mPortXf.prev, mXf, t, blend, cur))
      xf = blend * cur.GetQuickInverse() * mXf;
  }
  return xf;
}

void CSimpleShadow::PortStreamQuad(const CTransform4f& xf, float radius, uint color) {
  // The quad lies in the model's x/z plane: Calculate's LookAt points y along the floor's normal.
  CGraphics::StreamBegin(kP_Quads);
  CGraphics::StreamColor(color);
  CGraphics::StreamTexcoord(0.f, 0.f);
  CGraphics::StreamVertex(xf * CVector3f(-radius, 0.f, -radius));
  CGraphics::StreamTexcoord(0.f, 1.f);
  CGraphics::StreamVertex(xf * CVector3f(radius, 0.f, -radius));
  CGraphics::StreamTexcoord(1.f, 1.f);
  CGraphics::StreamVertex(xf * CVector3f(radius, 0.f, radius));
  CGraphics::StreamTexcoord(1.f, 0.f);
  CGraphics::StreamVertex(xf * CVector3f(-radius, 0.f, radius));
  CGraphics::StreamEnd();
}

bool CSimpleShadow::PortRenderSilhouette(const CStateManager& mgr) const {
  if (!PortOverlay::ActorShadows()) {
    return false;
  }
  if (!mCollision) {
    return true; // as Render: no floor, no shadow
  }
  const CActor* owner = mPortOwner;
  mPortOwner = nullptr; // queued again by the next frame's AddToRenderer
  if (owner == nullptr || !owner->GetModelData() || owner->NullModel() || !owner->PortRenderEnabled()) {
    return false;
  }
  // A layer of Aurora's 16 maps each; at most that many shadows a frame, the rest keep the blob.
  static int sFrame = -1;
  static int sUsed = 0;
  if (CGraphics::GetFrameCounter() != sFrame) {
    sFrame = CGraphics::GetFrameCounter();
    sUsed = 0;
  }
  if (sUsed >= PortActorShadow::kLayers) {
    return false;
  }
  const CModelFlags& ownerFlags = owner->GetModelFlags();
  // A fading actor's shadow fades with it.
  float fade = 1.f;
  if (ownerFlags.GetTrans() >= CModelFlags::kT_Blend) {
    fade = ownerFlags.GetColorRef().GetAlpha();
  }
  const float opacity = mUserAlpha * fade * float(PortOverlay::ActorShadowStrength()) / 100.f;
  if (!(opacity > 0.f)) {
    return true;
  }

  // The map's box: over the model's bounds (a quarter wider, as the drawn actor may sit between
  // ticks), from above its top to below the floor the blob found.
  const CAABox bounds = owner->GetModelData()->GetBounds(owner->GetTransform());
  const CVector3f& lo = bounds.GetMinPoint();
  const CVector3f& hi = bounds.GetMaxPoint();
  const CTransform4f floorXf = PortPresentedXf();
  const CVector3f floor = floorXf.GetTranslation();
  PortActorShadow::Box box;
  box.cx = 0.5f * (lo.GetX() + hi.GetX());
  box.cy = 0.5f * (lo.GetY() + hi.GetY());
  box.radius = 1.25f * 0.5f * rstl::max_val(hi.GetX() - lo.GetX(), hi.GetY() - lo.GetY()) + 0.1f;
  box.top = hi.GetZ() + 0.5f;
  box.bottom = floor.GetZ() - 1.f;
  box.base = lo.GetZ();
  if (!(box.top - box.bottom > 0.1f)) {
    return false;
  }

  // GX's view space (CGraphics' view: x right, y forward, z up, as columns).
  const CTransform4f& v = CGraphics::GetViewMatrix();
  const PortActorShadow::ViewFrame view{
      {v.Get00(), v.Get10(), v.Get20()},
      {v.Get02(), v.Get12(), v.Get22()},
      {-v.Get01(), -v.Get11(), -v.Get21()},
      {v.GetTranslation().GetX(), v.GetTranslation().GetY(), v.GetTranslation().GetZ()},
  };
  const int layer = sUsed;
  float params[64];
  PortActorShadow::Params(view, box, opacity, mMaxObjHeight, layer, params);

  // The casters: the owner's models, opaque and depth only, into the layer's map.
  GXPortActorShadowCasters(static_cast< u32 >(layer), params);
  {
    CPortActorRenderScope presented(*owner);
    const CModelFlags casterFlags(CModelFlags::kT_Opaque, static_cast< uchar >(ownerFlags.GetShaderSet()),
                                  static_cast< CModelFlags::EFlags >(CModelFlags::kF_DepthCompare |
                                                                     CModelFlags::kF_DepthUpdate),
                                  CColor::White());
    // The whole model: a static model whose unsorted parts were drawn already would otherwise
    // draw its sorted ones only (CModelData::RenderUnsortedParts).
    const CModelData& model = *owner->GetModelData();
    const bool sorted = model.PortRenderSorted();
    model.PortSetRenderSorted(false);
    model.Render(CModelData::GetRenderingModel(mgr), owner->GetTransform(), owner->GetActorLights(),
                 casterFlags);
    model.PortSetRenderSorted(sorted);
  }
  GXPortActorShadowEnd();
  if (!GXPortActorShadowRender()) {
    return false;
  }
  ++sUsed;

  // The receiver: the blob's quad, wide enough for the whole map, as a multiply decal.
  const float dx = box.cx - floor.GetX();
  const float dy = box.cy - floor.GetY();
  const float radius = rstl::max_val(mRadius * mScale, box.radius + CMath::SqrtF(dx * dx + dy * dy));
  CGraphics::DisableAllLights();
  gpRender->SetModelMatrix(floorXf);
  CGraphics::SetTevOp(kTS_Stage0, CGraphics::kEnvPassthru);
  CGraphics::SetTevOp(kTS_Stage1, CGraphics::kEnvPassthru);
  CGraphics::SetAlphaCompare(kAF_Always, 0, kAO_And, kAF_Always, 0);
  CGraphics::SetDepthWriteMode(true, kE_LEqual, false);
  CGraphics::SetBlendMode(kBM_Blend, kBF_Zero, kBF_SrcColor, kLO_Clear);
  GXPortActorShadowDecal(static_cast< u32 >(layer), params);
  PortStreamQuad(CTransform4f::Identity(), radius, 0xffffffff);
  GXPortActorShadowEnd();
  return true;
}
#endif

const CTransform4f& CSimpleShadow::GetTransform() const { return mXf; }

void CSimpleShadow::SetUserAlpha(float alpha) { mUserAlpha = alpha; }

float CSimpleShadow::GetMaxObjectHeight() const { return mMaxObjHeight; }

void CSimpleShadow::SetAlwaysCalculateRadius(bool value) { mAlwaysCalculateRadius = value; }

CAABox CSimpleShadow::GetBounds() const {
  const CVector3f& translation = mXf.GetTranslation();
  const float extent = mRadius * mScale;
  return CAABox(translation - CVector3f(extent, extent, extent),
                translation + CVector3f(extent, extent, extent));
}

CAABox CSimpleShadow::GetMaxShadowBox(const CAABox& bounds) const {
  const float extent = mRadius * mScale;
  const CVector3f center = bounds.GetCenterPoint();
  CAABox expanded = bounds;
  expanded.AccumulateBounds(center + CVector3f(extent, extent, -GetMaxObjectHeight()));
  expanded.AccumulateBounds(center + CVector3f(-extent, -extent, -GetMaxObjectHeight()));
  return expanded;
}

bool CSimpleShadow::Valid() const { return mCollision; }
