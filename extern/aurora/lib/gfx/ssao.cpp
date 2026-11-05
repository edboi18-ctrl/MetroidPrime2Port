#include "ssao.hpp"

#include "../logging.hpp"
#include "../webgpu/gpu.hpp"
#include "../webgpu/gpu_prof.hpp"
#include "frame.hpp"
#include "recording.hpp"

#include <aurora/gfx.hpp>

#include <array>
#include <cstring>
#include <string>
#include "texture_sites.hpp"

// Screen-space ambient obscurance (see ssao.hpp). M. McGuire, M. Mara, D. Luebke, "Scalable Ambient
// Obscurance", HPG 2012 (research.nvidia.com/publication/2012-06_scalable-ambient-obscurance):
//  - each sample's occlusion, in place of eq. 1's (AlchemyAO's) max(0, v.n + z_C beta) / (v.v + eps): its rise
//    above the surface, n . v/|v| (the sine of its elevation), less an angle bias b, times a falloff to the
//    radius, max(0, n . v/|v| - b) (1 - v.v / r^2); A = max(0, 1 - sigma / (s (1 - b)) sum). Eq. 1's 1 / v.v
//    grows without bound towards a fold, so the shallow folds between a low-poly room's facets (10 to 30
//    degrees) drew dark lines along every edge; samples less than asin(b) above the surface count for
//    nothing here, as NVIDIA's HBAO+ bias does ("can be used to hide low-tessellation artifacts", "weights
//    the AO contributions more strongly for samples towards the normal direction");
//  - eq. 3 and 4: C from the depth at the pixel, its face normal from the neighbouring positions (the
//    pair with the smaller depth step on each axis, so a silhouette doesn't bend it);
//  - eq. 5: the screen radius r' = r S' / |z_C|, S' the pixels of 1 unit at depth 1;
//  - eq. 6 to 8: sample i at alpha = (i + 0.5) / s, h = r' alpha, theta = 2 pi alpha tau + phi, s = 9 and
//    tau = 7 (the paper's pair), phi the per-pixel XOR hash rotation;
//  - section 2.4: two 1D bilateral passes of 7 taps (Gaussian weights, cut by the depth difference).
// The paper's z MIP chain (for radii far beyond the blur) is left out: the radius here is a metre or two.
// The obscurance is computed at half the EFB's size and drawn over the frame at full size. The EFB holds
// sRGB-encoded colour (as bloom.cpp reads it), so the frame is decoded, multiplied and encoded again.
// The world's GX fog is already in the frame, mixed in the encoded values (lib/gx/shader.cpp: prev =
// mix(prev, fog colour, f)), so the composite works out f again from the pixel's window z and the fog
// the world was drawn with, takes the fog colour's share back out, darkens the surface left and mixes
// the fog back over it: a pixel all fog is not darkened at all, as the surface under it would not show.
namespace aurora::gfx::ssao {
namespace {
Module Log("aurora::gfx::ssao");
using webgpu::g_device;

constexpr auto AoFormat = wgpu::TextureFormat::RG16Float; // r: the visibility, g: the view depth (0: none)
// A slot is live from record to the encode of its frame, at most FrameSlotCount frames; one a frame.
constexpr size_t RecordedSlots = 4;
constexpr size_t MaxPerFrame = 1;
static_assert(MaxPerFrame * detail::FrameSlotCount <= RecordedSlots, "ssao slots would be overwritten while pending");

constexpr const char* Source = R"(
struct Params {
  frustum: vec4f,
  depth: vec4f,
  ao: vec4f,
  misc: vec4f,
  // The GX fog (ssao::Fog): its colour, then a, b, c and the GXFogType.
  fog_color: vec4f,
  fog_a: f32,
  fog_b: f32,
  fog_c: f32,
  fog_kind: u32,
};

@group(0) @binding(0) var<uniform> p: Params;
@group(0) @binding(1) var depthTex: DEPTH_TYPE;
@group(0) @binding(2) var src: texture_2d<f32>;

struct VertexOutput {
  @builtin(position) pos: vec4f,
};

@vertex
fn vs_main(@builtin(vertex_index) i: u32) -> VertexOutput {
  var corners = array<vec2f, 3>(vec2f(-1.0, -1.0), vec2f(3.0, -1.0), vec2f(-1.0, 3.0));
  var out: VertexOutput;
  out.pos = vec4f(corners[i], 0.0, 1.0);
  return out;
}

// GX's window z at a full-size pixel (reversed Z, as volfog.cpp reads it): the z the fog was computed from.
fn window_z(at: vec2i) -> f32 {
  let size = vec2i(textureDimensions(depthTex));
  return 1.0 - textureLoad(depthTex, clamp(at, vec2i(0), size - vec2i(1)), 0);
}

// The view depth (positive, in world units) at a full-size pixel; 0 outside the world's depth range
// (the viewmodel nearer, the sky at the far plane).
fn view_depth(at: vec2i) -> f32 {
  let z = window_z(at);
  if (z < p.depth.z || z >= p.depth.w - 1e-6) {
    return 0.0;
  }
  let d = clamp((z - p.depth.z) / max(p.depth.w - p.depth.z, 1e-6), 0.0, 1.0);
  let near = p.depth.x;
  let far = p.depth.y;
  return near * far / (far - d * (far - near));
}

// Eq. 3: the view-space position at a full-size pixel of the given depth.
fn view_pos(at: vec2i, depth: f32) -> vec3f {
  let size = vec2f(textureDimensions(depthTex));
  let uv = (vec2f(at) + vec2f(0.5)) / size;
  return vec3f(mix(p.frustum.x, p.frustum.y, uv.x) * depth, mix(p.frustum.z, p.frustum.w, uv.y) * depth, -depth);
}

fn pos_at(at: vec2i) -> vec4f {
  let depth = view_depth(at);
  return vec4f(view_pos(at, depth), depth);
}

@fragment
fn fs_ao(in: VertexOutput) -> @location(0) vec4f {
  let cell = vec2i(floor(in.pos.xy));
  let q = cell * 2;
  let c4 = pos_at(q);
  if (c4.w <= 0.0) {
    return vec4f(1.0, 0.0, 0.0, 0.0);
  }
  let c = c4.xyz;
  // Eq. 4 from the neighbours: on each axis the side whose depth steps less.
  let px = pos_at(q + vec2i(1, 0));
  let nx = pos_at(q - vec2i(1, 0));
  let py = pos_at(q + vec2i(0, 1));
  let ny = pos_at(q - vec2i(0, 1));
  let ex = select(c - nx.xyz, px.xyz - c, px.w > 0.0 && (nx.w <= 0.0 || abs(px.w - c4.w) < abs(c4.w - nx.w)));
  let ey = select(c - ny.xyz, py.xyz - c, py.w > 0.0 && (ny.w <= 0.0 || abs(py.w - c4.w) < abs(c4.w - ny.w)));
  var n = cross(ey, ex);
  if (dot(n, n) < 1e-12) {
    return vec4f(1.0, c4.w, 0.0, 0.0);
  }
  n = normalize(n);
  if (dot(n, -c) < 0.0) {
    n = -n; // facing the camera, whichever way the pixel axes turn
  }
  let radius = p.ao.x;
  let r2 = radius * radius;
  let size = vec2f(textureDimensions(depthTex));
  // Eq. 5: S' in full-size pixels, the radius kept to a fifth of the screen up close.
  let sp = size.y / max(p.frustum.z - p.frustum.w, 1e-4);
  let rp = min(radius * sp / c4.w, size.y * 0.2);
  // Eq. 8, as the reference code groups it: (3x ^ (y + x y)) 10.
  let ux = u32(cell.x);
  let uy = u32(cell.y);
  let phi = (f32((3u * ux) ^ (uy + ux * uy)) * 10.0) % 6.2831853;
  var sum = 0.0;
  const S = 9;
  const TAU = 7.0;
  for (var i = 0; i < S; i++) {
    let alpha = (f32(i) + 0.5) / f32(S);
    let h = rp * alpha;
    let theta = 6.2831853 * alpha * TAU + phi;
    let off = vec2i(round(h * vec2f(cos(theta), sin(theta))));
    let s4 = pos_at(q + off);
    if (s4.w <= 0.0) {
      continue;
    }
    let v = s4.xyz - c;
    let vv = dot(v, v);
    if (vv < r2) {
      let elevation = dot(v, n) / max(sqrt(vv), 1e-4);
      sum += max(0.0, elevation - p.ao.z) * (1.0 - vv / r2);
    }
  }
  let a = max(0.0, 1.0 - p.ao.y * sum / (f32(S) * max(1.0 - p.ao.z, 1e-3)));
  return vec4f(a, c4.w, 0.0, 0.0);
}

// Section 2.4's bilateral pass along `dir`: 7 taps, 2 texels apart, Gaussian weights cut where the depth
// differs by more than a twentieth of its own.
fn blur(at: vec2i, dir: vec2i) -> vec4f {
  let size = vec2i(textureDimensions(src));
  let centre = textureLoad(src, at, 0);
  if (centre.y <= 0.0) {
    return centre;
  }
  var weights = array<f32, 4>(1.0, 0.8825, 0.6065, 0.3247);
  var sum = centre.x;
  var total = 1.0;
  for (var k = 1; k <= 3; k++) {
    for (var side = -1; side <= 1; side += 2) {
      let t = textureLoad(src, clamp(at + dir * (k * side * 2), vec2i(0), size - vec2i(1)), 0);
      if (t.y > 0.0) {
        let w = weights[k] * max(0.0, 1.0 - 20.0 * abs(t.y - centre.y) / centre.y);
        sum += t.x * w;
        total += w;
      }
    }
  }
  return vec4f(sum / total, centre.y, 0.0, 0.0);
}

@fragment
fn fs_blur_h(in: VertexOutput) -> @location(0) vec4f {
  return blur(vec2i(floor(in.pos.xy)), vec2i(1, 0));
}

@fragment
fn fs_blur_v(in: VertexOutput) -> @location(0) vec4f {
  return blur(vec2i(floor(in.pos.xy)), vec2i(0, 1));
}
)";

// The composite, in a module of its own: src is the frame copy, aoTex the smoothed obscurance.
constexpr const char* CompositeSource = R"(
@group(0) @binding(3) var aoTex: texture_2d<f32>;

fn srgb_enc(c: vec3f) -> vec3f {
  let l = clamp(c, vec3f(0.0), vec3f(1.0));
  return select(1.055 * pow(l, vec3f(1.0 / 2.4)) - 0.055, 12.92 * l, l <= vec3f(0.0031308));
}

fn srgb_dec(c: vec3f) -> vec3f {
  let e = clamp(c, vec3f(0.0), vec3f(1.0));
  return select(pow((e + 0.055) / 1.055, vec3f(2.4)), e / 12.92, e <= vec3f(0.04045));
}

// How much of a pixel at window z the GX fog colour makes up, as lib/gx/shader.cpp's fog computes it
// (without the range adjustment, which the game leaves off).
fn fog_amount(z: f32) -> f32 {
  if ((p.fog_kind & 7u) == 0u) {
    return 0.0; // GX_FOG_NONE
  }
  var base: f32;
  if ((p.fog_kind & 8u) != 0u) {
    base = p.fog_a * z;
  } else {
    base = p.fog_a / (p.fog_b - z);
  }
  let f = clamp(base - p.fog_c, 0.0, 1.0);
  var fz = f; // GX_FOG_PERSP_LIN, GX_FOG_ORTHO_LIN
  switch p.fog_kind & 7u {
    case 4u: { fz = 1.0 - exp2(-8.0 * f); }
    case 5u: { fz = 1.0 - exp2(-8.0 * f * f); }
    case 6u: { fz = exp2(-8.0 * (1.0 - f)); }
    case 7u: { fz = exp2(-8.0 * (1.0 - f) * (1.0 - f)); }
    default: {}
  }
  return clamp(fz, 0.0, 1.0);
}

@fragment
fn fs_composite(in: VertexOutput) -> @location(0) vec4f {
  let at = vec2i(floor(in.pos.xy));
  let fsize = vec2i(textureDimensions(src));
  let f = textureLoad(src, min(at, fsize - vec2i(1)), 0);
  let depth = view_depth(at);
  if (depth <= 0.0) {
    return f;
  }
  // The four half-size texels around the pixel (texel j sits on full-size pixel 2 j), bilinear weights
  // times how close their depth is to this pixel's.
  let asize = vec2i(textureDimensions(aoTex));
  let base = at / 2;
  let fr = vec2f(at - base * 2) * 0.5;
  var sum = 0.0;
  var total = 0.0;
  for (var j = 0; j < 4; j++) {
    let o = vec2i(j & 1, j >> 1u);
    let t = textureLoad(aoTex, clamp(base + o, vec2i(0), asize - vec2i(1)), 0);
    let bx = select(1.0 - fr.x, fr.x, o.x == 1);
    let by = select(1.0 - fr.y, fr.y, o.y == 1);
    let w = bx * by * select(0.0, 1.0 / (1e-3 + abs(t.y - depth) / depth), t.y > 0.0);
    sum += t.x * w;
    total += w;
  }
  let a = select(1.0, sum / total, total > 0.0);
  let fade = 1.0 - clamp((depth - p.ao.w) / max(p.misc.x - p.ao.w, 1e-3), 0.0, 1.0);
  let vis = mix(1.0, a, fade);
  // The frame is surface (1 - fz) + fog colour fz: only the surface is darkened.
  let fz = fog_amount(window_z(at));
  if (fz >= 0.999) {
    return f;
  }
  let fog = clamp(p.fog_color.rgb, vec3f(0.0), vec3f(1.0));
  let surface = clamp((f.rgb - fog * fz) / (1.0 - fz), vec3f(0.0), vec3f(1.0));
  return vec4f(mix(srgb_enc(srgb_dec(surface) * vis), fog, fz), f.a);
}
)";

