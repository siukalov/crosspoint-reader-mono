#!/usr/bin/env python3
"""Run the real diagnostic and SSD1683 driver against a recording host bus."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "freeink-sdk/libs/display/FreeInkDisplay/src/driver"

STUBS = {
    "Arduino.h": r"""
#pragma once
#include <cstdint>
#include <cstddef>
#define DRAM_ATTR
#define HIGH 1
#define LOW 0
inline unsigned long hostMillis = 0;
inline unsigned long millis() { return hostMillis; }
inline void delay(unsigned long ms) { hostMillis += ms; }
inline int digitalRead(int) { return LOW; }
struct HostSerial { template <class... T> void printf(const char*, T...) {} };
inline HostSerial Serial;
""",
    "SPI.h": "#pragma once\nstruct SPISettings {};\n",
    "BoardConfig.h": r"""
#pragma once
#include <cstdint>
namespace BoardConfig {
struct Orientation { bool mirrorX = true; bool mirrorY = true; };
struct Config { uint32_t displaySpiHz = 20000000; Orientation orientation; };
inline Config ACTIVE;
}
""",
    "esp_heap_caps.h": r"""
#pragma once
#include <cstdlib>
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
inline unsigned hostAllocationCalls = 0;
inline void* heap_caps_malloc(size_t count, int) { ++hostAllocationCalls; return malloc(count); }
inline void heap_caps_free(void* pointer) { free(pointer); }
""",
    "HalDisplay.h": r"""
#pragma once
#include "Ssd1683Driver.h"
inline freeink::EpdBus hostBus;
class HalDisplay {
 public:
  uint8_t frame[48000];
  uint16_t getDisplayWidth() const { return 800; }
  uint16_t getDisplayHeight() const { return 480; }
  uint8_t* getFrameBuffer() { return frame; }
  void waitRefreshComplete() {}
  void beginDisplayWork() { freeink::ssd1683Driver().beginDisplayWork(); }
  void copyGrayscaleBuffers(const uint8_t* lsb, const uint8_t* msb) {
    freeink::ssd1683Driver().copyGrayscaleLsb(hostBus, lsb);
    freeink::ssd1683Driver().copyGrayscaleMsb(hostBus, msb);
  }
  void displayGrayCalibration(uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
    freeink::ssd1683Driver().displayGrayCalibration(hostBus, frame, x, y, w, h);
  }
  bool displayCommitted() { return freeink::ssd1683Driver().displayCommitted(); }
};
inline HalDisplay display;
""",
}

HARNESS = r"""
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <vector>
#include "BoardConfig.h"
#include "HalDisplay.h"
#include "PanelDiagnostic.h"
#include "esp_heap_caps.h"

std::vector<uint8_t> ram24, ram26;
std::vector<uint8_t> controls;
uint8_t currentCommand = 0;
unsigned chunks = 0;
bool inTransaction = false;

void require(bool condition, const char* message) {
  if (!condition) { std::fprintf(stderr, "FAIL %s\n", message); std::exit(1); }
}

namespace freeink {
void EpdBus::reset(uint16_t) {}
void EpdBus::cmd(uint8_t command) {
  currentCommand = command;
  if (command == 0x24) ram24.clear();
  if (command == 0x26) ram26.clear();
}
void EpdBus::data(uint8_t value) { if (currentCommand == 0x22) controls.push_back(value); }
void EpdBus::data(const uint8_t* bytes, uint16_t count) {
  if (currentCommand != 0x24 && currentCommand != 0x26) return;
  auto& ram = currentCommand == 0x24 ? ram24 : ram26;
  ram.insert(ram.end(), bytes, bytes + count);
  ++chunks;
}
void EpdBus::cmdData(uint8_t command, const uint8_t* bytes, uint16_t count) { cmd(command); data(bytes, count); }
void EpdBus::waitBusy(const char*) {}
void EpdBus::waitRefreshComplete(const char*) { hostMillis += 137; }
void EpdBus::beginTxn() { require(!inTransaction, "nested transaction"); inTransaction = true; }
void EpdBus::endTxn() { require(inTransaction, "unbalanced transaction"); inTransaction = false; }
void EpdBus::rawWriteBytes(const uint8_t* bytes, uint16_t count) {
  require(inTransaction, "rotated write outside transaction"); data(bytes, count);
}
}

