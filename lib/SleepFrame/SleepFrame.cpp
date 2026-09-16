#include "SleepFrame.h"

#include <HalStorage.h>
#include <MinizConfig.h>
#include <esp_heap_caps.h>

#include <array>
#include <cstring>
#include <limits>
#include <utility>

namespace {
using Snapshot = GfxRenderer::FrameSnapshot;
using Kind = GfxRenderer::SnapshotKind;
using Header = std::array<uint8_t, 32>;
constexpr uint16_t VERSION = 1;
constexpr uint8_t TUPLE_KIND = 1;
constexpr uint8_t BW_KIND = 2;
constexpr size_t CHECKSUM_OFFSET = 28;

uint16_t read16(const uint8_t* bytes) { return bytes[0] | (uint16_t(bytes[1]) << 8); }

uint32_t read32(const uint8_t* bytes) {
  return uint32_t(bytes[0]) | (uint32_t(bytes[1]) << 8) | (uint32_t(bytes[2]) << 16) | (uint32_t(bytes[3]) << 24);
}

void write16(uint8_t* bytes, uint16_t value) {
  bytes[0] = value;
  bytes[1] = value >> 8;
}

void write32(uint8_t* bytes, uint32_t value) {
  for (size_t i = 0; i < 4; ++i) bytes[i] = value >> (8 * i);
}

bool planeSize(SleepFrame::Geometry geometry, size_t& bytes) {
  if (!geometry.width || !geometry.height || geometry.stride < (uint32_t(geometry.width) + 7) / 8 ||
      geometry.orientation < GfxRenderer::Portrait || geometry.orientation > GfxRenderer::LandscapeCounterClockwise)
    return false;
  const uint64_t size = uint64_t(geometry.stride) * geometry.height;
  if (size > std::numeric_limits<size_t>::max() / 3 || size > std::numeric_limits<int>::max() / 3) return false;
  bytes = static_cast<size_t>(size);
  return true;
}

bool sourceValid(const Snapshot& source, bool retentionEnabled, size_t& bytes) {
  if (!source.valid || source.physicalByteX || source.planes.y0() ||
      source.kind != (retentionEnabled ? Kind::RetainedTuple : Kind::LegacyBw) ||
      !planeSize({source.panelWidth, source.panelHeight, source.panelStride, source.orientation}, bytes))
    return false;
  if (source.planes.width() != source.panelWidth || source.planes.rows() != source.panelHeight ||
      source.planes.stride() != source.panelStride)
    return false;
  const GrayFrame::Plane planes[] = {source.planes.b(), source.planes.l(), source.planes.m()};
  for (size_t i = 0; i < (retentionEnabled ? 3u : 1u); ++i) {
    if (!planes[i].data || planes[i].capacity < bytes) return false;
  }
  return true;
}

Header makeHeader(const Snapshot& source, size_t bytes) {
  Header header{};
  std::memcpy(header.data(), "SLPF", 4);
  write16(header.data() + 4, VERSION);
  header[6] = source.kind == Kind::RetainedTuple ? TUPLE_KIND : BW_KIND;
  header[7] = source.orientation;
  write16(header.data() + 8, source.panelWidth);
  write16(header.data() + 10, source.panelHeight);
  write16(header.data() + 12, source.panelStride);
  write32(header.data() + 16, bytes);
  const size_t selectorBytes = source.kind == Kind::RetainedTuple ? bytes : 0;
  write32(header.data() + 20, selectorBytes);
  write32(header.data() + 24, selectorBytes);
  uint32_t crc = mz_crc32(MZ_CRC32_INIT, header.data(), CHECKSUM_OFFSET);
  crc = mz_crc32(crc, source.planes.b().data, bytes);
  if (selectorBytes) {
    crc = mz_crc32(crc, source.planes.l().data, selectorBytes);
    crc = mz_crc32(crc, source.planes.m().data, selectorBytes);
  }
  write32(header.data() + CHECKSUM_OFFSET, crc);
  return header;
}

bool readHeader(HalFile& file, SleepFrame::Geometry expected, bool retentionEnabled, Header& header, size_t& bytes,
                size_t& total) {
  if (!planeSize(expected, bytes) || file.read(header.data(), header.size()) != static_cast<int>(header.size()))
    return false;
  if (std::memcmp(header.data(), "SLPF", 4) || read16(header.data() + 4) != VERSION ||
      (header[6] != TUPLE_KIND && header[6] != BW_KIND) || read16(header.data() + 14) != 0)
    return false;
  if (header[7] != expected.orientation || read16(header.data() + 8) != expected.width ||
      read16(header.data() + 10) != expected.height || read16(header.data() + 12) != expected.stride)
    return false;
  if (retentionEnabled && header[6] != TUPLE_KIND) return false;
  const size_t selectorBytes = header[6] == TUPLE_KIND ? bytes : 0;
  if (read32(header.data() + 16) != bytes || read32(header.data() + 20) != selectorBytes ||
      read32(header.data() + 24) != selectorBytes)
    return false;
  total = bytes + 2 * selectorBytes;
  return file.fileSize64() == uint64_t(header.size()) + total;
}

bool removeExisting(const char* path) {
  bool exists;
  return Storage.exists(path, exists) && (!exists || Storage.remove(path));
}
}  // namespace

