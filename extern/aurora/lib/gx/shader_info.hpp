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
}; // namespace aurora::gx