struct State {
  EncoderTaskId task = InvalidEncoderTask;
  wgpu::BindGroupLayout layout;
  wgpu::RenderPipeline ao;
  wgpu::RenderPipeline blurH;
  wgpu::RenderPipeline blurV;
  wgpu::RenderPipeline composite;
  wgpu::TextureFormat format = wgpu::TextureFormat::Undefined;
  uint32_t samples = 0;
  std::array<wgpu::Buffer, RecordedSlots> uniforms;
  // The frame as it was, and the half-size obscurance and its blur target.
  wgpu::Texture frame;
  wgpu::TextureView frameView;
  std::array<wgpu::Texture, 2> aoTex;
  std::array<wgpu::TextureView, 2> aoView;
  uint32_t width = 0;
  uint32_t height = 0;
  wgpu::TextureFormat frameFormat = wgpu::TextureFormat::Undefined;
  // Bound where a pass reads nothing: a 1x1 texture of the right type.
  wgpu::TextureView dummy;
};
State g_state;
// The task's uniform: the command's words, then the fog.
struct Uniforms {
  Params params;
  Fog fog;
};
static_assert(sizeof(Uniforms) == 96);
std::array<Uniforms, RecordedSlots> g_recorded;
uint32_t g_nextSlot = 0;

wgpu::ShaderModule make_module(const char* label, const std::string& code) {
  wgpu::ShaderSourceWGSL source{};
  source.code = code.c_str();
  const wgpu::ShaderModuleDescriptor descriptor{.nextInChain = &source, .label = label};
  return g_device.CreateShaderModule(&descriptor);
}

