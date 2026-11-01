#include "port_texture_manifest.h"

#include "port_env.h"
#include "port_logfile.h"
#include "port_utf8_path.h"

#include <dolphin/gx.h>

#include <cstring>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <unordered_set>

namespace PortTextureManifest {
namespace {

std::mutex sMutex;
std::unordered_set< uint64_t > sSeen;
std::ofstream sFile;
bool sOpenTried = false;

// The manifest's path: the variable's value when it names one, else texture_manifest.jsonl beside
// the log file, else in the working directory.
std::string ManifestPath() {
  const char* value = port::EnvString("MP2_TEXTURE_MANIFEST");
  const std::string text = value != nullptr ? value : "";
  for (const char* on : {"1", "true", "on", "yes"}) {
    if (text == on) {
      const std::string log = PortLogFile::Path();
      if (!log.empty()) {
        return PortUtf8::ToUtf8(PortUtf8::FromUtf8(log).parent_path() / "texture_manifest.jsonl");
      }
      return "texture_manifest.jsonl";
    }
  }
  return text;
}

} // namespace

bool Enabled() {
  static const bool sEnabled = port::EnvFlag("MP2_TEXTURE_MANIFEST");
  return sEnabled;
}

bool Wanted(uint64_t key) {
  if (!Enabled()) {
    return false;
  }
  std::lock_guard< std::mutex > lock(sMutex);
  return sSeen.find(key) == sSeen.end();
}

void Record(uint64_t key, const void* texObj, int texelFormat, int slot, const Material& material) {
  if (!Enabled() || texObj == nullptr) {
    return;
  }
  std::lock_guard< std::mutex > lock(sMutex);
  if (!sSeen.insert(key).second) {
    return;
  }
  if (!sOpenTried) {
    sOpenTried = true;
    const std::string path = ManifestPath();
    sFile.open(PortUtf8::FromUtf8(path), std::ios::out | std::ios::app | std::ios::binary);
    const std::string note = sFile.is_open() ? "texture manifest: appending to " + path
                                             : "texture manifest: cannot open " + path;
    std::fprintf(stderr, "%s\n", note.c_str());
    PortLogFile::Write(note);
  }
  if (!sFile.is_open()) {
    return;
  }
  char name[96];
  if (GXPortTextureReplacementName(static_cast< const GXTexObj* >(texObj), name, sizeof(name)) ==
      0) {
    return;
  }
  sFile << FormatLine(name, texelFormat, slot, material) << '\n';
  // Flushed per record: they are few (once per material and texture), and a crash keeps them.
  sFile.flush();
}

} // namespace PortTextureManifest
