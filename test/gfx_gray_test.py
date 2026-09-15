#!/usr/bin/env python3
"""Compile the production renderer and font stack against host hardware boundaries."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "lib/GfxRenderer/GfxRenderer.cpp"
GRAY_FRAME = ROOT / "lib/GfxRenderer/GrayFrame.h"

STUBS = {
    "Arduino.h": r"""
#pragma once
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
inline unsigned long millis() { return 0; }
inline unsigned long micros() { return 0; }
inline void delay(unsigned long) {}
struct HostEsp {
  void restart() { std::abort(); }
  size_t getFreeHeap() const { return 1024 * 1024; }
};
inline HostEsp ESP;
""",
    "EInkDisplay.h": r"""
#pragma once
#include <cstdint>
class EInkDisplay {
 public:
  static constexpr uint16_t DISPLAY_WIDTH = 800;
  static constexpr uint16_t DISPLAY_HEIGHT = 480;
};
""",
    "HalGPIO.h": "#pragma once\n",
    "Logging.h": r"""
#pragma once
#include <Arduino.h>
#define LOG_DBG(...) ((void)0)
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
""",
    "HalStorage.h": r"""
#pragma once
#include <Arduino.h>
#include <string>
class HalFile {
 public:
  bool seekSet(size_t) { std::abort(); }
  int read(void*, size_t) { std::abort(); }
  bool close() { std::abort(); }
};
struct HostStorage {
  bool openFileForRead(const char*, const std::string&, HalFile&) { std::abort(); }
};
inline HostStorage Storage;
""",
}

HARNESS = r"""
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <GfxRenderer.h>
#include <DirectPixelWriter.h>
#include <EpdFont.h>

static std::array<uint8_t, 48000> physical;
HalDisplay::HalDisplay() = default;
HalDisplay::~HalDisplay() = default;
uint8_t* HalDisplay::getFrameBuffer() const { return physical.data(); }
uint16_t HalDisplay::getDisplayWidth() const { return 800; }
uint16_t HalDisplay::getDisplayHeight() const { return 480; }
uint16_t HalDisplay::getDisplayWidthBytes() const { return 100; }
uint32_t HalDisplay::getBufferSize() const { return 48000; }
void HalDisplay::clearScreen(uint8_t color) const { physical.fill(color); }

static void expect(const uint8_t* actual, const uint8_t* expected, size_t size, const char* name) {
  for (size_t i = 0; i < size; ++i) {
    if (actual[i] != expected[i]) {
      std::fprintf(stderr, "%s: byte %zu expected %02X, actual %02X\n", name, i, expected[i], actual[i]);
      std::exit(1);
    }
  }
}

int main() {
  HalDisplay hal;
  GfxRenderer renderer(hal);
  renderer.begin();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  physical.fill(0x55);
  renderer.clearScreen();
  std::array<uint8_t, 48000> expected;
  expected.fill(0xFF);
  expect(physical.data(), expected.data(), expected.size(), "HAL clear");

  renderer.drawPixel(1, 2);
  expected[200] = 0xBF;
  expect(physical.data(), expected.data(), expected.size(), "physical pixel");

  std::array<uint8_t, 300> strip;
  strip.fill(0xAA);
  renderer.beginStripTarget(strip.data(), 2, 3);
  renderer.clearScreen(0);
  std::array<uint8_t, 300> expectedStrip{};
  expect(strip.data(), expectedStrip.data(), strip.size(), "strip clear");
  expect(physical.data(), expected.data(), expected.size(), "strip preserves HAL");
  renderer.drawPixel(1, 2, false);
  expectedStrip[0] = 0x40;
  renderer.drawPixel(0, 1, false);
  expect(strip.data(), expectedStrip.data(), strip.size(), "strip pixel and clipping");
  expect(physical.data(), expected.data(), expected.size(), "strip pixel preserves HAL");

  renderer.clearScreen();
  expectedStrip.fill(0xFF);
  DirectPixelWriter direct;
  direct.init(renderer);
  direct.beginRow(3);
  direct.writePixel(0, 0);
  direct.writePixel(1, 3);
  expectedStrip[100] = 0x7F;
  expect(strip.data(), expectedStrip.data(), strip.size(), "direct writer targets strip");
  expect(physical.data(), expected.data(), expected.size(), "direct writer preserves HAL");

  renderer.endStripTarget();
  renderer.drawPixel(0, 0);
  expected[0] = 0x7F;
  expect(physical.data(), expected.data(), expected.size(), "return to HAL");
  expect(strip.data(), expectedStrip.data(), strip.size(), "return preserves strip");

  renderer.setOrientation(GfxRenderer::Portrait);
  renderer.drawPixel(0, 0);
  expected[47900] = 0x7F;
  expect(physical.data(), expected.data(), expected.size(), "portrait physical pixel");

  EpdGlyph glyph{};
  glyph.width = 4;
  glyph.height = 1;
  glyph.advanceX = 64;
  glyph.dataLength = 1;
  const uint8_t bitmap[] = {0xF0};
  const EpdUnicodeInterval interval{65, 65, 0};
  EpdFontData data{};
  data.bitmap = bitmap;
  data.glyph = &glyph;
  data.intervals = &interval;
  data.intervalCount = 1;
  EpdFont font(&data);
  if (font.getGlyph(65) != &glyph) return 2;
  std::puts("PASS: actual renderer HAL/strip binding, pixel transforms, DirectPixelWriter and font lookup");
}
"""

FRAME_HARNESS = r"""
#include <array>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <GrayFrame.h>

