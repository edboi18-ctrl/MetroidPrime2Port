#ifndef _CCUBEMATERIAL
#define _CCUBEMATERIAL

#include "types.h"

#include "Kyoto/Basics/CBasics.hpp"
#include "Kyoto/Graphics/CModelFlags.hpp"
#include "Kyoto/Math/CVector3f.hpp"

#include <dolphin/gx.h>

enum EStateFlags {
  kStateFlag_KonstValues = (1 << 3),
  kStateFlag_DepthSorting = (1 << 4),
  kStateFlag_AlphaTest = (1 << 5),
  kStateFlag_Reflection = (1 << 6),
  kStateFlag_DepthWrite = (1 << 7),
  kStateFlag_ReflectionSurfaceEye = (1 << 8),
  kStateFlag_ShadowOccluderMesh = (1 << 9),
  kStateFlag_ReflectionIndirectTexture = (1 << 10),
  kStateFlag_Lightmap = (1 << 11),
  kStateFlag_LightmapUvArray = (1 << 13),
  kStateFlag_TextureSlotMask = static_cast< uint >(~kStateFlag_LightmapUvArray),
};

class CCubeSurface;
class CCubeModel;

class CCubeMaterial {
public:
  explicit CCubeMaterial(const void* data) : mData(data) {}
  static void ResetCachedMaterials();
  static void EnsureViewDepStateCached(const CCubeSurface* surface);
  static void EnsureTevsDirect();
  static void KillCachedViewDepState();

  static void UseThermalTevs();
  static void UseNormalTevs();
  static void ResetTransparencyKColor();
  static int GetTransparencyKColor();
  static void ResetExtraTexCoord();
  static uint GetExtraTexCoord();
  static uint GetExtraPostTexMtx();

  const uchar* GetData() const { return static_cast< const uchar* >(mData); }
  uint GetFlags() const { return CBasics::SwapBytes(*reinterpret_cast< const uint* >(GetData())); }
  bool IsFlagSet(const EStateFlags flag) const { return (GetFlags() & flag) != 0; }
  uint GetTextureCount() const { return CBasics::SwapBytes(static_cast< const uint* >(mData)[1]); }

  uint GetVertexDesc() const {
    const uchar* data = GetData();
    data += GetTextureCount() * sizeof(uint);
    data += sizeof(uint) + sizeof(uint);
    return CBasics::SwapBytes(*reinterpret_cast< const uint* >(data));
  }

  // Guessed name. Echoes material masks follow the compressed vertex descriptor.
  u64 GetMaterialMask() const {
#ifdef TARGET_PC
    // Port: the mask is a big-endian u64 like the rest of the material data.
    return __builtin_bswap64(
        *reinterpret_cast< const u64* >(static_cast< const uint* >(mData) + GetTextureCount() + 3));
#else
    return *reinterpret_cast< const u64* >(static_cast< const uint* >(mData) + GetTextureCount() + 3);
#endif
  }

  void SetCurrent(const CModelFlags& flags, const CCubeSurface& surface,
                  const CCubeModel& model) const;
  void SetCurrentBlack() const;
  uint GetCompressedBlend() const;

  static const CVector3f& GetViewingReflection() { return sViewingFrom; }
#ifdef TARGET_PC
  // Port: around a surface's display list, GXPortSetSurfacePBR with the PBR maps SetCurrent chose
  // for the material it set last (port_pbr_maps.h), GXPortSetEnvProbe while sPortEnvProbe is set,
  // and for either the live probe's matrix (GXSetPBRProbe). Begin returns what End undoes.
  static uint PortBeginSurfaceDraw();
  static void PortEndSurfaceDraw(uint begun);
  // The material editor's override for material `materialIndex` of `model` (port_material_overrides.h),
  // which the next PortBeginSurfaceDraw applies; none when no materials.json is loaded.
  static void PortSetMaterialOverride(const CCubeModel& model, int materialIndex);
  // GXSetPBRProbe for the current view (the live probe's matrix and weight), and counts a draw
  // that reads it (sPortProbeDraws). PortBeginSurfaceDraw's, and the water's (CFluidPlaneCPU).
  static void PortLoadProbe();
  // CPlayerGun::Render sets it: the first-person gun's environment maps read the live probe.
  static bool sPortEnvProbe;
  // The look sPortEnvProbe's draws read the probe with (GXPortSetEnvProbeLook): the probe level and
  // the strength; a strength below 0 is the frame's look (the cannon's, aurora_set_env_probe_look).
  static float sPortEnvProbeLevel;
  static float sPortEnvProbeStrength;
  // Where the next capture centres the probe (CStateManager::PortCaptureProbeFace) when set: the
  // model that last read it, if it isn't the first-person view's. Unset, the probe is the camera's.
  static bool sPortProbeFocusSet;
  static CVector3f sPortProbeFocus;
  // How much of the PBR maps' lighting the surfaces drawn now take, 0..1 (GXPortSetSurfacePBRStrength);
  // 1 outside a CPortPbrStrengthScope.
  static float sPortPbrStrength;
  // The face reflection in Samus's visor (CSamusFaceReflection::Draw): lit by a flash right next to it,
  // full PBR (the GGX highlight, the normal map, the probe) made a glossy, plastic face. Its draws take
  // `strength` of it, the rest the game's own lighting.
  class CPortPbrStrengthScope {
  public:
    explicit CPortPbrStrengthScope(float strength);
    ~CPortPbrStrengthScope();

  private:
    CPortPbrStrengthScope(const CPortPbrStrengthScope&);
    CPortPbrStrengthScope& operator=(const CPortPbrStrengthScope&);
    float mSaved;
  };
  // Samus's body, her morph ball and the cutscene Samus (CPlayer::Render, CScriptPlayerActor::Render):
  // their environment maps read the live probe with their own look while it lives, and, if any of
  // their draws did, the next probe is centred on `focus`, so it holds the room around them rather
  // than around a third-person camera. `on` false leaves everything as it was.
  class CPortEnvProbeScope {
  public:
    CPortEnvProbeScope(bool on, float level, float strength, const CVector3f& focus);
    ~CPortEnvProbeScope();

  private:
    CPortEnvProbeScope(const CPortEnvProbeScope&);
    CPortEnvProbeScope& operator=(const CPortEnvProbeScope&);
    bool mOn;
    bool mSavedProbe;
    float mSavedLevel;
    float mSavedStrength;
    uint mDraws;
    CVector3f mFocus;
  };
  // CStateManager::PortCaptureProbeFace is drawing one of the probe's faces.
  static bool sPortCapturingProbe;
  // Draws that read the probe since the start (captures not counted): the capture runs only
  // while something reads it.
  static uint sPortProbeDraws;
  // 1 once all six faces have been drawn, else 0 (draws keep their own look until then).
  static float sPortProbeWeight;
#endif

private:
  static void SetupBlendMode(uint blendFactors, const CModelFlags& flags, bool alphaTest);
  static uint HandleReflection(GXTexMapID indTexSlot, int indMtxScaleExp, uint tevCount,
                               uint texCount, uint tcgCount, uint kColorCount);

  static const CCubeModel* sLastModelCached;
  static const CCubeModel* sRenderingModel;
  static CVector3f sViewingFrom;

  const void* mData;
};

#endif // _CCUBEMATERIAL
