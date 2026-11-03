#include "port_mods.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {
int sFailures = 0;

void Check(bool condition, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", what);
    ++sFailures;
  }
}

void Put32(std::vector<uint8_t>& out, uint32_t value) {
  out.push_back(uint8_t(value >> 24));
  out.push_back(uint8_t(value >> 16));
  out.push_back(uint8_t(value >> 8));
  out.push_back(uint8_t(value));
}

uint32_t Get32(const std::vector<uint8_t>& data, size_t at) {
  return (uint32_t(data[at]) << 24) | (uint32_t(data[at + 1]) << 16) | (uint32_t(data[at + 2]) << 8) | data[at + 3];
}

constexpr uint32_t kTXTR = 0x54585452;
constexpr uint32_t kSTRG = 0x53545247;

// A PAK with one named resource and three table entries; resource 0x11 is
// listed twice, as retail PAKs do. Data is filled with each entry's index + 1.
std::vector<uint8_t> MakePak(size_t& headerEnd) {
  std::vector<uint8_t> pak;
  Put32(pak, 0x00030005);
  Put32(pak, 0);
  Put32(pak, 1);
  Put32(pak, kSTRG);
  Put32(pak, 0x22);
  Put32(pak, 5);
  for (const char c : std::string("Hello")) {
    pak.push_back(uint8_t(c));
  }
  Put32(pak, 3);
  const size_t table = pak.size();
  headerEnd = table + 3 * 20;
  const uint32_t data = uint32_t((headerEnd + 31) & ~size_t(31));
  const uint32_t entries[3][3] = {{kTXTR, 0x11, 64}, {kSTRG, 0x22, 32}, {kTXTR, 0x11, 64}};
  const uint32_t offsets[3] = {data, data + 64, data};
  for (int i = 0; i < 3; ++i) {
    Put32(pak, i == 1 ? 1 : 0);
    Put32(pak, entries[i][0]);
    Put32(pak, entries[i][1]);
    Put32(pak, entries[i][2]);
    Put32(pak, offsets[i]);
  }
  pak.resize(data, 0);
  pak.resize(data + 64, 1);
  pak.resize(data + 96, 2);
  pak.resize(data + 100, 3); // an unaligned tail
  return pak;
}

// The "disc" a Reader's kSource segments read.
const std::vector<uint8_t>* sSource = nullptr;
int sOpens = 0;

void* OpenMemory(const PortMods::VirtualFile&) {
  ++sOpens;
  return const_cast<std::vector<uint8_t>*>(sSource);
}

int64_t ReadMemory(void* handle, uint64_t offset, uint8_t* buffer, size_t length) {
  const auto& data = *static_cast<const std::vector<uint8_t>*>(handle);
  if (offset >= data.size()) {
    return 0;
  }
  const size_t n = std::min<size_t>(length, data.size() - size_t(offset));
  std::memcpy(buffer, data.data() + offset, n);
  return int64_t(n);
}

void CloseMemory(void*) {}

const PortMods::SourceIo sIo{OpenMemory, ReadMemory, CloseMemory};

std::vector<uint8_t> ReadAll(const PortMods::VirtualFile& file, size_t chunk) {
  PortMods::Reader reader(std::make_shared<const PortMods::VirtualFile>(file), &sIo);
  std::vector<uint8_t> out(file.size + 16, 0xEE);
  size_t done = 0;
  for (;;) {
    const int64_t got = reader.Read(out.data() + done, std::min(chunk, out.size() - done));
    if (got <= 0) {
      break;
    }
    done += size_t(got);
  }
  out.resize(done);
  return out;
}

void TestNames() {
  uint32_t type = 0;
  uint32_t id = 0;
  Check(PortMods::ParseLooseName("1a2B3c4D.txtr", type, id) && type == kTXTR && id == 0x1A2B3C4D, "loose name");
  Check(!PortMods::ParseLooseName("1A2B3C4.TXTR", type, id), "short id");
  Check(!PortMods::ParseLooseName("1A2B3C4G.TXTR", type, id), "bad hex");
  Check(!PortMods::ParseLooseName("1A2B3C4D.TXT", type, id), "short type");
  Check(!PortMods::ParseLooseName("Metroid1.pak", type, id), "disc name");
  Check(PortMods::FourCCString(kTXTR) == "TXTR", "fourcc");
  Check(PortMods::SplitDisabled("a/b//c") == std::vector<std::string>({"a", "b", "c"}), "split");
  Check(PortMods::JoinDisabled({"a", "", "b/c", "d"}) == "a/d", "join");
}