wgpu::Texture make_texture(const char* label, uint32_t width, uint32_t height, wgpu::TextureFormat format,
                           wgpu::TextureUsage usage) {
  const wgpu::TextureDescriptor descriptor{
      .label = label,
      .usage = usage,
      .size = {width, height, 1},
      .format = format,
  };
  AURORA_NOTE_TEXTURE(&descriptor);
  return g_device.CreateTexture(&descriptor);
}

void ensure_pipelines(wgpu::TextureFormat format, uint32_t samples) {
  if (g_state.ao && g_state.format == format && g_state.samples == samples) {
    return;
  }
  const std::string code = shader_source(samples);
  const auto module = make_module("Ambient Occlusion", code);
  const std::array entries{
      wgpu::BindGroupLayoutEntry{
          .binding = 0,
          .visibility = wgpu::ShaderStage::Fragment,
          .buffer = {.type = wgpu::BufferBindingType::Uniform, .minBindingSize = sizeof(Uniforms)},
      },
      wgpu::BindGroupLayoutEntry{
          .binding = 1,
          .visibility = wgpu::ShaderStage::Fragment,
          .texture = {.sampleType = wgpu::TextureSampleType::Depth,
                      .viewDimension = wgpu::TextureViewDimension::e2D,
                      .multisampled = samples > 1},
      },
      wgpu::BindGroupLayoutEntry{
          .binding = 2,
          .visibility = wgpu::ShaderStage::Fragment,
          .texture = {.sampleType = wgpu::TextureSampleType::UnfilterableFloat,
                      .viewDimension = wgpu::TextureViewDimension::e2D},
      },
      wgpu::BindGroupLayoutEntry{
          .binding = 3,
          .visibility = wgpu::ShaderStage::Fragment,
          .texture = {.sampleType = wgpu::TextureSampleType::UnfilterableFloat,
                      .viewDimension = wgpu::TextureViewDimension::e2D},
      },
  };
  const wgpu::BindGroupLayoutDescriptor layoutDescriptor{
      .label = "Ambient Occlusion Layout",
      .entryCount = entries.size(),
      .entries = entries.data(),
  };
  g_state.layout = g_device.CreateBindGroupLayout(&layoutDescriptor);
  const wgpu::PipelineLayoutDescriptor pipelineLayoutDescriptor{
      .label = "Ambient Occlusion Pipeline Layout",
      .bindGroupLayoutCount = 1,
      .bindGroupLayouts = &g_state.layout,
  };
  const auto pipelineLayout = g_device.CreatePipelineLayout(&pipelineLayoutDescriptor);
  const auto make = [&](const char* label, const char* entry, wgpu::TextureFormat target, uint32_t count) {
    const wgpu::ColorTargetState colorTarget{.format = target, .writeMask = wgpu::ColorWriteMask::All};
    const wgpu::FragmentState fragment{
        .module = module,
        .entryPoint = entry,
        .targetCount = 1,
        .targets = &colorTarget,
    };
    const wgpu::RenderPipelineDescriptor descriptor{
        .label = label,
        .layout = pipelineLayout,
        .vertex = {.module = module, .entryPoint = "vs_main"},
        .primitive = {.topology = wgpu::PrimitiveTopology::TriangleList},
        .multisample = {.count = count, .mask = UINT32_MAX},
        .fragment = &fragment,
    };
    return g_device.CreateRenderPipeline(&descriptor);
  };
  g_state.ao = make("Ambient Occlusion", "fs_ao", AoFormat, 1);
  g_state.blurH = make("Ambient Occlusion Blur H", "fs_blur_h", AoFormat, 1);
  g_state.blurV = make("Ambient Occlusion Blur V", "fs_blur_v", AoFormat, 1);
  g_state.composite = make("Ambient Occlusion Composite", "fs_composite", format, samples);
  g_state.format = format;
  g_state.samples = samples;
}

