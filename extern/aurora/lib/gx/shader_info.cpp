#include "shader_info.hpp"

#include "../gfx/recording.hpp"

#include <cassert>
#include <cmath>

#include <tracy/Tracy.hpp>

namespace aurora::gx {
namespace {
constexpr Module Log{"aurora::gx"};

bool is_alpha_bump_channel(GXChannelID id) { return id == GX_ALPHA_BUMP || id == GX_ALPHA_BUMPN; }

Vec4<float> texture_size_bias(const gfx::TextureBind& tex) {
  auto width = static_cast<float>(tex.texObj.width());
  auto height = static_cast<float>(tex.texObj.height());
  // No viewport-scale bias, which Aurora adds for arbitrary-mip textures to keep the
  // console's mip choice: on the game's own textures it sampled the wrong mip at
  // non-integer EFB scales, and on a replacement it reads the console's mip of a texture
  // up to 32 times larger (a 1024 map over a 32 stub: 4 mips too fine), which shimmers
  // and cost the phone a third of its frame rate. The GPU's own LOD fits both.
  return {width, height, tex.texObj.lod_bias(), 0.0f};
}

void color_arg_reg_info(GXTevColorArg arg, const TevStage& stage, ShaderInfo& info) {
  switch (arg) {
  case GX_CC_CPREV:
  case GX_CC_APREV:
    if (!info.writesTevReg.test(GX_TEVPREV)) {
      info.loadsTevReg.set(GX_TEVPREV);
    }
    break;
  case GX_CC_C0:
  case GX_CC_A0:
    if (!info.writesTevReg.test(GX_TEVREG0)) {
      info.loadsTevReg.set(GX_TEVREG0);
    }
    break;
  case GX_CC_C1:
  case GX_CC_A1:
    if (!info.writesTevReg.test(GX_TEVREG1)) {
      info.loadsTevReg.set(GX_TEVREG1);
    }
    break;
  case GX_CC_C2:
  case GX_CC_A2:
    if (!info.writesTevReg.test(GX_TEVREG2)) {
      info.loadsTevReg.set(GX_TEVREG2);
    }
    break;
  case GX_CC_TEXC:
  case GX_CC_TEXA:
    if (stage.texMapId != GX_TEXMAP_NULL) {
      CHECK(stage.texCoordId != GX_TEXCOORD_NULL, "tex coord not bound");
      info.sampledTexCoords.set(stage.texCoordId);
      info.sampledTextures.set(stage.texMapId);
    }
    break;
  case GX_CC_RASC:
  case GX_CC_RASA:
    if (stage.channelId != GX_COLOR_NULL && stage.channelId != GX_COLOR_ZERO &&
        !is_alpha_bump_channel(stage.channelId)) {
      info.sampledColorChannels.set(color_channel(stage.channelId));
    }
    break;
  case GX_CC_KONST:
    info.usesTevKonst = true;
    break;
  default:
    break;
  }
}

void alpha_arg_reg_info(GXTevAlphaArg arg, const TevStage& stage, ShaderInfo& info) {
  switch (arg) {
  case GX_CA_APREV:
    if (!info.writesTevReg.test(GX_TEVPREV)) {
      info.loadsTevReg.set(GX_TEVPREV);
    }
    break;
  case GX_CA_A0:
    if (!info.writesTevReg.test(GX_TEVREG0)) {
      info.loadsTevReg.set(GX_TEVREG0);
    }
    break;
  case GX_CA_A1:
    if (!info.writesTevReg.test(GX_TEVREG1)) {
      info.loadsTevReg.set(GX_TEVREG1);
    }
    break;
  case GX_CA_A2:
    if (!info.writesTevReg.test(GX_TEVREG2)) {
      info.loadsTevReg.set(GX_TEVREG2);
    }
    break;
  case GX_CA_TEXA:
    if (stage.texMapId != GX_TEXMAP_NULL) {
      CHECK(stage.texCoordId != GX_TEXCOORD_NULL, "tex coord not bound");
      info.sampledTexCoords.set(stage.texCoordId);
      info.sampledTextures.set(stage.texMapId);
    }
    break;
  case GX_CA_RASA:
    if (stage.channelId != GX_COLOR_NULL && stage.channelId != GX_COLOR_ZERO &&
        !is_alpha_bump_channel(stage.channelId)) {
      info.sampledColorChannels.set(color_channel(stage.channelId));
    }
    break;
  case GX_CA_KONST:
    info.usesTevKonst = true;
    break;
  default:
    break;
  }
}

f32 tex_offset(GXTexOffset offs) noexcept {
  switch (offs) {
  default:
  case GX_TO_ZERO:
    return 0.f;
  case GX_TO_SIXTEENTH:
    return 1.f / 16.f;
  case GX_TO_EIGHTH:
    return 1.f / 8.f;
  case GX_TO_FOURTH:
    return 1.f / 4.f;
  case GX_TO_HALF:
    return 1.f / 2.f;
  case GX_TO_ONE:
    return 1.f;
  }
}

u32 point_texcoord_mask() noexcept {
  u32 mask = 0;
  for (int i = 0; i < MaxTexCoord; ++i) {
    if (g_gxState.texCoordScales[i].pointOffset) {
      mask |= 1 << i;
    }
  }
  return mask;
}

u32 line_texcoord_mask() noexcept {
  u32 mask = 0;
  for (int i = 0; i < MaxTexCoord; ++i) {
    if (g_gxState.texCoordScales[i].lineOffset) {
      mask |= 1 << i;
    }
  }
  return mask;
}
} // namespace

int port_surface_pbr_stage(const ShaderConfig& config) noexcept {
  const u32 kind = config.pbrKind & kPortSurfacePbrStageMask;
  if (config.pbr != 0 || kind == 0 || (config.pbrKind & kPortWaterBit) != 0 || config.perPixelLight == 0 ||
      !config.colorChannels[GX_COLOR0].lightingEnabled) {
    return -1;
  }
  const u32 idx = kind - 1u;
  if (idx >= config.tevStageCount) {
    return -1;
  }
  // Maps 6 and 7 are the PBR maps': the material must not use them itself.
  for (u32 i = 0; i < config.tevStageCount; ++i) {
    const auto map = config.tevStages[i].texMapId;
    if (map == GX_TEXMAP6 || map == GX_TEXMAP7) {
      return -1;
    }
  }
  for (u32 i = 0; i < config.numIndStages; ++i) {
    const auto map = config.indStages[i].texMapId;
    if (map == GX_TEXMAP6 || map == GX_TEXMAP7) {
      return -1;
    }
  }
  // RASC as rast0.rgb: channel 0 with the raster swap left as is (the relit stage subtracts rast0.rgb).
  const auto is_channel0 = [&config](const TevStage& s) {
    return (s.channelId == GX_COLOR0 || s.channelId == GX_COLOR0A0) && !config.tevSwapTable[s.tevSwapRas];
  };
  const auto plain_add = [](const TevOp& op) {
    return op.op == GX_TEV_ADD && op.bias == GX_TB_ZERO && op.scale == GX_CS_SCALE_1;
  };
  // The stage: 0 + lerp(0, L, TEXC) (or lerp(0, TEXC, L)), plain texturing (no indirect).
  const auto& stage = config.tevStages[idx];
  if (stage.texMapId == GX_TEXMAP_NULL || stage.texCoordId == GX_TEXCOORD_NULL ||
      stage.indTexMtxId != GX_ITM_OFF || stage.indTexWrapS != GX_ITW_OFF || stage.indTexWrapT != GX_ITW_OFF ||
      stage.indTexAddPrev || !plain_add(stage.colorOp) || stage.colorPass.a != GX_CC_ZERO ||
      stage.colorPass.d != GX_CC_ZERO) {
    return -1;
  }
  GXTevColorArg light;
  if (stage.colorPass.c == GX_CC_TEXC) {
    light = stage.colorPass.b;
  } else if (stage.colorPass.b == GX_CC_TEXC) {
    light = stage.colorPass.c;
  } else {
    return -1;
  }
  // L: channel 0's lit colour, or a colour register an earlier stage set to it plus something (a
  // room's lightmap stage: C0 = RASC + C1 x lightmap), so that L - rast0 is what isn't lit.
  if (light == GX_CC_RASC) {
    return is_channel0(stage) ? static_cast<int>(idx) : -1;
  }
  GXTevRegID reg;
  switch (light) {
  case GX_CC_C0:
    reg = GX_TEVREG0;
    break;
  case GX_CC_C1:
    reg = GX_TEVREG1;
    break;
  case GX_CC_C2:
    reg = GX_TEVREG2;
    break;
  default:
    return -1;
  }
  if (stage.colorOp.outReg == reg) {
    return -1; // the relit stage reads L after the stage has written it
  }
  for (u32 j = idx; j-- > 0;) {
    const auto& writer = config.tevStages[j];
    if (writer.colorOp.outReg != reg) {
      continue;
    }
    return plain_add(writer.colorOp) && writer.colorPass.d == GX_CC_RASC && is_channel0(writer)
               ? static_cast<int>(idx)
               : -1;
  }
  return -1;
}

// Whether the draw's shader has the per-pixel view-space position and normal (in.mv_pos, pp_nrm), which
// the probe's reflections read: build_shader_info's lightingEnabled (with perPixelLight), which needs a
// colour channel the TEV reads (sampledColorChannels) with its lighting on. A channel 0 lit but never
// read (a pass that draws the model in one colour: a silhouette, a visor's) gives the shader no such
// inputs, and the probe's code there was a WGSL error, which the device makes fatal. A stage reading a
// lit channel's RASC or RASA counts here; the rarer ways (a texgen from a colour, emboss) don't, so such
// a draw is refused, never broken.
static bool port_has_lit_varyings(const ShaderConfig& config) noexcept {
  for (u32 i = 0; i < config.tevStageCount && i < MaxTevStages; ++i) {
    const auto& stage = config.tevStages[i];
    if (stage.channelId == GX_COLOR_NULL || stage.channelId == GX_COLOR_ZERO || is_alpha_bump_channel(stage.channelId)) {
      continue;
    }
    const auto& c = stage.colorPass;
    const auto& a = stage.alphaPass;
    const auto ras = [](GXTevColorArg arg) { return arg == GX_CC_RASC || arg == GX_CC_RASA; };
    if (!ras(c.a) && !ras(c.b) && !ras(c.c) && !ras(c.d) && a.a != GX_CA_RASA && a.b != GX_CA_RASA &&
        a.c != GX_CA_RASA && a.d != GX_CA_RASA) {
      continue;
    }
    const u8 channel = color_channel(stage.channelId);
    if (channel < 2 && (config.colorChannels[channel].lightingEnabled ||
                        config.colorChannels[channel + GX_ALPHA0].lightingEnabled)) {
      return true;
    }
  }
  return false;
}

u32 port_env_probe_stages(const ShaderConfig& config) noexcept {
  if (config.pbr != 0 || (config.pbrKind & kPortEnvProbeBit) == 0 || (config.pbrKind & kPortWaterBit) != 0 ||
      config.perPixelLight == 0 ||
      !config.colorChannels[GX_COLOR0].lightingEnabled || !port_has_lit_varyings(config)) {
    return 0;
  }
  u32 stages = 0;
  for (u32 i = 0; i < config.tevStageCount; ++i) {
    const auto& stage = config.tevStages[i];
    if (stage.texMapId == GX_TEXMAP_NULL || stage.texCoordId == GX_TEXCOORD_NULL ||
        stage.texCoordId >= MaxTexCoord) {
      continue;
    }
    // Only a stage that reads its texture has a sample to replace (as the generator's
    // uses_texture_sample decides).
    const auto& c = stage.colorPass;
    const auto& a = stage.alphaPass;
    const auto tex = [](GXTevColorArg arg) { return arg == GX_CC_TEXC || arg == GX_CC_TEXA; };
    const bool sampled = tex(c.a) || tex(c.b) || tex(c.c) || tex(c.d) || a.a == GX_CA_TEXA || a.b == GX_CA_TEXA ||
                         a.c == GX_CA_TEXA || a.d == GX_CA_TEXA;
    const auto& tcg = config.tcgs[stage.texCoordId];
    if (sampled && tcg.src == GX_TG_NRM && (tcg.type == GX_TG_MTX2x4 || tcg.type == GX_TG_MTX3x4)) {
      stages |= 1u << i;
    }
  }
  return stages;
}

u32 port_emissive_stages(const ShaderConfig& config) noexcept {
  if (config.pbr != 0 || config.lineMode != 0 || (config.pbrKind & kPortWaterBit) != 0) {
    return 0;
  }
  const auto lit = [](const auto& c) {
    return c.a == GX_CC_RASC || c.b == GX_CC_RASC || c.c == GX_CC_RASC || c.a == GX_CC_RASA || c.b == GX_CC_RASA ||
           c.c == GX_CC_RASA;
  };
  const auto readsPrev = [](const auto& c) {
    return c.a == GX_CC_CPREV || c.b == GX_CC_CPREV || c.c == GX_CC_CPREV;
  };
  u32 stages = 0;
  for (u32 i = 1; i < config.tevStageCount && i < MaxTevStages; ++i) {
    const auto& stage = config.tevStages[i];
    if (stage.texMapId == GX_TEXMAP_NULL || stage.texCoordId == GX_TEXCOORD_NULL || stage.texCoordId >= MaxTexCoord) {
      continue;
    }
    const auto src = config.tcgs[stage.texCoordId].src;
    if (src < GX_TG_TEX0 || src > GX_TG_TEX7) {
      continue;
    }
    if (stage.colorOp.op != GX_TEV_ADD || stage.colorOp.outReg != GX_TEVPREV) {
      continue;
    }
    const auto& c = stage.colorPass;
    if (lit(c)) {
      continue;
    }
    bool glow = false;
    if (c.d == GX_CC_CPREV) {
      glow = c.c == GX_CC_TEXC ? c.a == GX_CC_ZERO : (c.a == GX_CC_TEXC || c.b == GX_CC_TEXC);
    } else if (c.d == GX_CC_TEXC) {
      glow = c.a == GX_CC_CPREV || c.b == GX_CC_CPREV;
    }
    if (!glow) {
      continue;
    }
    bool litLater = false;
    for (u32 j = i + 1; j < config.tevStageCount && j < MaxTevStages; ++j) {
      const auto& later = config.tevStages[j].colorPass;
      if (readsPrev(later) && lit(later)) {
        litLater = true;
        break;
      }
    }
    if (!litLater) {
      stages |= 1u << i;
    }
  }
  return stages;
}

int port_water_stage(const ShaderConfig& config) noexcept {
  if (config.pbr != 0 || (config.pbrKind & kPortWaterBit) == 0 || config.lineMode != 0) {
    return -1;
  }
  const u32 kind = config.pbrKind & kPortSurfacePbrStageMask;
  if (kind == 0 || kind - 1u >= config.tevStageCount) {
    return -1;
  }
  // The stage must read its texture's colour: that sample is what the reflection replaces.
  const auto& stage = config.tevStages[kind - 1u];
  if (stage.texMapId == GX_TEXMAP_NULL || stage.texCoordId == GX_TEXCOORD_NULL || stage.texCoordId >= MaxTexCoord) {
    return -1;
  }
  const auto& c = stage.colorPass;
  const auto tex = [](GXTevColorArg arg) { return arg == GX_CC_TEXC || arg == GX_CC_TEXA; };
  if (!(tex(c.a) || tex(c.b) || tex(c.c) || tex(c.d))) {
    return -1;
  }
  return static_cast<int>(kind - 1u);
}

bool port_water_ripples(const ShaderConfig& config) noexcept {
  if ((config.pbrKind & kPortWaterRippleBit) == 0 || port_water_stage(config) < 0) {
    return false;
  }
  const auto layer = [](const TcgConfig& tcg) { return tcg.type == GX_TG_MTX2x4 && tcg.src == GX_TG_POS; };
  return layer(config.tcgs[GX_TEXCOORD0]) && layer(config.tcgs[GX_TEXCOORD1]);
}

void apply_port_surface_flags(ShaderConfig& config, u8 surfacePbr, bool envProbe, u8 waterReflect,
                              bool waterRipples) noexcept {
  if (config.pbr != 0) {
    return;
  }
  if (waterReflect != 0) {
    // The water: no PBR maps or env probe stages, and no per-pixel lights of its own.
    config.pbrKind = static_cast<u8>(kPortWaterBit | (waterReflect & kPortSurfacePbrStageMask));
    if (port_water_stage(config) < 0) {
      config.pbrKind = 0;
    } else if (waterRipples) {
      config.pbrKind |= kPortWaterRippleBit;
      if (!port_water_ripples(config)) {
        config.pbrKind &= static_cast<u8>(~kPortWaterRippleBit);
      }
    }
    return;
  }
  if (surfacePbr == 0 && !envProbe) {
    return;
  }
  // Both predicates need per-pixel lights; wants_per_pixel_light keeps them on for the result.
  const u8 oldPerPixel = config.perPixelLight;
  config.perPixelLight = 1;
  config.pbrKind = surfacePbr & kPortSurfacePbrStageMask;
  if (config.pbrKind != 0 && port_surface_pbr_stage(config) < 0) {
    config.pbrKind = 0;
  }
  if (envProbe) {
    config.pbrKind |= kPortEnvProbeBit;
    if (port_env_probe_stages(config) == 0) {
      config.pbrKind &= static_cast<u8>(~kPortEnvProbeBit);
    }
  }
  if (config.pbrKind == 0) {
    config.perPixelLight = oldPerPixel;
  }
}

ShaderInfo build_shader_info(const ShaderConfig& config) noexcept {
  ZoneScoped;

  ShaderInfo info{
      // render/logical viewport size, proj
      .uniformSize = 8 + 8 + 64,
  };

  if (config.lineMode != 0) {
    info.uniformSize += 4 + 4 + 4 + 4; // line_width, line_aspect_y, line_tex_offset, line_texcoord_mask
    info.lineMode = config.lineMode;
  }

  for (int attr = 0; attr < config.attrs.size(); attr++) {
    const auto attrType = config.attrs[attr].attrType;
    if ((attr == GX_VA_PNMTXIDX || (attr >= GX_VA_TEX0MTXIDX && attr <= GX_VA_TEX7MTXIDX)) && attrType == GX_DIRECT) {
      info.indexAttr.set(attr);
    } else if (attrType == GX_INDEX8 || attrType == GX_INDEX16) {
      info.indexAttr.set(attr);
    }
  }

  // 10 position matrices, 10 texture matrices, 10 normal matrices.
  info.uniformSize += sizeof(Mat3x4<float>) * 30;

  for (int i = 0; i < config.tevStageCount; ++i) {
    const auto& stage = config.tevStages[i];
    // Color pass
    color_arg_reg_info(stage.colorPass.a, stage, info);
    color_arg_reg_info(stage.colorPass.b, stage, info);
    color_arg_reg_info(stage.colorPass.c, stage, info);
    color_arg_reg_info(stage.colorPass.d, stage, info);
    info.writesTevReg.set(stage.colorOp.outReg);

    // Alpha pass
    alpha_arg_reg_info(stage.alphaPass.a, stage, info);
    alpha_arg_reg_info(stage.alphaPass.b, stage, info);
    alpha_arg_reg_info(stage.alphaPass.c, stage, info);
    alpha_arg_reg_info(stage.alphaPass.d, stage, info);
    if (!info.writesTevReg.test(stage.alphaOp.outReg)) {
      // If we're writing alpha to a register that's not been
      // written to in the shader, load from uniform buffer
      info.loadsTevReg.set(stage.alphaOp.outReg);
      info.writesTevReg.set(stage.alphaOp.outReg);
    }
  }
  for (int i = 0; i < config.tevStageCount; ++i) {
    const auto& stage = config.tevStages[i];
    // Skip if not enabled
    if (stage.indTexStage >= config.numIndStages) {
      continue;
    }
    const bool usesIndStage = stage.indTexMtxId != GX_ITM_OFF || is_alpha_bump_channel(stage.channelId);
    const bool usesTevTexCoord = stage.indTexMtxId != GX_ITM_OFF || stage.indTexWrapS != GX_ITW_OFF ||
                                 stage.indTexWrapT != GX_ITW_OFF || stage.indTexAddPrev;
    if (usesTevTexCoord && stage.texCoordId != GX_TEXCOORD_NULL) {
      info.sampledTexCoords.set(stage.texCoordId);
    }
    if (usesTevTexCoord && stage.texMapId != GX_TEXMAP_NULL &&
        (stage.indTexMtxId != GX_ITM_OFF || stage.indTexAddPrev)) {
      info.sampledTextures.set(stage.texMapId);
    }
    if (!usesIndStage) {
      continue;
    }
    info.usedIndStages.set(stage.indTexStage);
    const auto& indStage = config.indStages[stage.indTexStage];
    info.sampledTextures.set(indStage.texMapId);
    info.sampledTexCoords.set(indStage.texCoordId);
    info.sampledIndTextures.set(indStage.texMapId);
    // Track which indirect matrix is used
    if (stage.indTexMtxId >= GX_ITM_0 && stage.indTexMtxId <= GX_ITM_2) {
      info.usedIndTexMtxs.set(stage.indTexMtxId - GX_ITM_0);
    } else if (stage.indTexMtxId >= GX_ITM_S0 && stage.indTexMtxId <= GX_ITM_S2) {
      info.usedIndTexMtxs.set(stage.indTexMtxId - GX_ITM_S0);
    } else if (stage.indTexMtxId >= GX_ITM_T0 && stage.indTexMtxId <= GX_ITM_T2) {
      info.usedIndTexMtxs.set(stage.indTexMtxId - GX_ITM_T0);
    }
  }

  if (port_water_ripples(config)) {
    // GXPortSetWaterRipples: the height map and its two layers.
    info.sampledTexCoords.set(GX_TEXCOORD0);
    info.sampledTexCoords.set(GX_TEXCOORD1);
    info.sampledTextures.set(GX_TEXMAP7);
    info.usesWaterRipples = true;
  }

  // Emboss bump needs its source texcoord generated and a light enabled
  for (int i = 0; i < info.sampledTexCoords.size(); ++i) {
    if (!info.sampledTexCoords.test(i)) {
      continue;
    }
    const auto& tcg = config.tcgs[i];
    if (tcg.type >= GX_TG_BUMP0 && tcg.type <= GX_TG_BUMP7) {
      info.sampledTexCoords.set(tcg.embossSrc);
      info.lightingEnabled = true;
    }
  }

  // Texgens can consume a color channel even when TEV never reads it directly.
  // Include these channels before calculating lighting and uniform requirements.
  for (int i = 0; i < info.sampledTexCoords.size(); ++i) {
    if (!info.sampledTexCoords.test(i)) {
      continue;
    }
    const auto& tcg = config.tcgs[i];
    if (tcg.type >= GX_TG_BUMP0 && tcg.type <= GX_TG_BUMP7) {
      continue;
    }
    if (tcg.src == GX_TG_COLOR0) {
      info.sampledColorChannels.set(0);
    } else if (tcg.src == GX_TG_COLOR1) {
      info.sampledColorChannels.set(1);
    }
  }

  info.uniformSize += info.loadsTevReg.count() * sizeof(Vec4<float>);
  for (int i = 0; i < info.sampledColorChannels.size(); ++i) {
    if (info.sampledColorChannels.test(i)) {
      const auto& cc = config.colorChannels[i];
      const auto& cca = config.colorChannels[i + GX_ALPHA0];
      if (cc.lightingEnabled || cca.lightingEnabled) {
        info.lightingEnabled = true;
      }
    }
  }
  if (info.lightingEnabled) {
    // Lights + light state for all channels
    info.uniformSize += 16 + sizeof(Light) * GX::MaxLights;
  }
  for (int i = 0; i < info.sampledColorChannels.size(); ++i) {
    if (info.sampledColorChannels.test(i)) {
      const auto& cc = config.colorChannels[i];
      if (cc.lightingEnabled && cc.ambSrc == GX_SRC_REG) {
        info.uniformSize += sizeof(Vec4<float>);
      }
      if (cc.matSrc == GX_SRC_REG) {
        info.uniformSize += sizeof(Vec4<float>);
      }
      const auto& cca = config.colorChannels[i + GX_ALPHA0];
      if (cca.lightingEnabled && cca.ambSrc == GX_SRC_REG) {
        info.uniformSize += sizeof(Vec4<float>);
      }
      if (cca.matSrc == GX_SRC_REG) {
        info.uniformSize += sizeof(Vec4<float>);
      }
    }
  }
  if (info.usesTevKonst) {
    info.tevStageCount = config.tevStageCount;
    info.uniformSize += config.tevStageCount * sizeof(Vec4<float>);
  }
  for (int i = 0; i < info.sampledTexCoords.size(); ++i) {
    if (!info.sampledTexCoords.test(i)) {
      continue;
    }
    const auto& tcg = config.tcgs[i];
    if (tcg.postMtx != GX_PTIDENTITY) {
      u32 postMtxIdx = (tcg.postMtx - GX_PTTEXMTX0) / 3;
      info.usesPTTexMtx.set(postMtxIdx);
    }
  }
  const bool surfacePbr = port_surface_pbr_stage(config) >= 0;
  if (surfacePbr) {
    // GXPortSetSurfacePBR's maps, sampled on the relit stage's texture coordinates.
    info.sampledTextures.set(GX_TEXMAP6);
    info.sampledTextures.set(GX_TEXMAP7);
  }
  if (surfacePbr || port_env_probe_stages(config) != 0 || port_water_stage(config) >= 0) {
    // surface_pbr (highlight strength, the environment stages' probe level and weight) and
    // surface_probe (GXSetPBRProbe's view-to-probe rows and weight).
    info.usesSurfacePbr = true;
    info.uniformSize += sizeof(Vec4<float>) * 2 + sizeof(Mat3x4<float>); // surface_pbr, surface_params, surface_probe
    if (info.usesWaterRipples) {
      info.uniformSize += sizeof(Vec4<float>) * GX_PORT_WATER_WAVE_ROWS; // water_waves
    }
  }
  if (port_emissive_stages(config) != 0) {
    info.usesEmissive = true;
    info.uniformSize += sizeof(Vec4<float>); // emissive
  }
  if (config.pbr) {
    info.usesPbr = true;
    info.usesLightmap = config.pbrLightmapAttr != GX_VA_NULL;
    // The probe, 7 single + ambient 6 + volume 6 + tone 3 + backlight 3 + light skip + light scale, then the
    // linear light colours, then 3 HDR rows per light, then shield 8, the lightmap 4, the room lights and the model
    // modulation (diffuse, incandescence, coverage)
    info.uniformSize += sizeof(Mat3x4<float>) + sizeof(Vec4<float>) * (27 + GX::MaxLights * 4 + 8 + 4 + 1 + 3);
  } else if (config.sceneHdr) {
    // The TEV inverse tone curve and actual-blend compatibility flag (PBR reads pbr_tone).
    info.usesHdrTone = true;
    info.uniformSize += sizeof(Vec4<float>) * 4; // three tone rows, then hdr_tev_blend
  }
  if (config.hudSample >= 2) {
    info.usesHudDyin = true;
    info.uniformSize += sizeof(Vec4<float>);
  }
  if (info.usesPTTexMtx.any()) {
    info.uniformSize += sizeof(Mat3x4<float>) * MaxPTTexMtx;
  }
  if (config.fogType != GX_FOG_NONE) {
    info.usesFog = true;
    info.uniformSize += sizeof(Fog);
  }
  if (config.volFog != VolFogNone) {
    info.usesVolFog = true;
    // near, range, exposure; the tone curve
    info.uniformSize += sizeof(Vec4<float>) * 4;
  }
  info.uniformSize += MaxTexCoord * sizeof(Vec4<float>);
  if (info.usedIndTexMtxs.any()) {
    info.uniformSize += MaxIndTexMtxs * sizeof(Mat2x4<float>);
  }
  info.uniformSize += info.sampledTextures.count() * sizeof(Vec4<float>);
  if (config.shadow) {
    info.usesShadow = true;
    info.shadowReceive = shadow_receives(config);
    info.actorDecal = info.shadowReceive && config.actorDecal != 0;
    // The caster and receiver matrices, the sun's direction and colour (GXState::shadowUniform)
    info.uniformSize += sizeof(g_gxState.shadowUniform);
  }
  if (config.alphaCompare.ref0 == AlphaRefUniform || config.alphaCompare.ref1 == AlphaRefUniform) {
    info.usesAlphaRef = true;
    info.uniformSize += sizeof(Vec4<u32>);
  }
  info.uniformSize = gfx::align_uniform(info.uniformSize);
  if (info.uniformSize > MaxUniformSize) {
    Log.fatal("Uniform size exceeds maximum: {} > {}", info.uniformSize, MaxUniformSize);
  }
  return info;
}

// The pipeline cache keys the whole config, so state the shader never reads split it: of 1741 seed
// rows, 81 built a pipeline another row already had (tools/pipeline_key_audit.cpp). The game leaves
// lighting set on a channel no stage reads, a texgen no stage samples, and a stage's channel when it
// reads no rasterized colour. Those reset here, as does a texgen's matrix (the shader reads it from
// texcoord_scale[i].z). Not channel 1 of a PBR draw, nor stage 0's channel:
// pbr_func tells its projected-shadow stage by them whatever the stage reads.
void pipeline_unused_inputs(ShaderConfig& config) noexcept {
  const auto info = build_shader_info(config);
  for (int i = 0; i < info.sampledColorChannels.size(); ++i) {
    if (info.sampledColorChannels.test(i) || (i == 1 && config.pbr != 0)) {
      continue;
    }
    config.colorChannels[i] = {};
    config.colorChannels[i + GX_ALPHA0] = {};
  }
  for (int i = 0; i < info.sampledTexCoords.size(); ++i) {
    if (!info.sampledTexCoords.test(i)) {
      config.tcgs[i] = {};
    }
    // The texgen matrix comes from the uniform (texcoord_scale[i].z).
    config.tcgs[i].mtx = GX_IDENTITY;
  }
  const auto rasC = [](GXTevColorArg arg) { return arg == GX_CC_RASC || arg == GX_CC_RASA; };
  for (int i = 1; i < config.tevStageCount; ++i) {
    auto& stage = config.tevStages[i];
    const auto& c = stage.colorPass;
    const auto& a = stage.alphaPass;
    if (is_alpha_bump_channel(stage.channelId) || rasC(c.a) || rasC(c.b) || rasC(c.c) || rasC(c.d) ||
        a.a == GX_CA_RASA || a.b == GX_CA_RASA || a.c == GX_CA_RASA || a.d == GX_CA_RASA) {
      continue;
    }
    stage.channelId = GX_COLOR_NULL;
  }
}

// A stage's konst input: rgb as its kcSel, alpha as its kaSel (a fraction, or a K register or one of
// its components).
static Vec4<float> tev_konst(const TevStage& stage) noexcept {
  const auto konst = [](u32 sel, u32 comp) -> float {
    if (sel < 8) {
      return static_cast<float>(8 - sel) / 8.f; // GX_TEV_K?SEL_8_8 .. 1_8
    }
    if (sel >= GX_TEV_KCSEL_K0_R) { // K0_R .. K3_A: one component of a register
      return g_gxState.kcolors[(sel - GX_TEV_KCSEL_K0_R) % 4][(sel - GX_TEV_KCSEL_K0_R) / 4];
    }
    if (sel >= GX_TEV_KCSEL_K0) { // K0 .. K3 (colour only)
      return g_gxState.kcolors[sel - GX_TEV_KCSEL_K0][comp];
    }
    return 0.f;
  };
  return {konst(stage.kcSel, 0), konst(stage.kcSel, 1), konst(stage.kcSel, 2), konst(stage.kaSel, 3)};
}

static void fill_uniform(ByteBuffer& buf, const ShaderInfo& info) noexcept {
  buf.reserve_extra(info.uniformSize);

  buf.append<f32>(g_gxState.renderViewport.width);
  buf.append<f32>(g_gxState.renderViewport.height);
  buf.append<f32>(g_gxState.logicalViewport.width);
  buf.append<f32>(g_gxState.logicalViewport.height);
  if (info.lineMode != 0) {
    if (info.lineMode == 3) { // GX_POINTS
      buf.append<f32>(static_cast<f32>(g_gxState.pointSize) / 6.f);
      buf.append<f32>(1.0f);
      buf.append<f32>(tex_offset(g_gxState.pointTexOffset));
      buf.append<u32>(point_texcoord_mask());
    } else { // GX_LINES / GX_LINESTRIP
      buf.append<f32>(static_cast<f32>(g_gxState.lineWidth) / 6.f);
      buf.append<f32>(g_gxState.lineHalfAspect ? 0.5f : 1.f);
      buf.append<f32>(tex_offset(g_gxState.lineTexOffset));
      buf.append<u32>(line_texcoord_mask());
    }
  }
  auto proj = g_gxState.proj;
  if constexpr (UseReversedZ) {
    proj.m2 = proj.m2 * Vec4{-1.f, -1.f, -1.f, -1.f};
  } else {
    proj.m2 = proj.m2 + proj.m3;
  }
  buf.append(proj);

  for (int i = 0; i < MaxPnMtx; i++) {
    buf.append(g_gxState.pnMtx[i].pos);
  }

  for (int i = 0; i < MaxTexMtx; i++) {
    buf.append(g_gxState.texMtxs[i]);
  }

  for (int i = 0; i < MaxPnMtx; i++) {
    buf.append(g_gxState.pnMtx[i].nrm);
  }

  for (int i = 0; i < info.loadsTevReg.size(); ++i) {
    if (info.loadsTevReg.test(i)) {
      buf.append(g_gxState.colorRegs[i]);
    }
  }
  if (info.lightingEnabled) {
    // Lights
    static_assert(sizeof(g_gxState.lights) == 80 * GX::MaxLights);
    buf.append(g_gxState.lights);
    // Light state for all channels
    for (int i = 0; i < 4; ++i) {
      buf.append<u32>(g_gxState.colorChannelState[i].lightMask.to_ulong());
    }
  }
  for (int i = 0; i < info.sampledColorChannels.size(); ++i) {
    if (!info.sampledColorChannels.test(i)) {
      continue;
    }
    const auto& ccc = g_gxState.colorChannelConfig[i];
    const auto& ccs = g_gxState.colorChannelState[i];
    if (ccc.lightingEnabled && ccc.ambSrc == GX_SRC_REG) {
      buf.append(ccs.ambColor);
    }
    if (ccc.matSrc == GX_SRC_REG) {
      buf.append(ccs.matColor);
    }
    const auto& ccca = g_gxState.colorChannelConfig[i + GX_ALPHA0];
    const auto& ccsa = g_gxState.colorChannelState[i + GX_ALPHA0];
    if (ccca.lightingEnabled && ccca.ambSrc == GX_SRC_REG) {
      buf.append(ccsa.ambColor);
    }
    if (ccca.matSrc == GX_SRC_REG) {
      buf.append(ccsa.matColor);
    }
  }
  if (info.usesTevKonst) {
    for (u32 i = 0; i < info.tevStageCount; ++i) {
      buf.append(tev_konst(g_gxState.tevStages[i]));
    }
  }
  if (info.usesPbr) {
    buf.append(g_gxState.pbrProbe);
    buf.append(g_gxState.pbrEmissive);
    buf.append(g_gxState.pbrBacklight);
    buf.append(g_gxState.pbrLayer);
    buf.append(g_gxState.pbrLayerHeight);
    buf.append(g_gxState.pbrParam);
    buf.append(g_gxState.pbrUp);
    buf.append(g_gxState.pbrCubeParams);
    for (const auto& v : g_gxState.pbrAmbient) {
      buf.append(v);
    }
    for (const auto& v : g_gxState.pbrVolumeRows) {
      buf.append(v);
    }
    for (const auto& v : g_gxState.pbrTone) {
      buf.append(v);
    }
    for (const auto& v : g_gxState.pbrBacklightLights) {
      buf.append(v);
    }
    // yzw carry the baked-light modulation.
    Vec4<float> lightSkip = g_gxState.pbrLightSkip;
    lightSkip.y() = g_gxState.pbrBakedLightModulation.x();
    lightSkip.z() = g_gxState.pbrBakedLightModulation.y();
    lightSkip.w() = g_gxState.pbrBakedLightModulation.z();
    buf.append(lightSkip);
    // z says whether the environment BRDF table is in use.
    Vec4<float> lightScale = g_gxState.pbrLightScale;
    lightScale.z() = g_gxState.pbrBrdfLut ? 1.f : 0.f;
    buf.append(lightScale);
    // The lights' colours made linear here once, not per light in every pixel. Lights rarely change
    // between draws, so the last colours seen per slot are kept with their linear values.
    static std::array<Vec4<float>, GX::MaxLights> lastColor{};
    static std::array<Vec4<float>, GX::MaxLights> lastLinear{};
    for (size_t i = 0; i < g_gxState.lights.size(); ++i) {
      const auto& color = g_gxState.lights[i].color;
      if (color.x() != lastColor[i].x() || color.y() != lastColor[i].y() || color.z() != lastColor[i].z()) {
        const auto linear = [](float c) { return std::pow(std::max(c, 0.f), 2.2f); };
        lastColor[i] = color;
        lastLinear[i] = Vec4<float>{linear(color.x()), linear(color.y()), linear(color.z()), 0.f};
      }
      buf.append(lastLinear[i]);
    }
    for (const auto& v : g_gxState.pbrLightHdr) {
      buf.append(v);
    }
    for (const auto& v : g_gxState.pbrShield) {
      buf.append(v);
    }
    buf.append(g_gxState.pbrLightmapRect);
    for (const auto& v : g_gxState.pbrLightmapAxes) {
      buf.append(v);
    }
    buf.append(g_gxState.pbrRoomLights);
    buf.append(g_gxState.pbrModelDiffuse);
    buf.append(g_gxState.pbrModelIncan);
    buf.append(g_gxState.pbrModelCoverage);
  }
  if (info.usesHdrTone) {
    for (const auto& v : g_gxState.pbrTone) {
      buf.append(v);
    }
    // x: a source weighted by its alpha, SRCALPHA/ONE (additive) or SRCALPHA/INVSRCALPHA (alpha over), goes
    // through the inverse curve already weighted (hdr_tev_srcalpha_add). ONE/INVSRCALPHA needs nothing: its
    // source is premultiplied, so the plain inverse already gives the source term exactly.
    const bool compatSrcAlphaAdditive =
        g_gxState.blendMode == GX_BM_BLEND && g_gxState.blendFacSrc == GX_BL_SRCALPHA &&
        (g_gxState.blendFacDst == GX_BL_ONE || g_gxState.blendFacDst == GX_BL_INVSRCALPHA);
    // y: the source colour scales the destination (hdr_tev_dst_factor): 1 dst * src, 2 dst * (1 + src).
    float dstFactor = 0.f;
    if (g_gxState.blendMode == GX_BM_BLEND) {
      const auto src = g_gxState.blendFacSrc;
      const auto dst = g_gxState.blendFacDst;
      if ((src == GX_BL_ZERO && dst == GX_BL_SRCCLR) || (src == GX_BL_DSTCLR && dst == GX_BL_ZERO)) {
        dstFactor = 1.f;
      } else if (src == GX_BL_DSTCLR && dst == GX_BL_ONE) {
        dstFactor = 2.f;
      }
    }
    buf.append(Vec4<float>{compatSrcAlphaAdditive ? 1.f : 0.f, dstFactor, 0.f, 0.f});
  }
  if (info.usesHudDyin) {
    buf.append(g_gxState.hudDyin);
  }
  if (info.usesSurfacePbr) {
    // y: the probe level an environment stage reads (0..MipCount-1), z: its reflection's strength
    // (aurora_set_env_probe_look).
    float envLevel = 0.f;
    float envStrength = 1.f;
    env_probe_look(envLevel, envStrength);
    // A draw's own look (GXPortSetEnvProbeLook) over the frame's.
    if (g_gxState.envProbeLook.y() >= 0.f) {
      envLevel = g_gxState.envProbeLook.x();
      envStrength = g_gxState.envProbeLook.y();
    }
    // w: GXPortSetSurfacePBRStrength, or on rippled water (which has no PBR stage) GXPortSetWaterRipples.
    buf.append(Vec4<float>{surface_pbr_highlight() * g_gxState.surfacePbrHighlight, envLevel, envStrength,
                           info.usesWaterRipples ? g_gxState.waterRipples : g_gxState.surfacePbrStrength});
    buf.append(g_gxState.surfacePbrParams);
    buf.append(g_gxState.pbrProbe);
    if (info.usesWaterRipples) {
      for (const auto& row : g_gxState.waterWaves) {
        buf.append(row);
      }
    }
  }
  if (info.usesEmissive) {
    buf.append(Vec4<float>{g_gxState.emissiveScale, 0.f, 0.f, 0.f});
  }
  if (info.usesPTTexMtx.any()) {
    for (int i = 0; i < info.usesPTTexMtx.size(); ++i) {
      buf.append(g_gxState.ptTexMtxs[i]);
    }
  }
  if (info.usesFog) {
    const auto& state = g_gxState.fog;
    Fog fog{
        .color = state.color,
        .a = state.a,
        .b = state.b,
        .c = state.c,
        .type = static_cast<u32>(state.type),
    };
    buf.append(fog);
  }
  if (info.usesVolFog) {
    buf.append(g_gxState.volFogParams);
    for (const auto& v : g_gxState.volFogTone) {
      buf.append(v);
    }
  }
  static_assert(std::tuple_size_v<decltype(g_gxState.texCoordScales)> == MaxTexCoord);
  for (u32 i = 0; i < MaxTexCoord; ++i) {
    const auto& scale = g_gxState.texCoordScales[i];
    // z: the texgen's matrix as a float index into postex_mtx (the shader reads it, the key does not).
    const u32 mtx = g_gxState.tcgs[i].mtx;
    const u32 mtxIdx = mtx == GX_IDENTITY ? TexMtxIdentity : mtx / 3;
    buf.append(Vec4{static_cast<f32>(scale.scaleS) + 1.0f, static_cast<f32>(scale.scaleT) + 1.0f,
                    static_cast<f32>(mtxIdx), 0.0f});
  }
  if (info.usedIndTexMtxs.any()) {
    for (u32 i = 0; i < MaxIndTexMtxs; ++i) {
      const auto& mtx = g_gxState.indTexMtxs[i];
      buf.append(Vec4{mtx.mtx.m0.x, mtx.mtx.m0.y, mtx.mtx.m1.x, mtx.mtx.m1.y});
      buf.append(Vec4{mtx.mtx.m2.x, mtx.mtx.m2.y, std::exp2f(mtx.scaleExp), 0.0f});
    }
  }
  for (int i = 0; i < info.sampledTextures.size(); ++i) {
    if (!info.sampledTextures.test(i)) {
      continue;
    }
    buf.append(texture_size_bias(get_texture(static_cast<GXTexMapID>(i))));
  }
  if (info.usesShadow) {
    for (const auto& v : g_gxState.shadowUniform) {
      buf.append(v);
    }
  }
  if (info.usesAlphaRef) {
    buf.append(Vec4<u32>{g_gxState.alphaCompare.ref0, g_gxState.alphaCompare.ref1, 0, 0});
  }
}

gfx::Range build_uniform(const ShaderInfo& info) noexcept {
  ZoneScoped;
  // Filled in place in the frame's uniform buffer, inside a MaxUniformSize window (build_shader_info
  // refuses anything larger), then trimmed to what was written.
  uint8_t* out = nullptr;
  auto range = gfx::map_uniform(out);
  if (out == nullptr) {
    return range;
  }
  ByteBuffer buf{out, MaxUniformSize};
  fill_uniform(buf, info);
  // build_shader_info's size formula must match the bytes fill_uniform appends (debug builds only).
  assert(gfx::align_uniform(buf.size()) == info.uniformSize && "uniformSize formula out of step with fill_uniform");
  gfx::unmap_uniform(range, buf.size());
  return range;
}
} // namespace aurora::gx