void TestParse() {
  size_t headerEnd = 0;
  const std::vector<uint8_t> pak = MakePak(headerEnd);
  PortMods::PakTable table;
  size_t needed = 0;
  Check(PortMods::ParsePakTable(pak.data(), pak.size(), table, needed), "parse");
  Check(table.headerEnd == headerEnd && table.resources.size() == 3, "table size");
  Check(table.resources[1].compressed == 1 && table.resources[1].id == 0x22, "entry fields");
  for (size_t cut = 0; cut < headerEnd; cut += 7) {
    Check(!PortMods::ParsePakTable(pak.data(), cut, table, needed) && needed > cut && needed <= headerEnd,
          "truncated header asks for more");
  }
  std::vector<uint8_t> bad = pak;
  bad[3] = 6;
  Check(!PortMods::ParsePakTable(bad.data(), bad.size(), table, needed) && needed == 0, "wrong version");
}

void TestPatch(const fs::path& dir) {
  size_t headerEnd = 0;
  const std::vector<uint8_t> pak = MakePak(headerEnd);
  PortMods::PakTable table;
  size_t needed = 0;
  PortMods::ParsePakTable(pak.data(), pak.size(), table, needed);

  // 40 bytes of 'A': padded to 64 in the patched PAK.
  const fs::path loosePath = dir / "00000011.TXTR";
  std::ofstream(loosePath, std::ios::binary) << std::string(40, 'A');
  PortMods::LooseResource loose{kTXTR, 0x11, loosePath.string(), 40, "test"};
  PortMods::LooseResource unused{kTXTR, 0x99, loosePath.string(), 40, "test"};
  const PortMods::VirtualFile file = PortMods::PatchPak(pak, table, pak.size(), {&loose, &unused});
  const uint64_t appended = (pak.size() + 31) & ~uint64_t(31);
  Check(file.size == appended + 64, "patched size");

  sSource = &pak;
  sOpens = 0;
  for (const size_t chunk : {size_t(1), size_t(13), size_t(4096)}) {
    const std::vector<uint8_t> out = ReadAll(file, chunk);
    Check(out.size() == file.size, "read whole file");
    if (out.size() != file.size) {
      continue;
    }
    // Both entries of 0x11 now point at the appended copy, uncompressed.
    for (const size_t entry : {table.resources[0].entryOffset, table.resources[2].entryOffset}) {
      Check(Get32(out, entry) == 0 && Get32(out, entry + 12) == 64 && Get32(out, entry + 16) == appended,
            "patched entry");
    }
    // The other entry and the original data are untouched.
    const size_t other = table.resources[1].entryOffset;
    Check(std::memcmp(out.data() + other, pak.data() + other, 20) == 0, "untouched entry");
    Check(std::memcmp(out.data() + headerEnd, pak.data() + headerEnd, pak.size() - headerEnd) == 0,
          "original data");
    bool padding = true;
    for (uint64_t i = pak.size(); i < appended; ++i) {
      padding = padding && out[i] == 0;
    }
    Check(padding, "alignment padding");
    Check(std::string(out.begin() + appended, out.begin() + appended + 40) == std::string(40, 'A'), "loose data");
    bool tail = true;
    for (uint64_t i = appended + 40; i < file.size; ++i) {
      tail = tail && out[i] == 0;
    }
    Check(tail, "loose padding");
  }
  Check(sOpens == 3, "source opened once per reader");

  // Seek to the middle of a segment boundary and read across it.
  PortMods::Reader reader(std::make_shared<const PortMods::VirtualFile>(file), &sIo);
  Check(reader.Seek(-8, 2) == int64_t(file.size - 8), "seek from end");
  Check(reader.Seek(int64_t(appended) - 4, 0) == int64_t(appended - 4), "seek set");
  uint8_t cross[8];
  Check(reader.Read(cross, 8) == 8 && cross[3] == 0 && cross[4] == 'A', "read across segments");
  Check(reader.Seek(-100000, 1) == -1, "seek before start");

  // A PAK replaced whole by a mod: the original data comes from its host file.
  const fs::path hostPak = dir / "Metroid1.pak";
  std::ofstream(hostPak, std::ios::binary).write(reinterpret_cast<const char*>(pak.data()), std::streamsize(pak.size()));
  const PortMods::VirtualFile hosted = PortMods::PatchPak(pak, table, pak.size(), {&loose}, hostPak.string());
  sSource = nullptr;
  sOpens = 0;
  const std::vector<uint8_t> out = ReadAll(hosted, 4096);
  Check(sOpens == 0 && out.size() == hosted.size, "hosted PAK reads without the disc");
  Check(out.size() == hosted.size &&
            std::memcmp(out.data() + headerEnd, pak.data() + headerEnd, pak.size() - headerEnd) == 0,
        "hosted original data");
}
// New resources (in no PAK) get new table entries: the table grows, the original data moves back by
// the entries' size rounded to 32, and every original offset moves with it.
void TestPatchAdded(const fs::path& dir) {
  size_t headerEnd = 0;
  const std::vector<uint8_t> pak = MakePak(headerEnd);
  PortMods::PakTable table;
  size_t needed = 0;
  PortMods::ParsePakTable(pak.data(), pak.size(), table, needed);

  const fs::path replacePath = dir / "00000011.TXTR";
  std::ofstream(replacePath, std::ios::binary) << std::string(40, 'A');
  const fs::path newPath = dir / "00000055.TXTR";
  std::ofstream(newPath, std::ios::binary) << std::string(50, 'N');
  const fs::path otherPath = dir / "00000066.STRG";
  std::ofstream(otherPath, std::ios::binary) << std::string(7, 'S');
  PortMods::LooseResource replace{kTXTR, 0x11, replacePath.string(), 40, "test"};
  PortMods::LooseResource added{kTXTR, 0x55, newPath.string(), 50, "test", "metroid1.pak"};
  PortMods::LooseResource other{kSTRG, 0x66, otherPath.string(), 7, "test", "metroid1.pak"};
  const PortMods::VirtualFile file = PortMods::PatchPak(pak, table, pak.size(), {&replace}, {}, {&added, &other});
  const uint64_t shift = 64;  // two 20-byte entries, rounded to 32

  sSource = &pak;
  const std::vector<uint8_t> out = ReadAll(file, 4096);
  Check(out.size() == file.size, "added: read whole file");
  if (out.size() != file.size) {
    return;
  }
  PortMods::PakTable patched;
  Check(PortMods::ParsePakTable(out.data(), out.size(), patched, needed), "added: patched table parses");
  Check(patched.resources.size() == 5, "added: two more entries");
  Check(patched.headerEnd == headerEnd + 40, "added: entries follow the table");
  if (patched.resources.size() != 5) {
    return;
  }
  // The untouched entry still finds its data, now `shift` further on.
  const PortMods::PakResource& strg = patched.resources[1];
  Check(strg.offset == table.resources[1].offset + shift, "added: original offset moved");
  Check(std::memcmp(out.data() + strg.offset, pak.data() + table.resources[1].offset, 32) == 0, "added: original data moved");
  Check(std::memcmp(out.data() + headerEnd + shift, pak.data() + headerEnd, pak.size() - headerEnd) == 0,
        "added: all original data after the header");
  bool gap = true;
  for (uint64_t i = patched.headerEnd; i < headerEnd + shift; ++i) {
    gap = gap && out[i] == 0;
  }
  Check(gap, "added: zeros between the table and the data");
  // The replaced entries point at its copy; the new ones at theirs, uncompressed.
  Check(patched.resources[0].offset == patched.resources[2].offset && patched.resources[0].size == 64, "added: replaced");
  Check(std::string(out.begin() + patched.resources[0].offset, out.begin() + patched.resources[0].offset + 40) ==
            std::string(40, 'A'),
        "added: replaced data");
  const PortMods::PakResource& n = patched.resources[3];
  const PortMods::PakResource& o = patched.resources[4];
  Check(n.type == kTXTR && n.id == 0x55 && n.compressed == 0 && n.size == 64, "added: new entry");
  Check(o.type == kSTRG && o.id == 0x66 && o.size == 32 && o.offset == n.offset + 64, "added: second new entry");
  Check(n.offset % 32 == 0 && n.offset >= pak.size() + shift, "added: new data appended, aligned");
  Check(std::string(out.begin() + n.offset, out.begin() + n.offset + 50) == std::string(50, 'N'), "added: new data");
  Check(std::string(out.begin() + o.offset, out.begin() + o.offset + 7) == std::string(7, 'S'), "added: second new data");
  Check(file.size == o.offset + 32, "added: file ends after the last resource");

  // Only additions: the original data still moves, and the old entries keep their own data.
  const PortMods::VirtualFile only = PortMods::PatchPak(pak, table, pak.size(), {}, {}, {&added});
  const std::vector<uint8_t> out2 = ReadAll(only, 13);
  PortMods::PakTable t2;
  Check(PortMods::ParsePakTable(out2.data(), out2.size(), t2, needed) && t2.resources.size() == 4, "added only: table");
  if (t2.resources.size() == 4) {
    for (int i = 0; i < 3; ++i) {
      Check(t2.resources[i].offset == table.resources[i].offset + 32, "added only: offsets moved by 32");
    }
    Check(std::string(out2.begin() + t2.resources[3].offset, out2.begin() + t2.resources[3].offset + 50) ==
              std::string(50, 'N'),
          "added only: new data");
  }
  sSource = nullptr;
}
} // namespace

int main() {
  const fs::path dir = fs::temp_directory_path() / "port_mods_tests";
  std::error_code ec;
  fs::remove_all(dir, ec);
  fs::create_directories(dir);
  TestNames();
  TestParse();
  TestPatch(dir);
  TestPatchAdded(dir);
  fs::remove_all(dir, ec);
  if (sFailures != 0) {
    std::fprintf(stderr, "%d failure(s)\n", sFailures);
    return 1;
  }
  std::puts("port_mods_tests: ok");
  return 0;
}
