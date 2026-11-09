#pragma once

#include "gx.hpp"

namespace aurora::gx {
ShaderInfo build_shader_info(const ShaderConfig& config) noexcept;
gfx::Range build_uniform(const ShaderInfo& info) noexcept;
u8 color_channel(GXChannelID id) noexcept;
// Port surface PBR (GXPortSetSurfacePBR: pbr 0, pbrKind = stage + 1): the TEV stage relit with the
// ORM map (GX_TEXMAP6) and the normal map (GX_TEXMAP7), or -1 when the draw's TEV doesn't have the
// shape it relights (see port_surface_pbr in shader.cpp).
int port_surface_pbr_stage(const ShaderConfig& config) noexcept;
// GXPortSetEnvProbe: the TEV stages (a bit each) whose texture coordinates are generated from the
// normal (GX_TG_NRM through a matrix: an environment map), which sample the live probe instead.
// 0 when the bit isn't set, or the draw can't (GXSetPBR's, unlit channel 0, vertex lighting).
u32 port_env_probe_stages(const ShaderConfig& config) noexcept;
// GXPortSetEmissive: the glow stages (a bit each), whose texture colour the emissive uniform scales.
// After the first stage, one that adds its texture onto the previous colour (d is CPREV and the
// texture is in the lerp, as a term or as the factor of a lerp from 0; or d is the texture and CPREV
// is in the lerp), writes PREV with an add, takes its coordinates from the model's UVs (GX_TG_TEX0..7:
// not an environment or projected map) and isn't lit (no RASC or RASA in the lerp, and no later stage
// multiplies CPREV by them). The disc's glow layer is CPREV + TEXC after the lit diffuse
// (docs/research/PBR_TEXTURES.md); tools/material_editor/material_core.py glow_stages is the same rule.
// 0 for GXSetPBR's draws, lines and the water.
u32 port_emissive_stages(const ShaderConfig& config) noexcept;
// GXPortSetWaterReflection: the TEV stage whose texture sample becomes the live probe reflected off
// the water, or -1.
int port_water_stage(const ShaderConfig& config) noexcept;
// GXPortSetWaterRipples: the water draw's normal is tipped by GX_TEXMAP7's height on TEXCOORD0 and
// TEXCOORD1 (kPortWaterRippleBit on a port_water_stage draw whose two texgens come from the position).
bool port_water_ripples(const ShaderConfig& config) noexcept;
// GXPortSetSurfacePBR (surfacePbr: the stage + 1, 0 off) and GXPortSetEnvProbe on a config whose
// TEV stages, swap table, colour channels and texgens are already filled, with pbr 0: pbrKind
// takes the stage + 1 and the probe bit, each kept only when it applies, so a draw they can't
// change keeps the config, and the pipeline, it had; perPixelLight is set while either is.
void apply_port_surface_flags(ShaderConfig& config, u8 surfacePbr, bool envProbe, u8 waterReflect = 0,
                              bool waterRipples = false) noexcept;
}; // namespace aurora::gx