bool bit(const uint8_t* plane, unsigned x, unsigned y) { return (plane[y * 100 + x / 8] & (128 >> (x % 8))) != 0; }
void checkPixel(const std::vector<uint8_t>& ram, unsigned x, unsigned y, bool value, bool rotated) {
  const unsigned px = rotated ? 799 - x : x;
  const unsigned py = rotated ? 479 - y : y;
  require(bit(ram.data(), px, py) == value, "post-transform pixel differs from literal expected selector");
}

int main(int argc, char** argv) {
  require(argc == 2, "missing output directory");
  std::array<uint8_t, 48000> bw, lsb, msb;
  PanelDiagnostic::writeFixture(bw.data(), lsb.data(), msb.data());
  const bool expected[4][3] = {{true, false, false}, {true, false, true}, {false, true, true}, {false, false, false}};
  for (unsigned i = 0; i < 4; ++i) {
    const unsigned x = 64 + i * 176;
    require(bit(bw.data(), x, 100) == expected[i][0], "fixture BW swatch literal");
    require(bit(lsb.data(), x, 100) == expected[i][1], "fixture LSB swatch literal");
    require(bit(msb.data(), x, 100) == expected[i][2], "fixture MSB swatch literal");
  }
  require(bw[8 * 100 + 1] == 0 && bw[8 * 100 + 4] == 0x0F && bw[12 * 100 + 1] == 0x0F,
          "top-left corner literal bytes");
  require(!bit(bw.data(), 760, 8) && bit(bw.data(), 776, 8) && !bit(bw.data(), 780, 24), "top-right corner");
  require(!bit(bw.data(), 20, 456) && bit(bw.data(), 32, 456) && !bit(bw.data(), 32, 468), "bottom-left corner");
  require(!bit(bw.data(), 780, 444) && bit(bw.data(), 780, 456) && !bit(bw.data(), 760, 468), "bottom-right corner");
  require(!bit(bw.data(), 48, 320) && bit(bw.data(), 54, 320), "WHITE label literal glyph pixels");

  freeink::ssd1683Driver().begin(hostBus);
  for (bool rotated : {true, false}) {
    BoardConfig::ACTIVE.orientation.mirrorX = rotated;
    BoardConfig::ACTIVE.orientation.mirrorY = rotated;
    chunks = 0;
    controls.clear();
    const unsigned allocationCalls = hostAllocationCalls;
    require(PanelDiagnostic::renderFixture(), "diagnostic capture valid: expected true, actual false");
    require(hostAllocationCalls - allocationCalls == (rotated ? 1u : 0u),
            "capture storage: expected one initial allocation and no repeat allocations");
    const auto meta = PanelDiagnostic::metadata();
    require(meta.valid && meta.width == 800 && meta.height == 480 && meta.planeBytes == 48000, "capture dimensions");
    require(meta.generation == (rotated ? 1u : 2u) && meta.driverGeneration != 0, "capture generations");
    require(meta.activationCount == 1 && meta.busyMs == 137 && meta.renderMs == 137, "activation and render milliseconds");
    require(controls.size() == 1 && controls[0] == (rotated ? 0xCC : 0x0C) && meta.control == controls[0],
            "unchanged three-tone update control");
    require(chunks == (rotated ? 6u : 2u) && !inTransaction, "original plane chunk/transaction boundaries");
    require(ram24.size() == 48000 && ram26.size() == 48000, "complete bus planes");
    const bool encoded24[] = {true, false, false, true};
    const bool encoded26[] = {false, true, true, true};
    for (unsigned i = 0; i < 4; ++i) {
      checkPixel(ram24, 64 + i * 176, 100, encoded24[i], rotated);
      checkPixel(ram26, 64 + i * 176, 100, encoded26[i], rotated);
    }
    for (unsigned y = 0; y < 480; ++y) {
      for (unsigned x = 0; x < 800; ++x) {
        const bool gray = bit(lsb.data(), x, y) || bit(msb.data(), x, y);
        checkPixel(ram24, x, y, !gray, rotated);
        checkPixel(ram26, x, y, gray || !bit(bw.data(), x, y), rotated);
      }
    }
    for (uint8_t command : {0x24, 0x26}) {
      const auto& expectedBytes = command == 0x24 ? ram24 : ram26;
      std::array<uint8_t, 48000> captured;
      size_t offset = 0;
      while (offset < captured.size()) {
        const size_t count = PanelDiagnostic::readCapture(command, meta.generation, offset, captured.data() + offset, 511);
        require(count != 0 && count <= 511, "bounded capture chunk");
        offset += count;
      }
      require(std::equal(captured.begin(), captured.end(), expectedBytes.begin()), "capture equals actual bus stream");
      require(PanelDiagnostic::readCapture(command, meta.generation - 1, 0, captured.data(), 1) == 0, "stale generation rejected");
      require(PanelDiagnostic::readCapture(command, meta.generation, 48000, captured.data(), 1) == 0, "past-end read rejected");
      const auto path = std::string(argv[1]) + (rotated ? "/rotated-" : "/native-") + std::to_string(command);
      std::ofstream file(path, std::ios::binary);
      file.write(reinterpret_cast<const char*>(captured.data()), captured.size());
    }
    std::printf("{\"rotated\":%s,\"crc24\":%u,\"crc26\":%u}\n", rotated ? "true" : "false", meta.crc24, meta.crc26);
  }
  const auto frozen = PanelDiagnostic::metadata();
  std::array<uint8_t, 16> before, after;
  require(PanelDiagnostic::readCapture(0x24, frozen.generation, 0, before.data(), 16) == 16, "frozen capture available");
  display.beginDisplayWork();
  memset(display.frame, 0, 48000);
  freeink::ssd1683Driver().display(hostBus, display.frame, nullptr, freeink::RefreshMode::Full, false);
  require(PanelDiagnostic::readCapture(0x24, frozen.generation, 0, after.data(), 16) == 16 && before == after,
          "ordinary rendering cannot replace frozen diagnostic capture");
  require(PanelDiagnostic::metadata().activationCount == frozen.activationCount, "disabled activation hook ignores ordinary renders");
  require(PanelDiagnostic::readCapture(0x25, frozen.generation, 0, after.data(), 1) == 0, "invalid RAM command rejected");
  PanelDiagnostic::releaseCapture();
  require(!PanelDiagnostic::metadata().valid, "released capture invalid");
  require(PanelDiagnostic::readCapture(0x24, frozen.generation, 0, after.data(), 1) == 0, "released capture unreadable");
}
"""


class PanelDiagnosticTest(unittest.TestCase):
    def test_real_driver_fixture_capture_and_rotation(self):
        with tempfile.TemporaryDirectory(prefix="panel-diagnostic-") as temporary:
            directory = Path(temporary)
            for name, content in STUBS.items():
                (directory / name).write_text(content)
            harness = directory / "test.cpp"
            harness.write_text(HARNESS)
            miniz_object = directory / "miniz.o"
            compiled = subprocess.run([
                os.environ.get("CC", "cc"), "-c", str(ROOT / "lib/miniz/src/miniz_impl.c"),
                "-o", str(miniz_object),
            ], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            executable = directory / "test"
            command = [
                os.environ.get("CXX", "c++"), "-std=c++17", "-DENABLE_SERIAL_LOG=1",
                "-I", str(directory), "-I", str(DRIVER),
                "-I", str(ROOT / "lib/PanelDiagnostic"),
                "-I", str(ROOT / "lib/miniz/src"),
                str(harness), str(ROOT / "lib/PanelDiagnostic/PanelDiagnostic.cpp"),
                str(DRIVER / "Ssd1683Driver.cpp"), str(miniz_object), "-o", str(executable),
            ]
            compiled = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable), str(directory)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for line in result.stdout.splitlines():
                metadata = json.loads(line)
                prefix = "rotated" if metadata["rotated"] else "native"
                self.assertEqual(metadata["crc24"], zlib.crc32((directory / f"{prefix}-36").read_bytes()))
                self.assertEqual(metadata["crc26"], zlib.crc32((directory / f"{prefix}-38").read_bytes()))


if __name__ == "__main__":
    unittest.main()