static void require(bool condition, const char* name) {
  if (!condition) {
    std::fprintf(stderr, "%s: expected true, actual false\n", name);
    std::exit(1);
  }
}

template <size_t N>
static void expect(const std::array<uint8_t, N>& actual, const std::array<uint8_t, N>& expected,
                   const char* name) {
  for (size_t i = 0; i < N; ++i) {
    if (actual[i] != expected[i]) {
      std::fprintf(stderr, "%s: byte %zu expected %02X, actual %02X\n", name, i, expected[i], actual[i]);
      std::exit(1);
    }
  }
}

static void testShades() {
  std::array<uint8_t, 1> b{}, l{}, m{};
  GrayFrame frame({b.data(), 1}, {l.data(), 1}, {m.data(), 1}, 8, 1, 1);
  require(frame.valid(), "valid plane views");
  for (size_t x = 0; x < 8; ++x) require(frame.writeShade(x, 0, x % 4), "write shade");
  expect(b, {0xCC}, "canonical B");
  expect(l, {0x22}, "canonical L");
  expect(m, {0x66}, "canonical M");
  const uint8_t shades[] = {0, 1, 2, 3, 0, 1, 2, 3};
  for (size_t x = 0; x < 8; ++x) {
    uint8_t darkness = 99;
    require(frame.shade(x, 0, darkness) && darkness == shades[x], "read canonical shade");
  }

  require(frame.writeMasked(0, 0, 0x60, 0), "white masked overwrite");
  expect(b, {0xEC}, "opaque white B");
  expect(l, {0x02}, "opaque white L");
  expect(m, {0x06}, "opaque white M");
  b[0] = 0xCC; l[0] = 0x22; m[0] = 0x66;
  require(frame.writeMasked(0, 0, 0x60, 3), "black masked overwrite");
  expect(b, {0x8C}, "opaque black B");
  expect(l, {0x02}, "opaque black L");
  expect(m, {0x06}, "opaque black M");
  b[0] = 0xCC; l[0] = 0x22; m[0] = 0x66;
  require(frame.invert(), "invert");
  expect(b, {0x33}, "inverted B");
  expect(l, {0x44}, "inverted L");
  expect(m, {0x66}, "inverted M");
  require(frame.invert(), "second inversion");
  expect(b, {0xCC}, "restored B");
  expect(l, {0x22}, "restored L");
  expect(m, {0x66}, "restored M");
  require(frame.writeMasked(0, 0, 0, 3), "empty mask");
  require(!frame.writeShade(8, 0, 0), "reject outside pixel");
  require(!frame.writeShade(0, 1, 0), "reject outside row");
  require(!frame.writeShade(0, 0, 4), "reject invalid shade");
  require(!frame.writeCoverage(0, 0, 4, true), "reject invalid coverage");
  require(!frame.writeMasked(1, 0, 0xFF, 0), "reject outside byte");
  expect(b, {0xCC}, "rejected writes preserve B");
  expect(l, {0x22}, "rejected writes preserve L");
  expect(m, {0x66}, "rejected writes preserve M");

  require(frame.fill(2) && frame.writeShade(0, 0, 1), "image shade replaces dark destination");
  expect(b, {0x80}, "image replacement B");
  expect(l, {0x7F}, "image replacement L");
  expect(m, {0xFF}, "image replacement M");
}