void ensure_targets(uint32_t width, uint32_t height, wgpu::TextureFormat format) {
  if (g_state.frame && g_state.width == width && g_state.height == height && g_state.frameFormat == format) {
    return;
  }
  g_state.width = width;
  g_state.height = height;
  g_state.frameFormat = format;
  g_state.frame = make_texture("Ambient Occlusion Frame Copy", width, height, format,
                               wgpu::TextureUsage::TextureBinding | wgpu::TextureUsage::CopyDst);
  g_state.frameView = g_state.frame.CreateView();
  const uint32_t aoWidth = (width + 1) / 2;
  const uint32_t aoHeight = (height + 1) / 2;
  for (size_t i = 0; i < g_state.aoTex.size(); ++i) {
    g_state.aoTex[i] = make_texture("Ambient Occlusion", aoWidth, aoHeight, AoFormat,
                                    wgpu::TextureUsage::TextureBinding | wgpu::TextureUsage::RenderAttachment);
    g_state.aoView[i] = g_state.aoTex[i].CreateView();
  }
}

void ensure_shared() {
  if (g_state.uniforms[0]) {
    return;
  }
  const wgpu::BufferDescriptor bufferDescriptor{
      .label = "Ambient Occlusion Uniforms",
      .usage = wgpu::BufferUsage::Uniform | wgpu::BufferUsage::CopyDst,
      .size = sizeof(Uniforms),
  };
  for (auto& uniforms : g_state.uniforms) {
    uniforms = g_device.CreateBuffer(&bufferDescriptor);
  }
  g_state.dummy = make_texture("Ambient Occlusion Placeholder", 1, 1, AoFormat, wgpu::TextureUsage::TextureBinding)
                      .CreateView();
}

