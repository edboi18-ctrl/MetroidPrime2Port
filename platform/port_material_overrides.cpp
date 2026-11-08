// The material editor's overrides at run time (port_material_overrides.h): every enabled mod's
// materials.json, read at start and on Reload mods, and again whenever one changes on disk, so
// tools/material_editor's saves show in the running game within a couple of seconds.
#include "port_material_overrides.h"

#include "port_mods.h"
#include "port_utf8_path.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <system_error>
#include <vector>

namespace PortMaterialOverrides {
namespace {
namespace fs = std::filesystem;

std::unordered_map<uint64_t, Override> sOverrides;
int sFiles = 0;
std::string sProblems;

// A candidate file as last seen: whether it was there, and its time and size.
struct Seen {
  fs::path path;
  bool present = false;
  fs::file_time_type time{};
  uintmax_t size = 0;
  bool operator==(const Seen& other) const {
    return path == other.path && present == other.present && time == other.time && size == other.size;
  }
};
std::vector<Seen> sSeen;
std::chrono::steady_clock::time_point sNextPoll{};

// materials.json in every mod folder the settings leave on, in name order, as PortMods applies the
// mods (a folder made since the last scan included, so the editor's first save needs no Reload mods).
std::vector<fs::path> Candidates() {
  std::vector<fs::path> out;
  const PortMods::Status& status = PortMods::CurrentStatus();
  if (status.folder.empty() || !status.active) {
    return out;
  }
  const std::vector<std::string> disabled = PortMods::SplitDisabled(PortMods::Disabled());
  std::vector<fs::path> dirs;
  std::error_code ec;
  for (fs::directory_iterator it(PortUtf8::FromUtf8(status.folder), ec), end; !ec && it != end; it.increment(ec)) {
    const std::string name = PortUtf8::ToUtf8(it->path().filename());
    std::error_code dirError;
    if (name.empty() || name[0] == '.' || !it->is_directory(dirError) ||
        std::find(disabled.begin(), disabled.end(), name) != disabled.end()) {
      continue;
    }
    dirs.push_back(it->path());
  }
  std::sort(dirs.begin(), dirs.end());
  for (const fs::path& dir : dirs) {
    out.push_back(dir / "materials.json");
  }
  return out;
}

std::vector<Seen> Look(const std::vector<fs::path>& files) {
  std::vector<Seen> out;
  out.reserve(files.size());
  for (const fs::path& path : files) {
    Seen seen;
    seen.path = path;
    std::error_code ec;
    if (fs::is_regular_file(path, ec)) {
      seen.present = true;
      seen.time = fs::last_write_time(path, ec);
      seen.size = fs::file_size(path, ec);
    }
    out.push_back(seen);
  }
  return out;
}

void Read(const std::vector<Seen>& files) {
  std::unordered_map<uint64_t, Override> overrides;
  int read = 0;
  std::string problems;
  for (const Seen& file : files) {
    if (!file.present) {
      continue;
    }
    std::ifstream in(file.path, std::ios::binary);
    const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    const std::string where = PortUtf8::ToUtf8(file.path.parent_path().filename()) + "/materials.json";
    if (!in && !in.eof()) {
      problems += where + ": could not be read\n";
      continue;
    }
    bool ok = false;
    std::string error;
    const int count = ParseFile(text, overrides, ok, error);
    ++read;
    std::fprintf(stderr, "materials: %s: %d material(s)%s%s\n", where.c_str(), count, ok ? "" : ", ",
                 ok ? "" : error.c_str());
    if (!ok) {
      problems += where + ": " + error + " (the materials before it are used)\n";
    }
  }
  sOverrides = std::move(overrides);
  sFiles = read;
  sProblems = std::move(problems);
  if (read != 0 || !sSeen.empty()) {
    std::fprintf(stderr, "materials: %zu material override(s) from %d file(s)\n", sOverrides.size(), read);
  }
}
} // namespace

void Load() {
  sSeen = Look(Candidates());
  Read(sSeen);
  sNextPoll = std::chrono::steady_clock::now() + std::chrono::seconds(2);
}

void Poll() {
  const auto now = std::chrono::steady_clock::now();
  if (now < sNextPoll) {
    return;
  }
  sNextPoll = now + std::chrono::seconds(2);
  std::vector<Seen> seen = Look(Candidates());
  if (seen == sSeen) {
    return;
  }
  sSeen = std::move(seen);
  Read(sSeen);
}

bool Any() { return !sOverrides.empty(); }

const Override* Find(uint64_t key) {
  const auto it = sOverrides.find(key);
  return it != sOverrides.end() ? &it->second : nullptr;
}

int Count() { return static_cast<int>(sOverrides.size()); }
int Files() { return sFiles; }
const std::string& Problems() { return sProblems; }

} // namespace PortMaterialOverrides