static void testCoverage() {
  const uint8_t black[4][4] = {{0, 1, 2, 3}, {1, 2, 2, 3}, {2, 2, 3, 3}, {3, 3, 3, 3}};
  const uint8_t white[4][4] = {{0, 0, 0, 0}, {1, 1, 0, 0}, {2, 1, 1, 0}, {3, 2, 1, 0}};
  const uint8_t canonical[4][3] = {{0xFF, 0, 0}, {0xFF, 0, 0xFF}, {0, 0xFF, 0xFF}, {0, 0, 0}};
  std::array<uint8_t, 1> b{}, l{}, m{};
  GrayFrame frame({b.data(), 1}, {l.data(), 1}, {m.data(), 1}, 8, 1, 1);
  for (bool blackInk : {false, true}) {
    for (uint8_t destination = 0; destination < 4; ++destination) {
      for (uint8_t coverage = 0; coverage < 4; ++coverage) {
        require(frame.fill(destination), "fill coverage destination");
        for (size_t x = 0; x < 8; ++x) {
          require(frame.writeCoverage(x, 0, coverage, blackInk), "paint coverage");
        }
        const uint8_t result = blackInk ? black[destination][coverage] : white[destination][coverage];
        expect(b, {canonical[result][0]}, "coverage B");
        expect(l, {canonical[result][1]}, "coverage L");
        expect(m, {canonical[result][2]}, "coverage M");
      }
    }
  }
}

static void testRowsAndCopy() {
  std::array<uint8_t, 4> sourceB{0xCC, 0, 0xFF, 0xCC};
  std::array<uint8_t, 4> sourceL{0x22, 0, 0, 0x22};
  std::array<uint8_t, 4> sourceM{0x66, 0, 0, 0x66};
  GrayFrame source({sourceB.data(), 4}, {sourceL.data(), 4}, {sourceM.data(), 4}, 16, 2, 2, 9);
  std::array<uint8_t, 5> b{0xFF, 0xFF, 0xFF, 0xFF, 0x55};
  std::array<uint8_t, 5> l{0, 0, 0, 0, 0xAA};
  std::array<uint8_t, 5> m{0, 0, 0, 0, 0xCC};
  GrayFrame frame({b.data(), 4}, {l.data(), 4}, {m.data(), 4}, 16, 2, 2, 20);
  require(frame.writeShade(9, 21, 2), "second physical row");
  require(!frame.writeShade(0, 19, 3), "reject before row origin");
  require(!frame.writeShade(0, 22, 3), "reject after row extent");
  expect(b, {0xFF, 0xFF, 0xFF, 0xBF, 0x55}, "disjoint rows B");
  expect(l, {0, 0, 0, 0x40, 0xAA}, "disjoint rows L");
  expect(m, {0, 0, 0, 0x40, 0xCC}, "disjoint rows M");

  require(frame.copyMasked(source, 0, 9, 0, 20, 0x60), "masked tuple copy");
  expect(b, {0xDF, 0xFF, 0xFF, 0xBF, 0x55}, "masked copy B");
  expect(l, {0x20, 0, 0, 0x40, 0xAA}, "masked copy L");
  expect(m, {0x60, 0, 0, 0x40, 0xCC}, "masked copy M");
  require(frame.copyMasked(source, 0, 10, 0, 20, 0x60), "copy white clears selectors");
  expect(b, {0xFF, 0xFF, 0xFF, 0xBF, 0x55}, "white copy B");
  expect(l, {0, 0, 0, 0x40, 0xAA}, "white copy L");
  expect(m, {0, 0, 0, 0x40, 0xCC}, "white copy M");
  expect(sourceB, {0xCC, 0, 0xFF, 0xCC}, "copy preserves source B");
  expect(sourceL, {0x22, 0, 0, 0x22}, "copy preserves source L");
  expect(sourceM, {0x66, 0, 0, 0x66}, "copy preserves source M");

  for (size_t shortPlane = 0; shortPlane < 3; ++shortPlane) {
    GrayFrame invalid({b.data(), shortPlane == 0 ? 3u : 4u}, {l.data(), shortPlane == 1 ? 3u : 4u},
                      {m.data(), shortPlane == 2 ? 3u : 4u}, 16, 2, 2, 20);
    require(!invalid.valid(), "reject short plane capacity");
    require(!invalid.writeShade(0, 20, 3), "short shade rejected");
    require(!invalid.writeCoverage(0, 20, 3, true), "short coverage rejected");
    require(!invalid.writeMasked(0, 20, 0xFF, 3), "short masked write rejected");
    require(!invalid.fill(3) && !invalid.invert(), "short frame operations rejected");
    require(!invalid.copyMasked(source, 0, 9, 0, 20, 0xFF), "short destination copy rejected");
    require(!frame.copyMasked(invalid, 0, 20, 0, 20, 0xFF), "short source copy rejected");
    expect(b, {0xFF, 0xFF, 0xFF, 0xBF, 0x55}, "invalid capacity preserves B");
    expect(l, {0, 0, 0, 0x40, 0xAA}, "invalid capacity preserves L");
    expect(m, {0, 0, 0, 0x40, 0xCC}, "invalid capacity preserves M");
  }
}

