#ifndef _CSIMPLESHADOW
#define _CSIMPLESHADOW

#include "types.h"

#include "Kyoto/Math/CTransform4f.hpp"
#include "Kyoto/TToken.hpp"

#ifdef TARGET_PC
#include "port_tick_pair.h"
#endif

class CAABox;
class CActor;
class CStateManager;
class CTexture;

class CSimpleShadow {
public:
  CSimpleShadow(float scale, float userAlpha, float maxObjHeight, float displacement);

  void Calculate(const CAABox& bounds, const CTransform4f& xf, const CStateManager& mgr);
  void Render(const CTexture* tex) const;
  const CTransform4f& GetTransform() const;
  void SetUserAlpha(float);
  float GetMaxObjectHeight() const;

  void SetAlwaysCalculateRadius(bool);
  CAABox GetBounds() const;
  CAABox GetMaxShadowBox(const CAABox& bounds) const;
  bool Valid() const;
#ifdef TARGET_PC
  // Port: the actor that queued this shadow this frame (CActor::AddToRenderer), whose models the
  // silhouette shadow draws.
  void PortSetOwner(const CActor* owner) const { mPortOwner = owner; }
  // Port: Video > Quality > Actor shadows (port_actor_shadow_rules.h): the owner's silhouette in the
  // blob's place. True when it drew it, or when there is nothing to draw; false to draw the blob.
  bool PortRenderSilhouette(const CStateManager& mgr) const;
#endif

private:
#ifdef TARGET_PC
  // mXf, or its blend between the last two ticks while the presentation interpolates.
  CTransform4f PortPresentedXf() const;
  // The blob's quad at `xf`, `radius` to a side, in whatever blend and TEV state is set.
  static void PortStreamQuad(const CTransform4f& xf, float radius, uint color);
#endif
  CTransform4f mXf;
  float mScale;
  float mRadius;
  float mUserAlpha;
  float mHeightAlpha;
  float mMaxObjHeight;
  float mDisplacement;
  bool mCollision : 1;
  bool mAlwaysCalculateRadius : 1;
  bool mRadiusCalculated : 1;
#ifdef TARGET_PC
  // Render() snapshots mXf per tick to draw the blob at the blend between ticks.
  mutable PortTickPair< CTransform4f > mPortXf{CTransform4f::Identity()};
  mutable const CActor* mPortOwner = nullptr;
#endif
};

CHECK_SIZEOF(CSimpleShadow, 0x4c)

#endif // _CSIMPLESHADOW
