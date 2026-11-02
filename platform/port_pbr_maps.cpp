// PBR maps for the HD texture pack; see port_pbr_maps.h.

#include "port_pbr_maps.h"

#include "port_overlay.h"
#include "port_utf8_path.h"

#include <aurora/texture.hpp>

#include <algorithm>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <tuple>

namespace PortPbrMaps {
struct Maps {
  GXTexObj orm;
  GXTexObj nrm;
};

namespace {
using Key = std::tuple< uint64_t, uint64_t, uint32_t, uint32_t, uint32_t, bool >;

Key MakeKey(const aurora::texture::TextureSourceKey& k) {
  return {k.textureHash, k.tlutHash, k.width, k.height, k.format, k.hasTlut};
}

struct Pair {
  std::filesystem::path orm;
  std::filesystem::path nrm;
  // Made, and its files registered with Aurora, the first time a drawn texture asks for it.
  std::unique_ptr< Maps > maps;
  aurora::texture::ReplacementRegistration regs[2];
};

// Neutral maps for the stand-in texture objects, drawn until Aurora has the files (and should a
// file fail to load): occlusion 1, roughness 1, metal 0; a flat normal (X = Y = 128). GX RGBA8,
// 4x4 with two mips (each level one 4x4 tile: 32 bytes of A,R pairs, then 32 of G,B pairs; Aurora's
// BuildRGBA8FromGCN, lib/gfx/texture_convert.cpp).
struct NeutralTexels {
  alignas(32) unsigned char bytes[3 * 64];
  NeutralTexels(unsigned char r, unsigned char g, unsigned char b) {
    for (int level = 0; level < 3; ++level) {
      unsigned char* tile = bytes + level * 64;
      for (int t = 0; t < 16; ++t) {
        tile[t * 2] = 255; // A
        tile[t * 2 + 1] = r;
        tile[32 + t * 2] = g;
        tile[32 + t * 2 + 1] = b;
      }
    }
  }
};
const NeutralTexels sNeutralOrm(255, 255, 0);
const NeutralTexels sNeutralNrm(128, 128, 255);

// The stand-ins' GXInitTexObjUserData values: Aurora's key for the registered file. Never freed or
// reused, so a FIFO command still naming an old stand-in after a rescan finds no file and draws the
// neutral texels (never another texture's map).
std::deque< unsigned char > sKeySlots;

std::map< Key, Pair > sPairs;
std::vector< std::string > sFolders;      // as read
std::vector< std::string > sWantFolders;  // as asked for
bool sWantRescan = false;
bool sEnabled = false;
bool sRead = false;
unsigned sGeneration = 1;
size_t sUsed = 0;

void UnregisterAll() {
  for (auto& [key, pair] : sPairs) {
    if (pair.maps) {
      for (const auto& reg : pair.regs) {
        if (reg.id != 0) {
          aurora::texture::unregister_replacement(reg);
        }
      }
    }
  }
  sPairs.clear();
  sUsed = 0;
}

void ReadFolder(const std::string& folder, size_t& files, size_t& unparsed) {
  std::error_code ec;
  const std::filesystem::path root = PortUtf8::FromUtf8(folder);
  if (!std::filesystem::is_directory(root, ec)) {
    return;
  }
  std::filesystem::recursive_directory_iterator it(
      root, std::filesystem::directory_options::skip_permission_denied, ec);
  for (; !ec && it != std::filesystem::recursive_directory_iterator(); it.increment(ec)) {
    std::error_code fileError;
    if (!it->is_regular_file(fileError)) {
      continue;
    }
    std::string base;
    bool isNormal = false;
    if (!SplitMapName(PortUtf8::ToUtf8(it->path().filename()), base, isNormal)) {
      continue;
    }
    ++files;
    const auto key = aurora::texture::parse_replacement_filename(base);
    if (!key.has_value()) {
      ++unparsed;
      continue;
    }
    Pair& pair = sPairs[MakeKey(*key)];
    // The first folder wins (the user pack is read first), as its textures do.
    std::filesystem::path& slot = isNormal ? pair.nrm : pair.orm;
    if (slot.empty()) {
      slot = it->path();
    }
  }
}

void Read() {
  UnregisterAll();
  ++sGeneration;
  sRead = false;
  if (!sEnabled) {
    return;
  }
  size_t files = 0;
  size_t unparsed = 0;
  for (const std::string& folder : sFolders) {
    ReadFolder(folder, files, unparsed);
  }
  size_t complete = 0;
  for (const auto& [key, pair] : sPairs) {
    complete += !pair.orm.empty() && !pair.nrm.empty();
  }
  sRead = true;
  std::fprintf(stderr, "[pbr] %zu PBR map pairs in %zu folder(s) (%zu map files, %zu without their pair, %zu "
                       "with a name that isn't a pack texture's)\n",
               complete, sFolders.size(), files, sPairs.size() - complete, unparsed);
}

std::optional< Key > LookupKey(const aurora::texture::TextureSourceKey& key) {
  // Aurora's order (find_source_replacement_key_locked): exact, any palette, any texels.
  if (sPairs.count(MakeKey(key)) != 0) {
    return MakeKey(key);
  }
  if (key.hasTlut) {
    auto tlutWildcard = key;
    tlutWildcard.tlutHash = aurora::texture::kWildcardTlutHash;
    if (sPairs.count(MakeKey(tlutWildcard)) != 0) {
      return MakeKey(tlutWildcard);
    }
  }
  auto textureWildcard = key;
  textureWildcard.textureHash = aurora::texture::kWildcardTextureHash;
  if (sPairs.count(MakeKey(textureWildcard)) != 0) {
    return MakeKey(textureWildcard);
  }
  return std::nullopt;
}

void InitStandIn(GXTexObj& obj, const NeutralTexels& texels, const std::filesystem::path& file,
                 aurora::texture::ReplacementRegistration& reg) {
  GXInitTexObj(&obj, const_cast< unsigned char* >(texels.bytes), 4, 4, GX_TF_RGBA8, GX_REPEAT, GX_REPEAT,
               GX_TRUE);
  // As the game's mipmapped textures, at 2x anisotropy: Aurora's PBR keeps a mod's maps at that
  // (MP2_PBR_ANISO), as more streaks a fine normal map along the view.
  GXInitTexObjLOD(&obj, GX_LIN_MIP_LIN, GX_LINEAR, 0.f, 2.f, 0.f, GX_FALSE, GX_FALSE, GX_ANISO_2);
  sKeySlots.emplace_back();
  void* key = &sKeySlots.back();
  GXInitTexObjUserData(&obj, key);
  // The user pack's priority (port_textures.cpp).
  reg = aurora::texture::register_file_replacement(aurora::texture::TexturePointerKey{key}, file,
                                                   aurora::texture::ReplacementOptions{.priority = 1});
}
} // namespace

void SetFolders(const std::vector< std::string >& folders, bool rescan) {
  sWantFolders = folders;
  sWantRescan = sWantRescan || rescan;
}

void Poll() {
  const bool enabled = PortOverlay::PbrMaps();
  if (enabled == sEnabled && sWantFolders == sFolders && !sWantRescan) {
    return;
  }
  const bool wasEnabled = sEnabled;
  sEnabled = enabled;
  sFolders = sWantFolders;
  sWantRescan = false;
  Read();
  if (wasEnabled && !enabled) {
    std::fprintf(stderr, "[pbr] PBR maps off\n");
  }
}

bool Active() { return sEnabled && sRead && !sPairs.empty(); }

unsigned Generation() { return sGeneration; }

const Maps* Find(const GXTexObj* texObj) {
  if (!Active() || texObj == nullptr) {
    return nullptr;
  }
  char name[96];
  if (GXPortTextureReplacementName(texObj, name, sizeof(name)) == 0) {
    return nullptr;
  }
  const auto sourceKey = aurora::texture::parse_replacement_filename(name);
  if (!sourceKey.has_value()) {
    return nullptr;
  }
  const auto key = LookupKey(*sourceKey);
  if (!key.has_value()) {
    return nullptr;
  }
  Pair& pair = sPairs[*key];
  if (pair.orm.empty() || pair.nrm.empty()) {
    return nullptr;
  }
  if (!pair.maps) {
    pair.maps = std::make_unique< Maps >();
    InitStandIn(pair.maps->orm, sNeutralOrm, pair.orm, pair.regs[0]);
    InitStandIn(pair.maps->nrm, sNeutralNrm, pair.nrm, pair.regs[1]);
    ++sUsed;
  }
  return pair.maps.get();
}

void Load(const Maps& maps) {
  GXLoadTexObj(const_cast< GXTexObj* >(&maps.orm), GX_TEXMAP6);
  GXLoadTexObj(const_cast< GXTexObj* >(&maps.nrm), GX_TEXMAP7);
}

size_t PairCount() {
  size_t complete = 0;
  for (const auto& [key, pair] : sPairs) {
    complete += !pair.orm.empty() && !pair.nrm.empty();
  }
  return complete;
}

size_t UsedCount() { return sUsed; }
} // namespace PortPbrMaps
