// The F1 > Video rows the Prime 1 port has and Aurora now backs (f1b): see include/port_video.h.
// Ported from MP1's platform/debug_ui/options.cpp (dynamic resolution, sharpening, the driver
// workarounds and storage clamp), ui_common.cpp (the GPU self-test) and main.cpp (the OpenGL ES
// crash marker); the settings are PortOverlay's.

#include "port_utf8_path.h"
#include "port_video.h"

#include "port_dynamic_res.h"
#include "port_env.h"
#include "port_gpu_driver.h"
#include "port_overlay.h"
#include "port_overlay_internal.h"
#include "port_photo.h"

#include "Kyoto/Graphics/CGX.hpp"
#include "Kyoto/Graphics/CGraphics.hpp"

#include <SDL3/SDL.h>
#include <aurora/aurora.h>
#include <aurora/gfx.h>
#include <dolphin/gx.h>
#include <dolphin/gx/GXExtra.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

using namespace PortOverlayState;

namespace aurora {
bool frame_buffer_size_for_scale(float scale, uint32_t* width, uint32_t* height) noexcept;
} // namespace aurora

namespace PortVideo {

namespace {

// Dynamic resolution (port_dynamic_res.h).
PortDynamicRes::Controller sDynController;
float sDynScale = 0.f;   // the scale in use; 0 = PortOverlay::RenderScale()
double sDynGpuMs = -1.0; // the last GPU time it was given, for the Quality tab
bool sDynActive = false;
bool sGpuTimesOn = false;
// The presented rate, once a second, on wall-clock time.
uint64_t sTimingStartNs = 0;
unsigned sTimingFrames = 0;
unsigned sFramesSeen = 0;

// BeginGpuStart's findings.
bool sDriverChecksThisRun = false;
const char* sDriverWorkaroundsEnv = nullptr;
bool sDriverWorkaroundsCleared = false;
// A file in the user folder while a start with driver workarounds is under way: still there at the
// next start means that one never reached its 60th frame, the same kind of marker kept for
// OpenGL ES and OpenGL below.
std::filesystem::path sDriverWorkaroundsMarker;
std::filesystem::path sGlesMarker;
bool sGlesStarting = false;
// The same for a custom Vulkan driver (port_gpu_driver.h), and MP2_GPU_DRIVER when it overrides
// the setting.
std::filesystem::path sGpuDriverMarker;
const char* sGpuDriverEnv = nullptr;

std::atomic< bool > sGpuSelfTestRequested{false};

float EffectiveRenderScale() { return PortOverlay::RenderScale(); }

void WantGpuTimes(bool on) {
  if (on != sGpuTimesOn) {
    sGpuTimesOn = on;
    GXPortSetGpuTimes(on ? GX_TRUE : GX_FALSE);
  }
}

// Once a second, from the presented rate and, where the GPU can time itself, the GPU's time per
// frame (gpu_prof's span). The controller (port_dynamic_res.cpp) decides; each change reallocates
// the EFB (through ApplyRenderScale) and stutters for a frame.
void UpdateDynamicRes(double fps, unsigned frames) {
  // Photo mode's capture holds its own scale.
  if (PortPhoto::Capturing()) {
    return;
  }
  const float top = DynamicResTop();
  const float bottom = std::min(PortOverlay::DynamicResMin(), top);
  const bool active = PortOverlay::DynamicRes() && top > bottom;
  // Auto's top moves with the window: start over from the new one.
  static float sDynTop = 0.f;
  const bool topMoved = sDynTop != top;
  sDynTop = top;
  // MP2_DYNRES_GPU_TIMES=0 steers on the frame rate alone, as on a GPU without timestamps.
  static const bool gpuTimesAllowed = port::EnvFlag("MP2_DYNRES_GPU_TIMES", true);
  const bool gpuTimes = gpuTimesAllowed && GXPortGpuTimesSupported() == GX_TRUE;
  WantGpuTimes(active && gpuTimes);
  if (!active) {
    if (sDynActive) {
      sDynActive = false;
      ResetDynamicRes();
    }
    return;
  }
  if (!sDynActive || topMoved) {
    sDynActive = true;
    ResetDynamicRes();
  }
  PortDynamicRes::Sample sample;
  sample.fps = fps;
  sample.frames = frames;
  sample.targetFps = DynamicResTargetFps();
  AuroraPipelineStats pipelines{};
  aurora_get_pipeline_stats(&pipelines);
  sample.compiling = pipelines.pending > 0;
  sample.top = top;
  sample.bottom = bottom;
  if (gpuTimes) {
    float total = 0.f;
    float span = 0.f;
    GXPortGetGpuTimes(nullptr, 0, &total, &span);
    sample.gpuMs = span;
    sample.gpuPassMs = span > 0.f ? total : -1.0;
  }
  sDynGpuMs = sample.gpuMs;
  const PortDynamicRes::Decision decision = sDynController.Update(sample);
  if (decision.changed) {
    // Back at the top draws as the setting does (auto: the window's exact size).
    sDynScale = decision.scale < top ? decision.scale : 0.f;
    ApplyPresentSharpness();
    std::fprintf(stderr, "[video] dynamic resolution %.2fx (%.0f fps, target %.0f, GPU %.1f ms): %s\n",
                 static_cast< double >(decision.scale), fps, sample.targetFps, sample.gpuMs,
                 PortDynamicRes::ReasonText(decision.reason));
  }
}

void DriverWorkaroundsStarted() {
  if (!sDriverWorkaroundsMarker.empty()) {
    std::error_code ec;
    std::filesystem::remove(sDriverWorkaroundsMarker, ec);
    sDriverWorkaroundsMarker.clear();
  }
}

// The test leaves its own GX state behind: drop the game's cached copy of it, and put back the copy
// clear colour its efb-clear-copy case sets (the game sets it rarely, so it isn't re-sent every frame).
void RestoreGxAfterGpuSelfTest() {
  CGX::ResetGXStatesFull();
  CGraphics::SetViewMatrix();
  CGraphics::SetClearColor(CGraphics::GetClearColor());
}

} // namespace

unsigned ParseDriverWorkarounds(std::string_view rest) {
  unsigned bits = 0;
  while (!rest.empty()) {
    const size_t comma = rest.find(',');
    const std::string_view name = rest.substr(0, comma);
    for (size_t i = 0; i < kDriverWorkaroundCount; ++i) {
      if (name == kDriverWorkarounds[i].key) {
        bits |= 1u << i;
      }
    }
    rest = comma == std::string_view::npos ? std::string_view{} : rest.substr(comma + 1);
  }
  return bits;
}

void BeginGpuStart(const std::string& userFolder, AuroraConfig& config) {
  const std::filesystem::path folder = userFolder.empty() ? std::filesystem::path(".") : PortUtf8::FromUtf8(userFolder);
  std::error_code ec;
  sOpenGlesAtStart = sOpenGles;
  sStorageClampAtStart = sStorageClamp;
  sLowPowerGpuAtStart = sLowPowerGpu;

#if defined(_WIN32) || (defined(__linux__) && !defined(__ANDROID__))
  // The renderer choice: Aurora tries it first and falls back to its own order. Read here, so a
  // change applies after a restart. MP2_GRAPHICS_BACKEND overrides the setting for one run.
  sGraphicsBackendAtStart = sGraphicsBackend;
  {
    const char* const env = PortGraphicsBackend::EnvRequest();
    const PortGraphicsBackend::Request request = PortGraphicsBackend::Resolve(sGraphicsBackend, env);
    if (request.rejected) {
      // The names this build offers, so the warning can't suggest one it can't start.
      std::string names;
      for (const PortGraphicsBackend::EGraphicsBackend choice : PortGraphicsBackend::Choices()) {
        names += names.empty() ? "" : ", ";
        names += PortGraphicsBackend::GraphicsBackendConfigName(choice);
      }
      std::fprintf(stderr, "[video] MP2_GRAPHICS_BACKEND=%s is not one of %s; using graphics_backend=%s\n", env,
                   names.c_str(), PortGraphicsBackend::GraphicsBackendConfigName(request.backend));
    }
    config.desiredBackend = PortGraphicsBackend::ToAurora(request.backend);
  }
#endif

  // A driver can crash outright while Dawn starts on OpenGL ES or OpenGL (Mesa does), and the
  // setting would then crash every launch with no way back to the F1 menu. The marker outlives such
  // a crash, so the next start turns the setting off (OpenGL: back to Auto) and uses the default
  // backend. OpenGL ES, where offered, wins over the renderer choice.
  if (PortOverlay::OpenGles()) {
    config.desiredBackend = BACKEND_OPENGLES;
  }
  if (config.desiredBackend == BACKEND_OPENGLES || config.desiredBackend == BACKEND_OPENGL) {
    const bool gles = config.desiredBackend == BACKEND_OPENGLES;
    sGlesMarker = folder / (gles ? "opengles_starting" : "opengl_starting");
    if (std::filesystem::exists(sGlesMarker, ec)) {
      if (gles) {
        std::fprintf(stderr, "[video] the last start on OpenGL ES did not finish; turning it off\n");
        sOpenGles = false;
        sOpenGlesAtStart = false;
      } else {
        std::fprintf(stderr, "[video] the last start on OpenGL did not finish; using Auto\n");
        sGraphicsBackend = PortGraphicsBackend::kGraphicsBackend_Auto;
        sGraphicsBackendAtStart = sGraphicsBackend;
      }
      PortOverlaySettings::MarkDirty();
      config.desiredBackend = BACKEND_AUTO;
      std::filesystem::remove(sGlesMarker, ec);
    } else {
      std::ofstream(sGlesMarker) << "1\n";
      sGlesStarting = true;
    }
  }

  // A custom Vulkan driver (Turnip, Android only) can crash while it starts, just like OpenGL ES
  // above; the same kind of marker sends the next start back to the system driver. MP1's main.cpp
  // block. MP2_GPU_DRIVER=<id> (empty = the system's) overrides the setting for one run.
  sGpuDriverAtStart = sGpuDriver;
  sGpuDriverMarker = folder / "gpu_driver_starting";
  sGpuDriverEnv = port::EnvString("MP2_GPU_DRIVER");
  {
    const std::string driver = sGpuDriverEnv != nullptr ? sGpuDriverEnv : sGpuDriver;
    if (!driver.empty() && config.desiredBackend != BACKEND_OPENGLES && PortGpuDriver::Supported()) {
      if (std::filesystem::exists(sGpuDriverMarker, ec)) {
        std::fprintf(stderr, "[video] the last start with GPU driver %s crashed; using the system driver\n",
                     driver.c_str());
        sGpuDriver.clear();
        PortOverlaySettings::MarkDirty();
        PortGpuDriver::SetLoadError("it crashed last time; switched back to the system driver");
        std::filesystem::remove(sGpuDriverMarker, ec);
      } else {
        std::ofstream(sGpuDriverMarker) << driver << '\n';
        static std::string vulkanLibraryDir;
        vulkanLibraryDir = PortGpuDriver::Prepare(driver);
        config.vulkanLibraryDir = vulkanLibraryDir.empty() ? nullptr : vulkanLibraryDir.c_str();
      }
    } else {
      // Left by a crash before the user switched back to System: don't hold it against the next driver.
      std::filesystem::remove(sGpuDriverMarker, ec);
    }
  }

  // The Adreno storage clamp: Aurora reads MP2_STORAGE_CLAMP, so a user's own variable wins.
  if (sStorageClamp >= 0 && port::EnvString("MP2_STORAGE_CLAMP") == nullptr) {
    port::SetEnv("MP2_STORAGE_CLAMP", sStorageClamp > 0 ? "1" : "0");
  }
  // The low-power adapter: Aurora reads MP2_GPU_POWER=low.
  if (sLowPowerGpu && port::EnvString("MP2_GPU_POWER") == nullptr) {
    port::SetEnv("MP2_GPU_POWER", "low");
  }

  // Driver workarounds.
  sDriverWorkaroundsMarker = folder / "driver_workarounds_starting";
  unsigned bits = sDriverWorkarounds;
  bool keepMarker = false;
  sDriverWorkaroundsEnv = port::EnvString("MP2_DRIVER_WORKAROUNDS");
  if (sDriverWorkaroundsEnv != nullptr) {
    // "0" (or anything that names none) starts without them, over the ticked boxes.
    bits = ParseDriverWorkarounds(sDriverWorkaroundsEnv);
    std::fprintf(stderr, "[video] MP2_DRIVER_WORKAROUNDS=%s replaces the ticked driver workarounds this run\n",
                 sDriverWorkaroundsEnv);
  } else if (bits != 0 && std::filesystem::exists(sDriverWorkaroundsMarker, ec)) {
    std::fprintf(stderr, "[video] the last start with driver workarounds did not reach the game; turning them off\n");
    bits = 0;
    sDriverWorkarounds = 0;
    sDriverWorkaroundsCleared = true;
    PortOverlaySettings::MarkDirty();
    // Until a start reaches the game and saves the cleared setting, a crash before that (for any
    // reason) must not bring the boxes back: the marker stays until DriverWorkaroundsStarted.
    keepMarker = true;
  }
  if (sDriverWorkaroundsEnv == nullptr && (bits & kDriverChecksBit) != 0) {
    // One start only: untick it now, so the next start is back to full speed whatever happens.
    sDriverWorkarounds &= ~kDriverChecksBit;
    PortOverlaySettings::MarkDirty();
    std::fprintf(stderr, "[video] driver error checks are on for this run (slower); the next start turns them off\n");
  }
  sDriverChecksThisRun = (bits & kDriverChecksBit) != 0;
  sDriverWorkaroundsAtStart = sDriverWorkarounds;
  static std::string enable;
  static std::string disable;
  enable.clear();
  disable.clear();
  const auto append = [](std::string& list, const char* toggles) {
    if (toggles != nullptr) {
      list += list.empty() ? "" : ",";
      list += toggles;
    }
  };
  for (size_t i = 0; i < kDriverWorkaroundCount; ++i) {
    if (bits & (1u << i)) {
      append(enable, kDriverWorkarounds[i].enable);
      append(disable, kDriverWorkarounds[i].disable);
      if (const char* var = kDriverWorkarounds[i].env; var != nullptr && port::EnvString(var) == nullptr) {
        port::SetEnv(var, "1");
        std::fprintf(stderr, "[video] driver workaround %s sets %s=1\n", kDriverWorkarounds[i].key, var);
      }
    }
  }
  // A run on MP2_DRIVER_WORKAROUNDS says nothing about the ticked boxes: it leaves the marker alone.
  if (sDriverWorkaroundsEnv != nullptr) {
    sDriverWorkaroundsMarker.clear();
  } else if (bits != 0) {
    std::ofstream(sDriverWorkaroundsMarker) << "1\n";
  } else if (!keepMarker) {
    std::filesystem::remove(sDriverWorkaroundsMarker, ec);
    sDriverWorkaroundsMarker.clear();
  }
  config.dawnEnableToggles = enable.empty() ? nullptr : enable.c_str();
  config.dawnDisableToggles = disable.empty() ? nullptr : disable.c_str();
  if (!enable.empty() || !disable.empty()) {
    std::fprintf(stderr, "[video] driver workarounds: enable '%s' disable '%s'\n", enable.c_str(), disable.c_str());
  }
}

void GpuStarted() {
  std::error_code ec;
  if (sGlesStarting) {
    std::filesystem::remove(sGlesMarker, ec);
    sGlesStarting = false;
  }
  // Aurora went back to the system driver when Vulkan failed with the custom one.
  if (aurora_vulkan_library_failed() && !PortGpuDriver::Active().empty()) {
    std::fprintf(stderr, "[video] Vulkan failed with GPU driver %s; using the system driver\n",
                 PortGpuDriver::Active().c_str());
    if (sGpuDriverEnv == nullptr) {
      sGpuDriver.clear();
      PortOverlaySettings::MarkDirty();
    }
    PortGpuDriver::SetLoadError("Vulkan failed to start with it; switched back to the system driver");
  }
  // The driver started: only a crash before this point reverts it.
  if (!sGpuDriverMarker.empty()) {
    std::filesystem::remove(sGpuDriverMarker, ec);
  }
}

bool DriverChecksThisRun() { return sDriverChecksThisRun; }
const char* DriverWorkaroundsEnv() { return sDriverWorkaroundsEnv; }
bool DriverWorkaroundsCleared() { return sDriverWorkaroundsCleared; }

void Frame() {
  ApplyPresentSharpness();
  {
    // Sent only on change: AuroraConfig has no field for it.
    static int sAppliedPerPixel = -1;
    const int perPixel = PortOverlay::PerPixelLighting() ? 1 : 0;
    if (perPixel != sAppliedPerPixel) {
      sAppliedPerPixel = perPixel;
      aurora_set_per_pixel_lighting(perPixel != 0);
    }
    static int sAppliedReflectionBlur = -1;
    static int sAppliedReflectionStrength = -1;
    const int reflectionBlur = PortOverlay::CannonReflectionBlur();
    const int reflectionStrength = PortOverlay::CannonReflectionStrength();
    if (reflectionBlur != sAppliedReflectionBlur || reflectionStrength != sAppliedReflectionStrength) {
      sAppliedReflectionBlur = reflectionBlur;
      sAppliedReflectionStrength = reflectionStrength;
      // Blur 0..100% over the probe's levels 0..5 (Aurora's gfx::probe::MipCount is 6).
      aurora_set_env_probe_look(static_cast< float >(reflectionBlur) * 0.05f,
                                static_cast< float >(reflectionStrength) / 100.f);
    }
    static int sAppliedPbrHighlight = -1;
    const int pbrHighlight = PortOverlay::PbrHighlight();
    if (pbrHighlight != sAppliedPbrHighlight) {
      sAppliedPbrHighlight = pbrHighlight;
      aurora_set_pbr_highlight(static_cast< float >(pbrHighlight) / 100.f);
    }
  }
  if (++sFramesSeen == 60) {
    DriverWorkaroundsStarted();
  }
  const uint64_t now = SDL_GetTicksNS();
  if (sTimingStartNs == 0) {
    sTimingStartNs = now;
    return;
  }
  ++sTimingFrames;
  const uint64_t elapsed = now - sTimingStartNs;
  if (elapsed >= 1000000000ull) {
    const double fps = static_cast< double >(sTimingFrames) / (static_cast< double >(elapsed) / 1e9);
    UpdateDynamicRes(fps, sTimingFrames);
    sTimingFrames = 0;
    sTimingStartNs = now;
  }
}

// What 1x draws and what auto draws, as the frame buffer's height: auto is the window's pixel
// size fitted to the game's aspect, so its scale is the ratio of the two.
float BaseRenderScale() {
  const float scale = PortOverlay::RenderScale();
  if (scale > 0.f) {
    return scale;
  }
  uint32_t width = 0, native = 0, base = 0;
  if (aurora::frame_buffer_size_for_scale(0.f, &width, &native) &&
      aurora::frame_buffer_size_for_scale(1.f, &width, &base) && native > 0 && base > 0) {
    return static_cast< float >(native) / static_cast< float >(base);
  }
  return 1.f;
}

// The most dynamic resolution draws at: the scale set, or with auto render scale the window's own.
float DynamicResTop() { return BaseRenderScale(); }

int DrawnLines(float scale) {
  uint32_t width = 0, height = 0;
  if (aurora::frame_buffer_size_for_scale(scale, &width, &height)) {
    return static_cast< int >(height);
  }
  return static_cast< int >(std::lround(scale * 480.f));
}

float DrawScale() { return sDynScale > 0.f ? sDynScale : PortOverlay::RenderScale(); }

float DynamicScale() { return sDynScale; }

double DynamicResTargetFps() {
  double target = PortOverlay::DynamicResTarget();
  if (target <= 0.0) {
    const SDL_DisplayMode* mode = SDL_GetCurrentDisplayMode(SDL_GetPrimaryDisplay());
    target = mode != nullptr && mode->refresh_rate > 0.f ? mode->refresh_rate : 60.0;
  }
  const int cap = PortOverlay::FrameLimit() ? 60 : PortOverlay::FpsCap();
  return cap > 0 ? std::min(target, static_cast< double >(cap)) : target;
}

double DynamicResGpuMs() { return sDynGpuMs; }

bool DynamicResGpuTimesDropped() { return sDynController.GpuTimesDropped(); }

const char* DynamicResReason() { return PortDynamicRes::ReasonText(sDynController.LastReason()); }

void ResetDynamicRes() {
  sDynController.Reset(DynamicResTop());
  sDynGpuMs = -1.0;
  sDynScale = 0.f;
  ApplyPresentSharpness();
}

// Sharpening for a frame stretched up to the window (Aurora's present resample); only passed on
// when it changes.
void ApplyPresentSharpness() {
  float amount = 0.f;
  const int mode = PortOverlay::UpscaleSharpen();
  const int strength = PortOverlay::UpscaleSharpenAmount();
  if (strength > 0) {
    const bool lowered = sDynScale > 0.f;
    if (mode == 2 || (mode == 1 && lowered)) {
      amount = static_cast< float >(strength) / 100.f;
    }
  }
  static float sApplied = -1.f;
  if (amount != sApplied) {
    sApplied = amount;
    aurora_set_present_sharpness(amount);
  }
}

void RequestGpuSelfTest() { sGpuSelfTestRequested.store(true, std::memory_order_release); }

void RunGpuSelfTestIfRequested() {
  // Pipelines compile asynchronously and a draw is skipped until its pipeline is ready, so a request first
  // runs a silent warm-up pass that waits for the test's own pipelines (selftest.cpp), then the logged pass.
  enum class Stage { Idle, WarmingUp };
  static Stage stage = Stage::Idle;
  static unsigned waited = 0;
  static unsigned frames = 0;
  static const bool envRun = port::EnvFlag("MP2_GPU_SELFTEST");
  if (envRun && ++frames == 120) {
    RequestGpuSelfTest();
  }
  bool runReal = false;
  if (stage == Stage::WarmingUp) {
    if (aurora_gpu_selftest_pending()) {
      return;
    }
    // The warm-up's lookups waited for every case's pipeline, so the logged pass cannot lose a draw:
    // a couple of frames between the passes is all that is left to do.
    if (++waited < 2) {
      return;
    }
    stage = Stage::Idle;
    runReal = true;
  } else if (sGpuSelfTestRequested.exchange(false, std::memory_order_acq_rel)) {
    if (aurora_gpu_selftest_run(true)) {
      stage = Stage::WarmingUp;
      waited = 0;
      RestoreGxAfterGpuSelfTest();
    }
    return;
  }
  if (runReal && aurora_gpu_selftest_run(false)) {
    RestoreGxAfterGpuSelfTest();
  }
}

} // namespace PortVideo