static void testGeometry() {
  std::array<uint8_t, 3> b{0xFF, 0xFF, 0xA5}, l{0, 0, 0x5A}, m{0, 0, 0x55};
  const GrayFrame::Plane bp{b.data(), 2}, lp{l.data(), 2}, mp{m.data(), 2};
  const size_t maximum = std::numeric_limits<size_t>::max();
  require(!GrayFrame().valid(), "unbound frame invalid");
  require(!GrayFrame(bp, lp, mp, 0, 1, 2).valid(), "zero width invalid");
  require(!GrayFrame(bp, lp, mp, 16, 0, 2).valid(), "zero rows invalid");
  require(!GrayFrame(bp, lp, mp, 16, 1, 0).valid(), "zero stride invalid");
  require(!GrayFrame(bp, lp, mp, 17, 1, 2).valid(), "narrow stride invalid");
  require(!GrayFrame(bp, lp, mp, 16, 2, 2, maximum).valid(), "row origin overflow invalid");
  require(!GrayFrame({b.data(), maximum}, {l.data(), maximum}, {m.data(), maximum}, 16, maximum, 2).valid(),
          "plane size overflow invalid");
  require(!GrayFrame(bp, {}, mp, 8, 1, 1).valid(), "missing selector invalid");
  GrayFrame padded(bp, lp, mp, 9, 1, 2);
  require(padded.writeShade(8, 0, 2), "partial final byte pixel");
  require(!padded.writeShade(9, 0, 3), "pixel padding clipped");
  expect(b, {0xFF, 0x7F, 0xA5}, "partial byte B");
  expect(l, {0, 0x80, 0x5A}, "partial byte L");
  expect(m, {0, 0x80, 0x55}, "partial byte M");
  require(padded.fill(1) && padded.invert(), "full operations include byte padding");
  expect(b, {0, 0, 0xA5}, "padded inversion B");
  expect(l, {0xFF, 0xFF, 0x5A}, "padded inversion L");
  expect(m, {0xFF, 0xFF, 0x55}, "padded inversion M");
}

