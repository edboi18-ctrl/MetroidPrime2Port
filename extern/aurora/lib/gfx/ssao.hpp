#pragma once

#include <cstdint>
#include <string>

// Port extension: screen-space ambient obscurance over the finished EFB (GXPortAmbientOcclusion), from
// its depth alone: McGuire, Mara and Luebke's Scalable Ambient Obscurance (HPG 2012) without its depth
// MIP chain. A half-resolution pass estimates the obscurance at each pixel with the AlchemyAO estimator
// (the paper's equation 1) over 9 samples on a 7-turn spiral (equations 5 to 8), two 7-tap bilateral
// passes smooth it, and a full-resolution pass multiplies the frame by it (in linear light), weighting
// the four nearest half-resolution texels by depth so that no edge bleeds. Only depths in the world's
// GX z range are touched: nearer is the viewmodel, and the far plane is the sky.
namespace aurora::gfx::ssao {

// The task's uniform, word for word. View space is GX's: x right, y up, z towards the camera.
struct Params {
  float frustum[4]; // left, right, top, bottom at a view depth of 1
  float depth[4];   // near, far, and the GX z range the world draws in (min, max)
  float ao[4];      // radius (world units), intensity (sigma), bias (beta, per unit of depth), fade start
  float misc[4];    // fade end (no obscurance from there on), then 0 (the task sets the rest)
};
static_assert(sizeof(Params) == 64);

// Registers the encoder task (game thread); false if it could not be (no core features: the passes
// read the depth buffer with textureLoad).
bool ensure_task();
// Records the obscurance from the FIFO processor (GX_AURORA_PORT_AMBIENT_OCCLUSION), once ensure_task
// has returned true. False if nothing was recorded.
bool record(const Params& params);
void shutdown();

// The passes' WGSL for a depth buffer's sample count (exposed for the tests).
std::string shader_source(uint32_t samples);
} // namespace aurora::gfx::ssao
