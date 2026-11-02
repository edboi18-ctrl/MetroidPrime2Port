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
}; // namespace aurora::gx