void draw(const wgpu::CommandEncoder& cmd, const char* label, const wgpu::RenderPipeline& pipeline,
          const wgpu::BindGroup& group, const wgpu::TextureView& target, const wgpu::TextureView& resolve) {
  // Every pixel is written without blending, so nothing need be loaded first.
  const wgpu::RenderPassColorAttachment attachment{
      .view = target,
      .resolveTarget = resolve,
      .loadOp = wgpu::LoadOp::Clear,
      .storeOp = wgpu::StoreOp::Store,
      .clearValue = {1.0, 0.0, 0.0, 0.0},
  };
  const wgpu::RenderPassDescriptor passDescriptor{
      .label = label,
      .colorAttachmentCount = 1,
      .colorAttachments = &attachment,
      .timestampWrites = webgpu::gpu_prof::pass_writes(label),
  };
  const auto pass = cmd.BeginRenderPass(&passDescriptor);
  pass.SetPipeline(pipeline);
  pass.SetBindGroup(0, group);
  pass.Draw(3);
  pass.End();
}

void encode(const EncoderTaskContext& ctx, const wgpu::CommandEncoder& cmd, const void* payload, size_t payloadSize,
            void*) {
  uint32_t slot = 0;
  if (payloadSize != sizeof(slot)) {
    return;
  }
  std::memcpy(&slot, payload, sizeof(slot));
  const Uniforms& params = g_recorded[slot % g_recorded.size()];
  const auto efb = webgpu::efb_targets();
  const auto& source = webgpu::present_source();
  const uint32_t samples = webgpu::g_graphicsConfig.msaaSamples > 1 ? webgpu::g_graphicsConfig.msaaSamples : 1;
  const auto format = webgpu::g_graphicsConfig.surfaceConfiguration.format;
  const uint32_t width = source.size.width;
  const uint32_t height = source.size.height;
  if (width == 0 || height == 0 || !source.texture || !efb.color.view || !efb.depth.view ||
      efb.depth.size.width != width || efb.depth.size.height != height) {
    return;
  }
  ensure_shared();
  ensure_pipelines(format, samples);
  ensure_targets(width, height, format);
  const wgpu::Buffer& uniforms = g_state.uniforms[slot % g_state.uniforms.size()];
  ctx.queue.WriteBuffer(uniforms, 0, &params, sizeof(params));

  const wgpu::TexelCopyTextureInfo copySource{.texture = source.texture};
  const wgpu::TexelCopyTextureInfo copyTarget{.texture = g_state.frame};
  const wgpu::Extent3D copySize{width, height, 1};
  cmd.CopyTextureToTexture(&copySource, &copyTarget, &copySize);

  const auto group = [&](const wgpu::TextureView& srcView, const wgpu::TextureView& aoView) {
    const std::array entries{
        wgpu::BindGroupEntry{.binding = 0, .buffer = uniforms, .size = sizeof(Uniforms)},
        wgpu::BindGroupEntry{.binding = 1, .textureView = efb.depth.view},
        wgpu::BindGroupEntry{.binding = 2, .textureView = srcView},
        wgpu::BindGroupEntry{.binding = 3, .textureView = aoView},
    };
    const wgpu::BindGroupDescriptor descriptor{
        .label = "Ambient Occlusion",
        .layout = g_state.layout,
        .entryCount = entries.size(),
        .entries = entries.data(),
    };
    return g_device.CreateBindGroup(&descriptor);
  };
  draw(cmd, "Ambient occlusion", g_state.ao, group(g_state.dummy, g_state.dummy), g_state.aoView[0], {});
  draw(cmd, "Ambient occlusion blur", g_state.blurH, group(g_state.aoView[0], g_state.dummy), g_state.aoView[1], {});
  draw(cmd, "Ambient occlusion blur", g_state.blurV, group(g_state.aoView[1], g_state.dummy), g_state.aoView[0], {});
  draw(cmd, "Ambient occlusion composite", g_state.composite, group(g_state.frameView, g_state.aoView[0]),
       efb.color.view, samples > 1 ? efb.resolved.view : wgpu::TextureView{});
}
} // namespace

