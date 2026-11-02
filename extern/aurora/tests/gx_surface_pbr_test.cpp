#include <string>

#include <gtest/gtest.h>

#include "gx/gx.hpp"
#include "gx/shader_info.hpp"

namespace aurora::gx {
namespace {

// GXPortSetSurfacePBR (GX_AURORA_PORT_SURFACE_PBR): pbr 0, pbrKind = the relit stage + 1, per-pixel lights.

void set_stage(TevStage& stage, GXTevColorArg a, GXTevColorArg b, GXTevColorArg c, GXTevColorArg d, GXTevRegID out,
               u32 texIdx) {
  stage.colorPass = {a, b, c, d};
  stage.colorOp.outReg = out;
  stage.colorOp.clamp = true;
  stage.alphaPass = {GX_CA_ZERO, GX_CA_ZERO, GX_CA_ZERO, GX_CA_RASA};
  stage.texCoordId = static_cast<GXTexCoordID>(texIdx);
  stage.texMapId = static_cast<GXTexMapID>(texIdx);
  stage.channelId = GX_COLOR0A0;
}

ShaderConfig base_config(u32 stages) {
  ShaderConfig config;
  config.tevStageCount = stages;
  for (const auto attr : {GX_VA_POS, GX_VA_NRM, GX_VA_TEX0, GX_VA_TEX1, GX_VA_TEX2}) {
    auto& a = config.attrs[attr];
    a.attrType = GX_DIRECT;
    a.cnt = attr == GX_VA_POS || attr == GX_VA_NRM ? 3 : 2;
    a.compType = GX_F32;
    a.stride = 12;
  }
  for (u32 i = 0; i < 3; ++i) {
    config.tcgs[i].src = static_cast<GXTexGenSrc>(GX_TG_TEX0 + i);
  }
  auto& cc = config.colorChannels[GX_COLOR0];
  cc.lightingEnabled = true;
  cc.matSrc = GX_SRC_REG;
  cc.ambSrc = GX_SRC_REG;
  cc.diffFn = GX_DF_CLAMP;
  cc.attnFn = GX_AF_SPOT;
  config.perPixelLight = 1;
  return config;
}

// A room's surface: C0 = RASC + C1 x lightmap; PREV = C0 x diffuse; PREV += glow.
ShaderConfig room_config() {
  auto config = base_config(3);
  set_stage(config.tevStages[0], GX_CC_ZERO, GX_CC_C1, GX_CC_TEXC, GX_CC_RASC, GX_TEVREG0, 0);
  set_stage(config.tevStages[1], GX_CC_ZERO, GX_CC_C0, GX_CC_TEXC, GX_CC_ZERO, GX_TEVPREV, 1);
  set_stage(config.tevStages[2], GX_CC_ZERO, GX_CC_TEXC, GX_CC_ONE, GX_CC_CPREV, GX_TEVPREV, 2);
  config.pbrKind = 2;
  return config;
}

// A model's surface: PREV = RASC x diffuse.
ShaderConfig model_config() {
  auto config = base_config(1);
  set_stage(config.tevStages[0], GX_CC_ZERO, GX_CC_TEXC, GX_CC_RASC, GX_CC_ZERO, GX_TEVPREV, 0);
  config.pbrKind = 1;
  return config;
}

// The arm cannon's shape (Echoes' beam guns): base x lit colour; a mask into C2; PREV += C2 x an
// environment map on texture coordinates generated from the normal.
ShaderConfig gun_config() {
  auto config = base_config(3);
  set_stage(config.tevStages[0], GX_CC_ZERO, GX_CC_RASC, GX_CC_TEXC, GX_CC_ZERO, GX_TEVPREV, 0);
  set_stage(config.tevStages[1], GX_CC_ZERO, GX_CC_ZERO, GX_CC_ZERO, GX_CC_TEXC, GX_TEVREG2, 1);
  set_stage(config.tevStages[2], GX_CC_ZERO, GX_CC_C2, GX_CC_TEXC, GX_CC_CPREV, GX_TEVPREV, 2);
  config.tcgs[2].src = GX_TG_NRM;
  config.tcgs[2].type = GX_TG_MTX3x4;
  config.pbrKind = kPortEnvProbeBit;
  return config;
}

TEST(GxSurfacePbr, EnvironmentStageReadsTheProbe) {
  const auto config = gun_config();
  EXPECT_EQ(port_env_probe_stages(config), 1u << 2);
  EXPECT_EQ(port_surface_pbr_stage(config), -1);
  const auto info = build_shader_info(config);
  EXPECT_TRUE(info.usesSurfacePbr);
  EXPECT_FALSE(info.sampledTextures.test(GX_TEXMAP6));
  const std::string source = build_shader_source(config);
  EXPECT_NE(source.find("TEV stage 2's environment map is the live probe"), std::string::npos) << source;
  EXPECT_EQ(source.find("TEV stage 0's environment map"), std::string::npos);
  EXPECT_NE(source.find("var pbr_cube: texture_cube<f32>;"), std::string::npos);
  EXPECT_NE(source.find("surface_probe: mat3x4f,"), std::string::npos);
  EXPECT_NE(source.find("let pe2_r = reflect(normalize(in.mv_pos), pp_nrm);"), std::string::npos);
  // Replaced before the TEV stages read it.
  EXPECT_LT(source.find("sampled2 = vec4f(mix(sampled2.rgb, pe2_c,"), source.find("// TEV stage 0"));
  EXPECT_EQ(source.find("pbrs_"), std::string::npos);
}

TEST(GxSurfacePbr, EnvironmentProbeAndRelitStageTogether) {
  auto config = gun_config();
  config.pbrKind = kPortEnvProbeBit | 1;
  EXPECT_EQ(port_surface_pbr_stage(config), 0);
  EXPECT_EQ(port_env_probe_stages(config), 1u << 2);
  const std::string source = build_shader_source(config);
  EXPECT_NE(source.find("GXPortSetSurfacePBR: TEV stage 0 relit"), std::string::npos);
  EXPECT_NE(source.find("TEV stage 2's environment map is the live probe"), std::string::npos);
  // The relit stage reflects the probe too once it is filled.
  EXPECT_NE(source.find("textureSampleLevel(pbr_cube, pbr_cube_samp, pbrs_pd, pbrs_rough * 5.0)"), std::string::npos);
}

TEST(GxSurfacePbr, EnvironmentProbeRefusals) {
  {
    auto config = gun_config();
    config.pbrKind = 0;
    EXPECT_EQ(port_env_probe_stages(config), 0u);
    EXPECT_FALSE(build_shader_info(config).usesSurfacePbr);
  }
  {
    auto config = gun_config();
    config.perPixelLight = 0;
    EXPECT_EQ(port_env_probe_stages(config), 0u);
  }
  {
    auto config = gun_config();
    config.pbr = 1;
    EXPECT_EQ(port_env_probe_stages(config), 0u);
  }
  {
    auto config = gun_config();
    config.colorChannels[GX_COLOR0].lightingEnabled = false;
    EXPECT_EQ(port_env_probe_stages(config), 0u);
  }
  {
    auto config = gun_config();
    config.tcgs[2].src = GX_TG_TEX2;
    EXPECT_EQ(port_env_probe_stages(config), 0u) << "not an environment map";
  }
}

TEST(GxSurfacePbr, RoomDiffuseStageIsRelit) {
  const auto config = room_config();
  EXPECT_EQ(port_surface_pbr_stage(config), 1);
  const auto info = build_shader_info(config);
  EXPECT_TRUE(info.sampledTextures.test(GX_TEXMAP6));
  EXPECT_TRUE(info.sampledTextures.test(GX_TEXMAP7));
  EXPECT_TRUE(info.usesSurfacePbr);
  EXPECT_FALSE(info.usesPbr);

  const std::string source = build_shader_source(config);
  const auto block = source.find("GXPortSetSurfacePBR: TEV stage 1 relit");
  ASSERT_NE(block, std::string::npos) << source;
  const auto stage1 = source.find("// TEV stage 1");
  const auto stage2 = source.find("// TEV stage 2");
  ASSERT_NE(stage1, std::string::npos);
  ASSERT_NE(stage2, std::string::npos);
  // After the stage's own line, before the glow layer, which then adds to the relit colour.
  EXPECT_LT(stage1, block);
  EXPECT_LT(block, stage2);
  EXPECT_NE(source.find("let pbrs_uv = tex1_uv;"), std::string::npos);
  EXPECT_NE(source.find("let pbrs_base = max(vec3f(sampled1.rgb), vec3f(0.0));"), std::string::npos);
  EXPECT_NE(source.find("textureSampleBias(tex6, tex6_samp, pbrs_uv, ubuf.tex6_size_bias.z)"), std::string::npos);
  EXPECT_NE(source.find("textureSampleBias(tex7, tex7_samp, pbrs_uv, ubuf.tex7_size_bias.z)"), std::string::npos);
  // L is C0 (the lit colour plus the lightmap): its lit part is replaced, the lightmap kept.
  EXPECT_NE(source.find("let pbrs_light = max(vec3f(tevreg0.rgb) - rast0.rgb + pbrs_rast, vec3f(0.0));"),
            std::string::npos);
  EXPECT_NE(source.find("var pbrs_lighting = ubuf.cc0_amb;"), std::string::npos);
  EXPECT_NE(source.find("let pbrs_rast = (ubuf.cc0_mat * clamp(pbrs_lighting"), std::string::npos);
  EXPECT_NE(source.find("var diff = max(0.0, dot(ldir, pbrs_n));"), std::string::npos);
  EXPECT_NE(source.find("prev = vec4f(clamp(pbrs_lit, vec3f(0.0), vec3f(1.0)), prev.a);"), std::string::npos);
  // The highlights: each light feeds at most 1.0, all of them together at most 1.0 (GX's clamped
  // sum), scaled by aurora_set_pbr_highlight through the surface_pbr uniform.
  EXPECT_NE(source.find("surface_pbr: vec4f,"), std::string::npos);
  EXPECT_NE(source.find("let e = min(max(attn, 0.0) * max(light.color.rgb, vec3f(0.0)), vec3f(1.0));"),
            std::string::npos);
  EXPECT_NE(source.find("pbrs_spec *= min(vec3f(1.0), 1.0 / max(pbrs_feed, vec3f(1e-4))) * ubuf.surface_pbr.x;"),
            std::string::npos);
}

TEST(GxSurfacePbr, ModelStageIsRelit) {
  const auto config = model_config();
  EXPECT_EQ(port_surface_pbr_stage(config), 0);
  const std::string source = build_shader_source(config);
  EXPECT_NE(source.find("GXPortSetSurfacePBR: TEV stage 0 relit"), std::string::npos);
  EXPECT_NE(source.find("let pbrs_base = max(vec3f(sampled0.rgb), vec3f(0.0));"), std::string::npos);
  EXPECT_NE(source.find("let pbrs_light = max(vec3f(rast0.rgb) - rast0.rgb + pbrs_rast, vec3f(0.0));"),
            std::string::npos);
}

TEST(GxSurfacePbr, OffLeavesTheShaderAlone) {
  auto config = room_config();
  config.pbrKind = 0;
  EXPECT_EQ(port_surface_pbr_stage(config), -1);
  EXPECT_FALSE(build_shader_info(config).sampledTextures.test(GX_TEXMAP6));
  EXPECT_FALSE(build_shader_info(config).usesSurfacePbr);
  EXPECT_EQ(build_shader_source(config).find("pbrs_"), std::string::npos);
}

TEST(GxSurfacePbr, RefusesStagesItCannotRelight) {
  const auto refused = [](const ShaderConfig& config, const char* why) {
    EXPECT_EQ(port_surface_pbr_stage(config), -1) << why;
    EXPECT_EQ(build_shader_source(config).find("pbrs_"), std::string::npos) << why;
  };
  {
    auto config = room_config();
    config.perPixelLight = 0;
    refused(config, "vertex lighting");
  }
  {
    auto config = room_config();
    config.pbr = 1;
    refused(config, "GXSetPBR's draw");
  }
  {
    auto config = room_config();
    config.colorChannels[GX_COLOR0].lightingEnabled = false;
    refused(config, "unlit channel");
  }
  {
    auto config = room_config();
    config.pbrKind = 4;
    refused(config, "stage past the last");
  }
  {
    auto config = room_config();
    config.pbrKind = 1;
    refused(config, "the lightmap stage (d is RASC)");
  }
  {
    auto config = room_config();
    config.pbrKind = 3;
    refused(config, "the glow stage");
  }
  {
    auto config = room_config();
    config.tevStages[2].texMapId = GX_TEXMAP7;
    refused(config, "material uses map 7");
  }
  {
    auto config = room_config();
    config.tevStages[1].colorOp.bias = GX_TB_ADDHALF;
    refused(config, "biased stage");
  }
  {
    auto config = room_config();
    config.tevStages[1].colorOp.outReg = GX_TEVREG0;
    refused(config, "stage overwrites L");
  }
  {
    auto config = room_config();
    config.tevStages[0].colorPass.d = GX_CC_ZERO;
    refused(config, "L isn't lit");
  }
  {
    auto config = room_config();
    config.tevStages[1].colorPass.b = GX_CC_C2;
    refused(config, "L never set");
  }
  {
    auto config = model_config();
    config.tevStages[0].tevSwapRas = GX_TEV_SWAP1;
    config.tevSwapTable[1] = TevSwap{GX_CH_RED, GX_CH_RED, GX_CH_RED, GX_CH_ALPHA};
    refused(config, "swapped raster colour");
  }
  {
    auto config = model_config();
    config.tevStages[0].channelId = GX_COLOR1A1;
    refused(config, "channel 1");
  }
}

} // namespace
} // namespace aurora::gx