namespace SleepFrame {
void LoadedFrame::Deleter::operator()(uint8_t* bytes) const { heap_caps_free(bytes); }

bool save(const char* path, const Snapshot& source, bool retentionEnabled) {
  if (!removeExisting(path)) return false;
  size_t bytes;
  if (!sourceValid(source, retentionEnabled, bytes)) return false;
  HalFile file;
  if (!Storage.openFileForWrite("SLP", path, file)) {
    Storage.remove(path);
    return false;
  }
  const Header header = makeHeader(source, bytes);
  bool written = file.write(header.data(), header.size()) == header.size();
  const GrayFrame::Plane planes[] = {source.planes.b(), source.planes.l(), source.planes.m()};
  for (size_t i = 0; written && i < (retentionEnabled ? 3u : 1u); ++i)
    written = file.write(planes[i].data, bytes) == bytes;
  const bool synced = written && file.sync();
  const bool closed = file.close();
  if (synced && closed) return true;
  Storage.remove(path);
  return false;
}

bool load(const char* path, Geometry expected, bool retentionEnabled, LoadedFrame& out) {
  HalFile file;
  if (!Storage.openFileForRead("SLP", path, file)) return false;
  Header header;
  size_t bytes = 0;
  size_t total = 0;
  LoadedFrame candidate;
  bool valid = readHeader(file, expected, retentionEnabled, header, bytes, total);
  if (valid) {
    candidate.storage_.reset(static_cast<uint8_t*>(heap_caps_malloc(total, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
    valid = candidate.storage_ && file.read(candidate.storage_.get(), total) == static_cast<int>(total);
  }
  if (valid) {
    uint32_t crc = mz_crc32(MZ_CRC32_INIT, header.data(), CHECKSUM_OFFSET);
    crc = mz_crc32(crc, candidate.storage_.get(), total);
    valid = crc == read32(header.data() + CHECKSUM_OFFSET);
  }
  const bool closed = file.close();
  if (!valid || !closed) return false;
  uint8_t* data = candidate.storage_.get();
  const GrayFrame::Plane l = retentionEnabled ? GrayFrame::Plane{data + bytes, bytes} : GrayFrame::Plane{};
  const GrayFrame::Plane m = retentionEnabled ? GrayFrame::Plane{data + 2 * bytes, bytes} : GrayFrame::Plane{};
  candidate.snapshot_.planes = {{data, bytes}, l, m, expected.width, expected.height, expected.stride};
  candidate.snapshot_.panelWidth = expected.width;
  candidate.snapshot_.panelHeight = expected.height;
  candidate.snapshot_.panelStride = expected.stride;
  candidate.snapshot_.orientation = expected.orientation;
  candidate.snapshot_.kind = retentionEnabled ? Kind::RetainedTuple : Kind::LegacyBw;
  candidate.snapshot_.valid = true;
  out = std::move(candidate);
  return true;
}
}  // namespace SleepFrame