int main() {
  testShades();
  testCoverage();
  testRowsAndCopy();
  testGeometry();
  std::puts("PASS: GrayFrame shade, coverage, masked writes/copies, inversion, geometry and capacity");
}
"""

CPP_SOURCES = [
    "lib/EpdFont/EpdFont.cpp",
    "lib/EpdFont/EpdFontFamily.cpp",
    "lib/EpdFont/FontDecompressor.cpp",
    "lib/EpdFont/SdCardFont.cpp",
    "lib/GfxRenderer/FontCacheManager.cpp",
    "lib/InflateReader/InflateReader.cpp",
    "lib/Memory/BuildScratch.cpp",
    "lib/MiniBidi/BidiUtils.cpp",
    "lib/Utf8/Utf8.cpp",
]
INCLUDES = [
    "lib/GfxRenderer", "lib/EpdFont", "lib/hal", "lib/Memory", "lib/MiniBidi",
    "lib/Utf8", "lib/InflateReader", "lib/uzlib/src", "lib/Epub/Epub/converters",
]


class GrayFrameTest(unittest.TestCase):
    def test_plane_operations(self):
        with tempfile.TemporaryDirectory(prefix="gray-frame-") as name:
            work = Path(name)
            harness = work / "frame.cpp"
            harness.write_text(FRAME_HARNESS)
            cxx = shlex.split(os.environ.get("CXX", "clang++"))
            common = [*cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-g",
                      "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
            source = GRAY_FRAME.read_text()
            original = "    replace(l_.data[offset], mask, darkness == 2 ? 0xFF : 0);"
            self.assertEqual(source.count(original), 1, "stale-gray mutant anchor changed")
            (work / "GrayFrame.h").write_text(source.replace(original, "    if (darkness != 0)\n" + original))
            mutant = work / "frame-mutant"
            subprocess.run([*common, f"-I{work}", str(harness), "-o", str(mutant)], check=True)
            result = subprocess.run([str(mutant)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, "stale gray under white survived")
            self.assertIn("opaque white L: byte 0 expected 02, actual 22", result.stderr)
            print("EXPECTED MUTANT FAILURE:", result.stderr.strip())

            binary = work / "frame"
            subprocess.run([*common, f"-I{GRAY_FRAME.parent}", str(harness), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)
            self.assertEqual(GRAY_FRAME.read_text(), source, "production header changed during test")


class RendererSeamTest(unittest.TestCase):
    def test_actual_renderer(self):
        with tempfile.TemporaryDirectory(prefix="gfx-gray-") as name:
            work = Path(name)
            for filename, content in STUBS.items():
                (work / filename).write_text(content)
            harness = work / "harness.cpp"
            harness.write_text(HARNESS)
            cxx = shlex.split(os.environ.get("CXX", "clang++"))
            cc = shlex.split(os.environ.get("CC", "clang"))
            includes = [f"-I{work}", *(f"-I{ROOT / path}" for path in INCLUDES)]
            common = ["-O1", "-g", "-ffunction-sections", "-fdata-sections", *includes]
            objects = []
            sources = [RENDERER, *(ROOT / path for path in CPP_SOURCES), harness]
            sources += sorted((ROOT / "lib/uzlib/src").glob("*.c"))
            sources.append(ROOT / "lib/MiniBidi/minibidi.c")
            for index, source in enumerate(sources):
                obj = work / f"source-{index}.o"
                compiler = cc if source.suffix == ".c" else cxx
                standard = "-std=c11" if source.suffix == ".c" else "-std=c++17"
                subprocess.run([*compiler, standard, *common, "-c", str(source), "-o", str(obj)], check=True)
                objects.append(str(obj))
            binary = work / "gfx-gray"
            dead_strip = "-Wl,-dead_strip" if sys.platform == "darwin" else "-Wl,--gc-sections"
            subprocess.run([*cxx, dead_strip, *objects, "-o", str(binary)], check=True)

            source = RENDERER.read_text()
            original = "    return;\n  }\n  display.clearScreen(color);"
            self.assertEqual(source.count(original), 1, "strip-clear mutant anchor changed")
            mutated = work / "GfxRenderer-mutant.cpp"
            mutated.write_text(source.replace(original, "  }\n  display.clearScreen(color);"))
            mutant_object = work / "renderer-mutant.o"
            subprocess.run([*cxx, "-std=c++17", *common, "-c", str(mutated), "-o", str(mutant_object)], check=True)
            mutant_binary = work / "gfx-gray-mutant"
            subprocess.run([*cxx, dead_strip, str(mutant_object), *objects[1:], "-o", str(mutant_binary)], check=True)
            result = subprocess.run([str(mutant_binary)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, "strip clear incorrectly touching HAL survived")
            self.assertIn("strip preserves HAL: byte 0 expected FF, actual 00", result.stderr)
            print("EXPECTED MUTANT FAILURE:", result.stderr.strip())
            self.assertEqual(RENDERER.read_text(), source, "production source changed during test")
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
