#pragma once

#include <string>
#include <vector>

#include <dolphin/gx.h>

#include "port_pbr_rules.h"

// PBR maps for the HD texture pack (step 3 of docs/research/PBR_TEXTURES.md).
//
// tools/pbr/make_pbr_maps.py writes, for a pack texture "<stem>.dds", a normal map "<stem>_nrm.dds"
// and an occlusion/roughness/metal map "<stem>_orm.dds". Put that folder in a texture pack folder
// (the user pack, `user_textures`, or the built-in set's folder on disk): the port's replacement
// loader skips both names, and this module finds them by the pack name they sit beside, in every
// subfolder. A map pair is registered with Aurora under a stand-in texture object the first time a
// surface with that texture is drawn, and streams in like the pack's own textures (a neutral map
// is drawn until it has).
//
// With the `pbr_maps` setting on (`MP2_PBR_MAPS`, F1 > Video > Quality), CCubeMaterial::SetCurrent
// picks the TEV stage that lights a surface's base texture and asks Find for its maps;
// CCubeModel::DrawSurface binds them (GX_TEXMAP6 and 7) and relights that stage with
// GXPortSetSurfacePBR for the surface's display list only.
namespace PortPbrMaps {
struct Maps;

// The setting and the pack folders (from PortTextures: each loaded set's folder on disk), applied
// at the next Poll; a change of either, or `rescan`, reads the folders again.
void SetFolders(const std::vector< std::string >& folders, bool rescan);

// Main thread, once per frame: applies the setting and the folders.
void Poll();

// The setting as last applied, and folders read: surfaces may be relit.
bool Active();

// Bumped by every rescan and setting change. A Maps pointer, and a texture's cached answer, is
// valid only in the generation it came from (never 0).
unsigned Generation();

// The maps for the texture loaded as `texObj`, or null (none, or only one of the two). Names the
// texture as the pack does (its hash; for a paletted texture, ask after its TLUT is loaded).
// Main thread; the caller caches the answer per texture and generation.
const Maps* Find(const GXTexObj* texObj);

// Loads the maps into GX_TEXMAP6 (occlusion/roughness/metal) and GX_TEXMAP7 (normal). The caller
// drops its own record of what those slots hold (CTexture::InvalidateTexmap).
void Load(const Maps& maps);

// How many map pairs the folders hold, and how many a drawn texture has asked for so far.
size_t PairCount();
size_t UsedCount();
} // namespace PortPbrMaps