bool ensure_task() {
  // The passes read the depth buffer with textureLoad, which Compatibility devices reject.
  if (!webgpu::g_hasCoreFeatures) {
    return false;
  }
  if (g_state.task == InvalidEncoderTask) {
    g_state.task = register_encoder_task_type(EncoderTaskDescriptor{.label = "Ambient Occlusion", .callback = encode});
    if (g_state.task == InvalidEncoderTask) {
      Log.warn("could not register the ambient occlusion task");
      return false;
    }
  }
  return true;
}

bool record(const Params& params, const Fog& fog) {
  if (g_state.task == InvalidEncoderTask) {
    return false;
  }
  // The display EFB only: the HDR scene's linear targets are not what the composite encodes.
  if (scene_hdr_active()) {
    return false;
  }
  const auto& size = webgpu::present_source().size;
  if (size.width == 0 || size.height == 0) {
    return false;
  }
  const uint32_t slot = g_nextSlot++ % g_recorded.size();
  g_recorded[slot] = {params, fog};
  return record_encoder_task(g_state.task, &slot, sizeof(slot));
}

std::string shader_source(uint32_t samples) {
  std::string code = std::string(Source) + CompositeSource;
  const std::string token = "DEPTH_TYPE";
  code.replace(code.find(token), token.size(), samples > 1 ? "texture_depth_multisampled_2d" : "texture_depth_2d");
  return code;
}

void shutdown() {
  const auto task = g_state.task;
  g_state = {};
  if (task != InvalidEncoderTask) {
    unregister_encoder_task_type(task);
  }
}
} // namespace aurora::gfx::ssao
