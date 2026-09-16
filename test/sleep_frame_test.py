#!/usr/bin/env python3
"""Exercise production sleep persistence with fake storage and literal wire fixtures."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import gfx_gray_test as renderer_test

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "lib/SleepFrame/SleepFrame.cpp"

STORAGE = r'''
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <vector>
struct FakeStorage {
  std::vector<uint8_t> bytes;
  bool present = false, failOpen = false, failRemove = false, failSync = false, failClose = false;
  int shortRead = 0, shortWrite = 0, reads = 0, writes = 0, opens = 0;
  bool exists(const char*, bool& result) { result = present; return true; }
  bool remove(const char*) {
    if (failRemove) return false;
    present = false; bytes.clear(); return true;
  }
  bool openFileForRead(const char*, const char*, class HalFile&);
  bool openFileForWrite(const char*, const char*, class HalFile&);
};
inline FakeStorage Storage;
class HalFile {
 public:
  size_t position = 0;
  uint64_t fileSize64() { return Storage.bytes.size(); }
  int read(void* output, size_t length) {
    const bool shortTransfer = ++Storage.reads == Storage.shortRead;
    length = std::min(length, Storage.bytes.size() - position);
    if (shortTransfer && length) --length;
    std::memcpy(output, Storage.bytes.data() + position, length); position += length;
    return static_cast<int>(length);
  }
  size_t write(const void* input, size_t length) {
    if (++Storage.writes == Storage.shortWrite && length) --length;
    const auto* bytes = static_cast<const uint8_t*>(input);
    Storage.bytes.insert(Storage.bytes.end(), bytes, bytes + length); return length;
  }
  bool sync() { return !Storage.failSync; }
  bool close() { return !Storage.failClose; }
};
inline bool FakeStorage::openFileForRead(const char*, const char*, HalFile&) {
  ++opens; return present && !failOpen;
}
inline bool FakeStorage::openFileForWrite(const char*, const char*, HalFile&) {
  ++opens; if (failOpen) return false;
  bytes.clear(); present = true; return true;
}
'''

HARNESS = r'''
#include <SleepFrame.h>
#include <esp_heap_caps.h>
#include <array>
#include <cstdio>
#include <cstdlib>

using Kind = GfxRenderer::SnapshotKind;
using Snapshot = GfxRenderer::FrameSnapshot;
const SleepFrame::Geometry geometry{16, 2, 2, GfxRenderer::LandscapeClockwise};
const std::vector<uint8_t> tuple = {
  0x53,0x4c,0x50,0x46,0x01,0x00,0x01,0x01,0x10,0x00,0x02,0x00,0x02,0x00,0x00,0x00,
  0x04,0x00,0x00,0x00,0x04,0x00,0x00,0x00,0x04,0x00,0x00,0x00,0xff,0xe5,0x3b,0x45,
  0x01,0x23,0x45,0x67,0x89,0xab,0xcd,0xef,0x10,0x32,0x54,0x76
};
const std::vector<uint8_t> bw = {
  0x53,0x4c,0x50,0x46,0x01,0x00,0x02,0x01,0x10,0x00,0x02,0x00,0x02,0x00,0x00,0x00,
  0x04,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0xf9,0x0b,0xb3,0x56,
  0x01,0x23,0x45,0x67
};
void require(bool value, const char* message) {
  if (!value) { std::fprintf(stderr, "%s: expected true, actual false\n", message); std::exit(1); }
}
void input(const std::vector<uint8_t>& bytes) { Storage = {}; Storage.present = true; Storage.bytes = bytes; }
void reseal(std::vector<uint8_t>& bytes) {
  uint32_t crc = 0xffffffff;
  for (size_t i=0; i<bytes.size(); ++i) {
    if (i>=28 && i<32) continue;
    crc ^= bytes[i];
    for (int bit=0; bit<8; ++bit) crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  crc ^= 0xffffffff;
  for (int i=0; i<4; ++i) bytes[28+i] = crc >> (i*8);
}
void reject(SleepFrame::LoadedFrame& out, const char* reason, bool retention = true,
            SleepFrame::Geometry expected = geometry) {
  const auto before = out.snapshot();
  std::array<uint8_t, 12> bytes;
  std::memcpy(bytes.data(), before.planes.b().data, bytes.size());
  const int allocations = liveAllocations;
  require(!SleepFrame::load("/sleep", expected, retention, out), reason);
  const auto& after = out.snapshot();
  require(after.valid == before.valid && after.kind == before.kind &&
          after.orientation == before.orientation && after.restoreEpoch == before.restoreEpoch &&
          after.physicalByteX == before.physicalByteX && after.panelWidth == before.panelWidth &&
          after.panelHeight == before.panelHeight && after.panelStride == before.panelStride &&
          after.planes.b().data == before.planes.b().data && after.planes.l().data == before.planes.l().data &&
          after.planes.m().data == before.planes.m().data &&
          std::memcmp(after.planes.b().data, bytes.data(), bytes.size()) == 0,
          "failed load preserves prior descriptor and every destination byte");
  require(liveAllocations == allocations, "failed load releases temporary storage");
}
int main() {
  auto oracle = tuple; reseal(oracle);
  require(oracle == tuple, "independent CRC32 matches tuple literal 453BE5FF");
  oracle = bw; reseal(oracle);
  require(oracle == bw, "independent CRC32 matches BW literal 56B30BF9");
  {
    SleepFrame::LoadedFrame out;
    input(tuple);
    require(SleepFrame::load("/sleep", geometry, true, out), "valid literal tuple loads");
    auto frame = out.snapshot();
    require(frame.valid && frame.kind == Kind::RetainedTuple && frame.planes.valid() &&
            frame.panelWidth == 16 && frame.panelHeight == 2 && frame.panelStride == 2 &&
            frame.orientation == geometry.orientation && frame.restoreEpoch == 0 && frame.physicalByteX == 0 &&
            frame.planes.width() == 16 && frame.planes.rows() == 2 && frame.planes.stride() == 2 &&
            frame.planes.y0() == 0 && frame.planes.b().capacity == 4 && frame.planes.l().capacity == 4 &&
            frame.planes.m().capacity == 4 && std::memcmp(frame.planes.b().data, tuple.data()+32, 12) == 0,
            "loaded tuple exposes exact complete geometry and all three planes");
    require(liveAllocations == 1 && allocatedBytes == 12, "load owns exactly one payload allocation");
    input(bw); reject(out, "retained mode rejects LegacyBw");
    for (size_t n=0; n<tuple.size(); ++n) {
      input({tuple.begin(), tuple.begin()+n}); reject(out, "truncated tuple rejected");
    }
    for (size_t i=0; i<tuple.size(); ++i) {
      auto corrupt = tuple; corrupt[i] ^= 0x80; input(corrupt);
      reject(out, "corruption rejected before publication");
    }
    for (auto offset : {0,4,5,6,7,8,10,12,14,15,16,20,24}) {
      auto invalid = tuple; invalid[offset] = 0xff; reseal(invalid); input(invalid);
      reject(out, "checksummed invalid metadata rejected");
    }
    auto trailing = tuple; trailing.push_back(0); input(trailing); reject(out, "trailing bytes rejected");
    input(std::vector<uint8_t>(48000, 0xff)); reject(out, "old raw 48000-byte BW rejected");
    for (int read=1; read<=2; ++read) {
      input(tuple); Storage.shortRead = read; reject(out, "short read rejected");
    }
    input(tuple); Storage.failOpen = true; reject(out, "open failure rejected");
    Storage = {}; reject(out, "missing file rejected");
    input(tuple); Storage.failClose = true; reject(out, "close failure rejected");
    input(tuple); failAllocation = allocationCalls + 1; reject(out, "allocation failure rejected"); failAllocation = 0;
    input(tuple); reject(out, "orientation mismatch rejected", true, {16,2,2,GfxRenderer::Portrait});
    input(tuple); reject(out, "width mismatch rejected", true, {15,2,2,geometry.orientation});
    input(tuple); reject(out, "height mismatch rejected", true, {16,3,2,geometry.orientation});
    input(tuple); reject(out, "stride mismatch rejected", true, {16,2,3,geometry.orientation});
    input(tuple); reject(out, "invalid expected geometry rejected", true, {0,2,2,geometry.orientation});
    input(tuple); reject(out, "overflowing expected lengths rejected", true, {65535,65535,65535,geometry.orientation});
    auto corruptM = tuple; corruptM.back() ^= 1; input(corruptM);
    reject(out, "BW fallback validates stored selectors", false);
    input(tuple); Storage.shortRead = 2; reject(out, "BW fallback requires complete payload", false);

    input(bw);
    require(SleepFrame::save("/sleep", frame, true) && Storage.bytes == tuple,
            "save emits canonical tuple literal including header checksum");
    for (int write=1; write<=4; ++write) {
      input(tuple); Storage.shortWrite = write;
      require(!SleepFrame::save("/sleep", frame, true), "short write fails save");
      require(!Storage.present, "short write removes failed and stale payload");
      reject(out, "failed save cannot load next boot");
    }
    for (int failure=0; failure<3; ++failure) {
      input(tuple);
      Storage.failOpen = failure == 0; Storage.failSync = failure == 1; Storage.failClose = failure == 2;
      require(!SleepFrame::save("/sleep", frame, true) && !Storage.present,
              "open sync and close failure remove persisted frame");
    }
    input(tuple); Storage.failRemove = true;
    require(!SleepFrame::save("/sleep", frame, true) && Storage.opens == 0 && Storage.bytes == tuple,
            "stale removal failure stops before opening save");
    for (int failure=0; failure<7; ++failure) {
      Snapshot invalid = frame;
      if (failure == 0) invalid.valid = false;
      if (failure == 1) invalid.physicalByteX = 1;
      if (failure == 2) invalid.kind = static_cast<Kind>(42);
      if (failure == 3) invalid.panelWidth = 17;
      if (failure == 4) invalid.planes = {frame.planes.b(), frame.planes.l(), frame.planes.m(), 16, 2, 2, 1};
      if (failure == 5) invalid.planes = {frame.planes.b(), {frame.planes.l().data,3}, frame.planes.m(), 16, 2, 2};
      if (failure == 6) invalid.planes = {frame.planes.b(), frame.planes.l(), {nullptr,4}, 16, 2, 2};
      input(tuple);
      require(!SleepFrame::save("/sleep", invalid, true) && !Storage.present,
              "invalid source fails save and removes stale payload");
    }
    input(tuple);
    require(!SleepFrame::save("/sleep", frame, false) && !Storage.present, "save rejects policy mismatch");
    input(tuple);
    require(SleepFrame::load("/sleep", geometry, false, out), "validated tuple permits explicit BW fallback");
    frame = out.snapshot();
    require(frame.valid && frame.kind == Kind::LegacyBw && !frame.planes.l().data && !frame.planes.m().data &&
            frame.planes.l().capacity == 0 && frame.planes.m().capacity == 0 &&
            std::memcmp(frame.planes.b().data, tuple.data()+32, 4) == 0,
            "BW fallback exposes only B without manufactured selectors");
    input(tuple);
    require(SleepFrame::save("/sleep", frame, false) && Storage.bytes == bw, "save emits canonical BW literal");
    input(bw);
    require(!SleepFrame::save("/sleep", frame, true) && !Storage.present, "enabled save rejects LegacyBw source");
    input(bw);
    require(SleepFrame::load("/sleep", geometry, false, out) && out.snapshot().kind == Kind::LegacyBw &&
            !out.snapshot().planes.l().data && !out.snapshot().planes.m().data &&
            std::memcmp(out.snapshot().planes.b().data, bw.data()+32, 4) == 0,
            "versioned BW loads only when retention unavailable");
  }
  require(liveAllocations == 0, "loaded frames release all storage");
  std::puts("PASS: sleep frame literal format, validation, policy and storage failures");
}
'''


class SleepFrameTest(unittest.TestCase):
    def test_production_module(self):
        with tempfile.TemporaryDirectory(prefix="sleep-frame-") as name:
            work = Path(name)
            for filename in ("Arduino.h", "EInkDisplay.h", "esp_heap_caps.h"):
                (work / filename).write_text(renderer_test.STUBS[filename])
            (work / "HalStorage.h").write_text(STORAGE)
            harness = work / "harness.cpp"
            harness.write_text(HARNESS)
            cxx = shlex.split(os.environ.get("CXX", "clang++"))
            cc = shlex.split(os.environ.get("CC", "clang"))
            includes = [f"-I{work}", *(f"-I{ROOT / path}" for path in (
                "lib/SleepFrame", "lib/GfxRenderer", "lib/EpdFont", "lib/hal", "lib/miniz/src"))]
            flags = ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                     "-fno-omit-frame-pointer"]
            miniz = work / "miniz.o"
            subprocess.run([*cc, "-std=c11", *flags, *includes, "-c",
                            str(ROOT / "lib/miniz/src/miniz_impl.c"), "-o", str(miniz)], check=True)
            binary = work / "sleep-frame"
            command = [*cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", *flags, *includes,
                       str(SOURCE), str(harness), str(miniz), "-o", str(binary)]
            subprocess.run(command, check=True)
            subprocess.run([str(binary)], check=True)

            source = SOURCE.read_text()
            mutants = [
                ("skip checksum", "valid = crc == read32(header.data() + CHECKSUM_OFFSET);",
                 "(void)crc; valid = true;", "corruption rejected before publication"),
                ("accept BW with retention", "if (retentionEnabled && header[6] != TUPLE_KIND) return false;",
                 "(void)retentionEnabled;", "retained mode rejects LegacyBw"),
                ("ignore short plane writes", "written = file.write(planes[i].data, bytes) == bytes;",
                 "written = file.write(planes[i].data, bytes) <= bytes;", "short write fails save"),
                ("publish before validation", "if (!valid || !closed) return false;",
                 "if (!valid || !closed) { out = std::move(candidate); return false; }",
                 "failed load preserves prior descriptor and every destination byte"),
                ("skip selector capacity", "if (!planes[i].data || planes[i].capacity < bytes) return false;",
                 "if (!planes[i].data || (i == 0 && planes[i].capacity < bytes)) return false;",
                 "invalid source fails save and removes stale payload"),
                ("skip geometry match", "read16(header.data() + 8) != expected.width",
                 "false", "checksummed invalid metadata rejected"),
            ]
            for label, anchor, replacement, failure in mutants:
                self.assertEqual(source.count(anchor), 1, f"mutant anchor changed: {label}")
                mutated = work / "SleepFrame-mutant.cpp"
                mutated.write_text(source.replace(anchor, replacement))
                mutant_command = [str(mutated) if part == str(SOURCE) else part for part in command]
                subprocess.run(mutant_command, check=True)
                result = subprocess.run([str(binary)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, f"survived mutant: {label}")
                self.assertIn(f"{failure}: expected true, actual false", result.stderr, label)
                print(f"EXPECTED MUTANT FAILURE ({label}): {result.stderr.strip()}")
            self.assertEqual(SOURCE.read_text(), source, "production source changed during mutation tests")


if __name__ == "__main__":
    unittest.main()
