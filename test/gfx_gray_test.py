#!/usr/bin/env python3
"""Compile the production renderer and font stack against host hardware boundaries."""

import os
import re
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
    "esp_heap_caps.h": r"""
#pragma once
#include <cstdlib>
#include <cstddef>
constexpr unsigned MALLOC_CAP_SPIRAM = 1;
constexpr unsigned MALLOC_CAP_8BIT = 2;
inline int allocationCalls = 0;
inline int failAllocation = 0;
inline int liveAllocations = 0;
inline size_t allocatedBytes = 0;
inline void* heap_caps_malloc(size_t size, unsigned caps) {
  if (caps != (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)) std::abort();
  ++allocationCalls;
  if (allocationCalls == failAllocation) return nullptr;
  void* p = std::malloc(size);
  if (p) { ++liveAllocations; allocatedBytes += size; }
  return p;
}
inline void heap_caps_free(void* p) { if (p) { --liveAllocations; std::free(p); } }
""",
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
#include <vector>
class HalFile {
 public:
  std::vector<uint8_t> bytes;
  size_t position = 0;
  HalFile() = default;
  explicit HalFile(std::vector<uint8_t> data) : bytes(std::move(data)) {}
  explicit operator bool() const { return !bytes.empty(); }
  bool seek(size_t offset) { if (offset > bytes.size()) return false; position = offset; return true; }
  bool seekSet(size_t offset) { return seek(offset); }
  bool seekCur(size_t offset) { return seek(position + offset); }
  int read() { return position < bytes.size() ? bytes[position++] : -1; }
  int read(void* target, size_t size) {
    size = std::min(size, bytes.size() - position);
    std::memcpy(target, bytes.data() + position, size); position += size; return size;
  }
  bool close() { return true; }
};
struct HostStorage {
  bool openFileForRead(const char*, const std::string&, HalFile&) { std::abort(); }
};
inline HostStorage Storage;
""",
}

HARNESS = r"""
#include <array>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <GfxRenderer.h>
#include <DirectPixelWriter.h>
#include <EpdFont.h>
#include <FontCacheManager.h>
#include <FontDecompressor.h>
#include <esp_heap_caps.h>

static std::array<uint8_t, 48000> physical;
static GfxRenderer* captureAbortRenderer = nullptr;
static uint8_t* captureAbortDestination = nullptr;
static int captureAbortHits = 0;
static const uint8_t* restoreAbortL = nullptr;
static const uint8_t* restoreAbortM = nullptr;
static bool restoreAbortSawMixed = false;
static size_t restoreAbortBytes = 1;
extern "C" void* memcpy(void* destination, const void* source, size_t size)
    noexcept(noexcept(std::memcpy(destination, source, size))) {
  void* result = std::memmove(destination, source, size);
  if (captureAbortRenderer && destination == captureAbortDestination) {
    GfxRenderer* renderer = captureAbortRenderer;
    captureAbortRenderer = nullptr;
    ++captureAbortHits;
    if (restoreAbortL && restoreAbortM) {
      restoreAbortSawMixed = size == restoreAbortBytes;
      for (size_t i = 0; i < restoreAbortBytes; ++i)
        restoreAbortSawMixed &= static_cast<uint8_t*>(destination)[i] == 0xCC &&
                                restoreAbortL[i] == 0x11 && restoreAbortM[i] == 0x55;
    }
    renderer->abortDisplayWork();
  }
  return result;
}
static int scratchAllocationCalls = 0;
static int failScratchAllocation = 0;
extern "C" void* gfx_malloc(size_t size) noexcept {
  ++scratchAllocationCalls;
  if (scratchAllocationCalls == failScratchAllocation) return nullptr;
  return std::malloc(size);
}
struct ScratchChunks {
  using type = std::vector<uint8_t*> GfxRenderer::*;
  friend type scratchChunksMember(ScratchChunks);
};
template <ScratchChunks::type Member> struct ScratchAccess {
  friend ScratchChunks::type scratchChunksMember(ScratchChunks) { return Member; }
};
template struct ScratchAccess<&GfxRenderer::bwBufferChunks>;
static std::vector<uint8_t*>& scratchChunks(GfxRenderer& renderer) {
  return renderer.*scratchChunksMember(ScratchChunks{});
}
static bool lent = false;
static bool aborted = false;
static int submissions = 0;
static int graySubmissions = 0;
static int baseSubmissions = 0;
static int asyncSubmissions = 0;
static int grayCopies = 0;
static int workStarts = 0;
static int cleanupCalls = 0;
static int cleanupSubmissions = 0;
static bool pendingCleanup = false;
static bool cleanupSawComplete = false;
static GfxRenderer* cleanupRenderer = nullptr;
static std::array<uint8_t, 48000> cleanupB{};
static HalDisplay::RefreshMode lastFallback = HalDisplay::FAST_REFRESH;
static bool lastFadingFix = false;
static std::array<uint8_t, 48000> stagedB{}, stagedL{}, stagedM{};
static void require(bool condition, const char* name) {
  if (!condition) {
    std::fprintf(stderr, "%s: expected true, actual false\n", name);
    std::exit(1);
  }
}
HalDisplay::HalDisplay() = default;
HalDisplay::~HalDisplay() = default;
uint8_t* HalDisplay::getFrameBuffer() const { return lent ? nullptr : physical.data(); }
uint16_t HalDisplay::getDisplayWidth() const { return 800; }
uint16_t HalDisplay::getDisplayHeight() const { return 480; }
uint16_t HalDisplay::getDisplayWidthBytes() const { return 100; }
uint32_t HalDisplay::getBufferSize() const { return 48000; }
void HalDisplay::clearScreen(uint8_t color) const { physical.fill(color); }
void HalDisplay::drawImage(const uint8_t* data, uint16_t x, uint16_t y, uint16_t width, uint16_t height, bool) const {
  for (unsigned row = 0; row < height && y + row < 480; ++row)
    for (unsigned byte = 0; byte < width / 8 && x / 8 + byte < 100; ++byte)
      physical[(y + row) * 100 + x / 8 + byte] = data[row * (width / 8) + byte];
}
void HalDisplay::displayBuffer(RefreshMode, bool) { ++submissions; }
void HalDisplay::displayBufferAsync(RefreshMode) { ++submissions; ++asyncSubmissions; }
void HalDisplay::displayGrayscaleBase(RefreshMode fallback, bool fading) {
  ++baseSubmissions; stagedB = physical; lastFallback = fallback; lastFadingFix = fading;
}
void HalDisplay::copyGrayscaleLsbBuffers(const uint8_t* source) { ++grayCopies; std::memcpy(stagedL.data(), source, 48000); }
void HalDisplay::copyGrayscaleMsbBuffers(const uint8_t* source) { ++grayCopies; std::memcpy(stagedM.data(), source, 48000); }
void HalDisplay::writeGrayscalePlaneStrip(bool lsb, const uint8_t* source, uint16_t y, uint16_t rows) {
  std::memcpy((lsb ? stagedL : stagedM).data() + y * 100, source, rows * 100);
}
void HalDisplay::prepareGrayscaleTarget() { pendingCleanup = true; }
void HalDisplay::cleanupGrayscaleBuffers(const uint8_t* source) {
  ++cleanupCalls;
  std::memcpy(cleanupB.data(), source, cleanupB.size());
  if (pendingCleanup && !aborted) {
    ++cleanupSubmissions;
    cleanupSawComplete = cleanupRenderer && cleanupRenderer->liveFrameValid() &&
                        cleanupRenderer->getFrameOwner() == GfxRenderer::FrameOwner::ReaderBase;
  }
  pendingCleanup = false;
}
void HalDisplay::displayGrayBuffer(bool fading) { ++graySubmissions; lastFadingFix = fading; }
bool HalDisplay::supportsAsyncRefresh() const { return true; }
bool HalDisplay::refreshBusy() { return false; }
void HalDisplay::waitRefreshComplete() {}
void HalDisplay::beginDisplayWork() { ++workStarts; aborted = false; }
void HalDisplay::abortPostRefresh() { aborted = true; }
bool HalDisplay::postRefreshAborted() const { return aborted; }
bool HalDisplay::displayCommitted() const { return !aborted; }
uint8_t* HalDisplay::lendFrameBufferStorage(uint32_t* size) {
  if (lent) return nullptr;
  lent = true;
  *size = 48000;
  return physical.data();
}
void HalDisplay::returnFrameBufferStorage() { lent = false; physical.fill(0xFF); }

static void expect(const uint8_t* actual, const uint8_t* expected, size_t size, const char* name) {
  for (size_t i = 0; i < size; ++i) {
    if (actual[i] != expected[i]) {
      std::fprintf(stderr, "%s: byte %zu expected %02X, actual %02X\n", name, i, expected[i], actual[i]);
      std::exit(1);
    }
  }
}

static void testOwnership(HalDisplay& hal, GfxRenderer& renderer) {
  using Owner = GfxRenderer::FrameOwner;
  require(!renderer.uiGrayEnabled() && allocationCalls == 0, "default disabled allocates no selectors");
  renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  renderer.clearScreen();
  renderer.displayBuffer();
  require(submissions == 1, "disabled binary submission preserved");
  require(!renderer.drawCoverage(1, 2, 2), "disabled coverage refused");
  for (int failure : {1, 2}) {
    allocationCalls = 0;
    failAllocation = failure;
    {
      GfxRenderer failing(hal);
      failing.begin();
      require(!failing.setUiGrayEnabled(true), "partial allocation rejects enable");
      require(!failing.uiGrayEnabled() && liveAllocations == 0, "partial allocation releases both halves");
      require(allocationCalls == 2, "both plane allocations attempted once");
      require(!failing.setUiGrayEnabled(true) && allocationCalls == 2, "failed pair is not repeatedly allocated");
      failing.beginDisplayWork();
      failing.clearScreen();
      failing.displayBuffer();
      require(failing.liveFrameValid(), "failed allocation retains binary frame");
      std::array<uint8_t, 100> readerL{}, readerM{};
      failing.beginDualStripTarget(readerL.data(), readerM.data(), 0, 1);
      failing.setOrientation(GfxRenderer::LandscapeCounterClockwise);
      failing.setRenderMode(GfxRenderer::GRAYSCALE_BOTH);
      failing.drawGrayPixel(0, 0, true, true);
      require(readerL[0] == 0x80 && readerM[0] == 0x80, "allocation fallback preserves reader selector AA");
      failing.endStripTarget();
    }
  }
  allocationCalls = 0;
  failAllocation = 0;
  allocatedBytes = 0;
  require(renderer.setUiGrayEnabled(true), "enable retained gray pair");
  require(allocationCalls == 2 && liveAllocations == 2 && allocatedBytes == 96000, "96 KB pair allocated in PSRAM");
  require(renderer.liveFrameNeedsRedraw() && !renderer.canCaptureLiveFrame(), "policy change requires full redraw");
  renderer.clearScreen();
  require(renderer.liveFrameValid() && renderer.coverageEnabled(), "full clear establishes live tuple");
  require(renderer.drawCoverage(1, 2, 2), "live coverage operation");
  const uint8_t* liveL = renderer.getLiveGrayPlane(true);
  const uint8_t* liveM = renderer.getLiveGrayPlane(false);
  require(physical[200] == 0xBF && liveL[200] == 0x40 && liveM[200] == 0x40, "live dark marker literal tuple");
  std::array<uint8_t, 48000> expectedLive;
  expectedLive.fill(0xFF); expectedLive[200] = 0xBF;
  std::array<uint8_t, 48000> expectedSelectors{};
  expectedSelectors[200] = 0x40;
  std::array<uint8_t, 300> outer, inner;
  outer.fill(0x11); inner.fill(0x22);
  renderer.setGrayscaleClipRect(4, 5, 6, 7);
  const uint32_t originalGeneration = renderer.getTargetGeneration();
  {
    GfxRenderer::ScopedTarget target(renderer, outer.data(), 2, 3);
    require(target.active() && renderer.getFrameOwner() == Owner::OffscreenBw, "offscreen BW owns strip");
    require(!renderer.isTargetCurrent(originalGeneration), "new binding rejects cached live target");
    require(!renderer.canCaptureLiveFrame() && !renderer.coverageEnabled(), "scratch refuses live capture and coverage");
    renderer.clearScreen(0);
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_LSB);
    renderer.setGrayscaleClipRect(1, 2, 3, 4);
    const uint32_t outerGeneration = renderer.getTargetGeneration();
    {
      GfxRenderer::ScopedTarget nested(renderer, inner.data(), 3, 3);
      renderer.setRenderMode(GfxRenderer::BW_GRAY_BASE);
      renderer.clearGrayscaleClipRect();
      renderer.clearScreen(0xA5);
      renderer.displayBuffer();
    }
    require(renderer.getWriteTarget() == outer.data() && renderer.getWriteOriginY() == 2 && renderer.getWriteRows() == 3,
            "nested target restores pointer and physical band");
    require(renderer.getRenderMode() == GfxRenderer::GRAYSCALE_LSB && renderer.grayscaleClipEnabled() &&
            renderer.grayscaleClipX0() == 1 && renderer.grayscaleClipY0() == 2 && renderer.grayscaleClipX1() == 4 &&
            renderer.grayscaleClipY1() == 6, "nested scope restores mode and clip");
    require(!renderer.isTargetCurrent(outerGeneration), "restored target gets a fresh generation");
    renderer.clearScreen(0x33);
  }
  require(renderer.getWriteTarget() == physical.data() && renderer.getRenderMode() == GfxRenderer::BW &&
          renderer.grayscaleClipX0() == 4 && renderer.grayscaleClipY0() == 5 && renderer.grayscaleClipX1() == 10 &&
          renderer.grayscaleClipY1() == 12, "outer scope restores live metadata");
  require(renderer.coverageEnabled() && !renderer.isTargetCurrent(originalGeneration), "coverage restored and old generation stays stale");
  std::array<uint8_t, 300> expectedOuter, expectedInner;
  expectedOuter.fill(0x33); expectedInner.fill(0xA5);
  expect(outer.data(), expectedOuter.data(), 300, "scope keeps written outer bytes");
  expect(inner.data(), expectedInner.data(), 300, "scope keeps written inner bytes");
  expect(physical.data(), expectedLive.data(), 48000, "scoped scratch preserves live B");
  expect(liveL, expectedSelectors.data(), 48000, "scoped scratch preserves live L");
  expect(liveM, expectedSelectors.data(), 48000, "scoped scratch preserves live M");
  require(submissions == 3, "offscreen display makes no submission");
  renderer.clearGrayscaleClipRect();
  {
    GfxRenderer::ScopedTarget scope(renderer, outer.data(), 2, 3);
    renderer.beginStripTarget(inner.data(), 3, 3);
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_MSB);
  }
  renderer.beginStripTarget(inner.data(), 3, 3);
  renderer.endStripTarget();
  require(renderer.getWriteTarget() == physical.data() && renderer.getRenderMode() == GfxRenderer::BW,
          "scope unwinds unfinished legacy strip metadata");
  {
    GfxRenderer::ScopedTarget base(renderer, Owner::ReaderBase);
    require(base.active() && !renderer.coverageEnabled(), "reader base suspends UI coverage");
    renderer.setRenderMode(GfxRenderer::BW_GRAY_BASE);
    GfxRenderer::FrameBufferLoan refused(renderer);
    require(renderer.hasFrameBuffer(), "reader base refuses framebuffer loan");
  }
  std::array<uint8_t, 300> slotB, slotL, slotM;
  bool coherent = false;
  GrayFrame slot({slotB.data(), 300}, {slotL.data(), 300}, {slotM.data(), 300}, 800, 3, 100, 3);
  renderer.beginDisplayWork();
  {
    GfxRenderer::ScopedTarget target(renderer, slot, coherent);
    require(target.active() && renderer.getFrameOwner() == Owner::OffscreenFrame, "offscreen tuple binds");
    renderer.clearScreen();
    require(coherent && renderer.drawCoverage(0, 3, 1), "offscreen full clear establishes tuple");
    require(slotB[0] == 0xFF && slotL[0] == 0 && slotM[0] == 0x80, "offscreen light marker literal tuple");
    renderer.abortDisplayWork();
    require(coherent && renderer.liveFrameValid(), "input cancellation changes no tuple validity");
  }
  require(!coherent && renderer.liveFrameValid(), "cancelled offscreen invalidates only slot");
  renderer.beginDisplayWork();
  expect(physical.data(), expectedLive.data(), 48000, "cancelled offscreen preserves live B");
  expect(liveL, expectedSelectors.data(), 48000, "cancelled offscreen preserves live L");
  expect(liveM, expectedSelectors.data(), 48000, "cancelled offscreen preserves live M");
  {
    GfxRenderer::ScopedTarget ui(renderer, Owner::LiveUi);
    renderer.abortDisplayWork();
    require(renderer.liveFrameValid(), "input cancellation leaves live metadata for render thread");
  }
  require(!renderer.liveFrameValid() && !renderer.canCaptureLiveFrame(), "cancelled live tuple needs redraw");
  renderer.displayBuffer();
  require(submissions == 3, "cancelled live frame refuses submission");
  renderer.beginDisplayWork();
  renderer.clearScreen();
  require(renderer.drawCoverage(1, 2, 2), "new generation full clear recovers live frame");
  {
    GfxRenderer::ScopedTarget scratch(renderer, Owner::ReaderScratch);
    require(renderer.getRenderMode() == GfxRenderer::BW && !renderer.coverageEnabled(), "scratch ownership precedes mode change");
    renderer.clearScreen(0);
    require(!renderer.canCaptureLiveFrame(), "HAL scratch refuses live capture");
    expect(liveL, expectedSelectors.data(), 48000, "HAL scratch clear preserves live L");
    expect(liveM, expectedSelectors.data(), 48000, "HAL scratch clear preserves live M");
  }
  require(renderer.liveFrameNeedsRedraw(), "scratch cannot restore validity without restoring bytes");
  renderer.clearScreen();
  {
    GfxRenderer::ScopedTarget alias(renderer, physical.data(), 0, 1);
    require(alias.active() && renderer.getFrameOwner() == Owner::ReaderScratch, "HAL alias is reader scratch");
    renderer.clearScreen(0);
  }
  require(renderer.liveFrameNeedsRedraw(), "HAL strip alias requires live redraw");
  renderer.clearScreen();
  require(renderer.drawCoverage(1, 2, 2), "secondary alias starts with live gray marker");
  renderer.beginDualStripTarget(outer.data(), physical.data(), 0, 1);
  require(renderer.getFrameOwner() == Owner::ReaderScratch && renderer.liveFrameNeedsRedraw(),
          "legacy secondary HAL alias owns reader scratch and invalidates live frame");
  renderer.clearScreen(0);
  require(physical[0] == 0x00 && physical[99] == 0x00 && physical[100] == 0xFF && physical[200] == 0xBF,
          "secondary alias clear changes only its HAL band");
  expect(liveL, expectedSelectors.data(), 48000, "secondary alias clear preserves live L");
  expect(liveM, expectedSelectors.data(), 48000, "secondary alias clear preserves live M");
  renderer.endStripTarget();
  require(renderer.getWriteTarget() == physical.data() && renderer.getFrameOwner() == Owner::LiveUi,
          "secondary alias end restores live target metadata");
  require(renderer.liveFrameNeedsRedraw() && !renderer.canCaptureLiveFrame() && physical[0] == 0x00,
          "secondary alias end restores neither bytes nor coherence");
  renderer.displayBuffer();
  require(submissions == 3 && !renderer.drawCoverage(0, 0, 2),
          "secondary alias cannot publish or blend before full rebuild");
  renderer.clearScreen();
  uint8_t rebuilt = 0;
  GfxRenderer::FrameSnapshot rebuiltSnapshot;
  std::array<uint8_t, 3> rebuiltTuple{};
  require(renderer.liveFrameValid() && renderer.captureRegion(0, 0, 8, 1,
              {rebuiltTuple.data(), rebuiltTuple.size()}, rebuiltSnapshot) == GfxRenderer::FrameResult::Ok &&
              rebuiltTuple[0] == 0xFF, "secondary alias full rebuild restores usable live frame");
  require(renderer.readFramebufferRegion(0, 0, 8, 1, &rebuilt, 1) == 0 && rebuilt == 0,
          "retained legacy capture rejected after successful rebuild");
  const auto beforeLoan = renderer.getTargetGeneration();
  {
    GfxRenderer::FrameBufferLoan loan(renderer);
    GfxRenderer::FrameBufferLoan nested(renderer);
    require(!renderer.hasFrameBuffer() && renderer.getFrameOwner() == Owner::Loan, "loan owns live storage");
    require(!renderer.isTargetCurrent(beforeLoan) && !renderer.getWriteTarget(), "loan rejects cached target");
    physical.fill(0x5A);
    renderer.clearScreen();
    renderer.drawPixel(0, 0);
    renderer.fillRect(0, 0, 8, 2);
    renderer.invertScreen();
    renderer.displayBuffer();
    renderer.displayBufferAsync();
    uint8_t capture = 0x77;
    require(renderer.readFramebufferRegion(0, 0, 8, 1, &capture, 1) == 0 && capture == 0x77, "loan refuses capture");
    require(!renderer.copyRegionToBuffer(0, 0, 8, 1, &capture, 1), "loan refuses region copy");
    require(!renderer.drawCoverage(0, 0, 2), "loan refuses coverage");
    GfxRenderer::ScopedTarget refused(renderer, outer.data(), 2, 3);
    require(!refused.active(), "loan refuses target binding");
    nested.end();
    require(!renderer.hasFrameBuffer() && physical[0] == 0x5A && submissions == 3, "nested loan cannot restore outer storage");
  }
  std::array<uint8_t, 48000> white, zero{}; white.fill(0xFF);
  expect(physical.data(), white.data(), 48000, "loan returns white B");
  expect(liveL, zero.data(), 48000, "loan clears L");
  expect(liveM, zero.data(), 48000, "loan clears M");
  require(renderer.liveFrameNeedsRedraw() && !renderer.canCaptureLiveFrame(), "loan return requires full redraw");
  renderer.displayBuffer();
  require(submissions == 3, "loan return cannot publish blank partial frame");
  renderer.clearScreen();
  require(renderer.canCaptureLiveFrame(), "full clear recovers after loan");
  require(renderer.setUiGrayEnabled(false) && renderer.liveFrameNeedsRedraw(), "disabling policy requires redraw");
  renderer.clearScreen();
  require(renderer.setUiGrayEnabled(true) && allocationCalls == 2 && liveAllocations == 2, "policy toggle reuses pair");
}

static void testUiSubmission(GfxRenderer& renderer) {
  renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  renderer.clearScreen();
  require(renderer.drawCoverage(1, 2, 2), "seed live UI marker for submission");
  renderer.displayBuffer(HalDisplay::HALF_REFRESH);
  require(graySubmissions == 1, "UI marker submits exactly one gray frame");
  require(baseSubmissions == 1 && lastFallback == HalDisplay::HALF_REFRESH, "UI gray uses requested base fallback");
  require(stagedB[200] == 0xBF && stagedL[200] == 0x40 && stagedM[200] == 0x40,
          "UI gray submits literal complete marker tuple");
  require(!renderer.supportsAsyncRefresh(), "UI gray reports blocking refresh support");
  renderer.setFadingFix(true);
  renderer.displayBufferAsync(HalDisplay::FULL_REFRESH);
  require(graySubmissions == 2 && baseSubmissions == 2 && asyncSubmissions == 0 && lastFadingFix &&
          lastFallback == HalDisplay::FULL_REFRESH && !renderer.refreshBusy(), "async UI gray completes blocking with fading fix");
  renderer.setFadingFix(false);
  renderer.clearScreen();
  const int binaryBefore = submissions;
  renderer.displayBufferAsync();
  require(submissions == binaryBefore + 1 && graySubmissions == 2 && renderer.supportsAsyncRefresh(),
          "empty selectors retain binary async submission");
}

static void testReaderImport(GfxRenderer& renderer) {
  using Owner = GfxRenderer::FrameOwner;
  renderer.beginDisplayWork();
  renderer.clearScreen();
  require(renderer.drawCoverage(1, 2, 2) && renderer.drawCoverage(6, 2, 1) && renderer.drawCoverage(2, 2, 2),
          "seed UI boundary markers and stale reader gray");
  const uint8_t* liveL = renderer.getLiveGrayPlane(true);
  const uint8_t* liveM = renderer.getLiveGrayPlane(false);
  std::array<uint8_t, 200> sourceB{}, sourceL{}, sourceM{};
  sourceB[0] = 0x0C;
  sourceB[100] = 0x3C;
  sourceL[0] = 0x10;
  sourceM[0] = 0x18;
  const int grayBefore = graySubmissions;
  const int baseBefore = baseSubmissions;
  {
    GfxRenderer::ScopedTarget scratch(renderer, Owner::ReaderScratch);
    require(renderer.beginReaderImport(2, 2, 4, 2), "begin clipped reader import");
    renderer.clearScreen(0);
    require(physical[200] == 0x83 && physical[0] == 0xFF, "scratch clear preserves B outside content clip");
    require(liveL[200] == 0x60 && liveM[200] == 0x62, "scratch before mode change preserves UI and reader selectors");
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_LSB);
    require(!renderer.finishReaderImport(), "incomplete selector coverage rejects completion");
    renderer.displayBuffer();
    require(graySubmissions == grayBefore, "incomplete reader import cannot submit");
    require(!renderer.importReaderPlane(true, sourceL.data(), 99, 2, 1), "short selector source rejected");
    require(renderer.importReaderPlane(true, sourceL.data(), 100, 2, 1), "replace first L strip");
    require(stagedL[200] == 0x50, "import forwards whole L row with UI marker");
    {
      std::array<uint8_t, 100> nestedL{}, nestedM{};
      GfxRenderer::ScopedTarget strip(renderer, nestedL.data(), 3, 1, nestedM.data());
      renderer.clearScreen(0);
      require(!renderer.importReaderPlane(false, sourceM.data(), 100, 2, 1), "different target cannot import reader plane");
      require(!renderer.finishReaderImport(), "different target cannot finish reader import");
    }
    require(renderer.importReaderPlane(true, sourceL.data() + 100, 100, 3, 1), "L coverage survives nested strip binding");
    renderer.writeGrayscalePlaneStrip(false, sourceM.data(), 2, 2);
    require(!renderer.finishReaderImport(), "complete selectors without B restoration rejected");
    require(renderer.restoreReaderBase(sourceB.data(), 100, 2, 1), "restore first B strip");
    require(!renderer.finishReaderImport(), "partial B restoration rejected");
    require(renderer.restoreReaderBase(sourceB.data() + 100, 100, 3, 1), "restore final B strip");
    require(renderer.finishReaderImport(), "complete B L M reader import accepted");
    renderer.clearScreen(0);
    require(renderer.liveFrameNeedsRedraw() && !renderer.finishReaderImport(), "cleared completed import requires B restoration");
    require(physical[200] == 0x83 && physical[0] == 0xFF, "repeated scratch clear preserves outside B marker");
    require(renderer.restoreReaderBase(sourceB.data(), 200, 2, 2) && renderer.finishReaderImport(),
            "B restoration completes cleared reader import again");
  }
  require(renderer.liveFrameValid(), "completed reader tuple readable after scratch exit");
  renderer.displayBuffer(HalDisplay::HALF_REFRESH);
  require(graySubmissions == grayBefore + 1 && baseSubmissions == baseBefore + 1,
          "reader scratch sequence submits exactly one complete gray frame");
  std::array<uint8_t, 48000> expectedB, expectedL{}, expectedM{};
  expectedB.fill(0xFF);
  expectedB[200] = 0x8F;
  expectedL[200] = 0x50;
  expectedM[200] = 0x5A;
  expect(stagedB.data(), expectedB.data(), 48000, "reader submitted B");
  expect(stagedL.data(), expectedL.data(), 48000, "reader submitted L");
  expect(stagedM.data(), expectedM.data(), 48000, "reader submitted M");

  for (int abortPoint = 0; abortPoint < 5; ++abortPoint) {
    renderer.beginDisplayWork();
    renderer.clearScreen();
    const int before = graySubmissions;
    {
      GfxRenderer::ScopedTarget scratch(renderer, Owner::ReaderScratch);
      require(renderer.beginReaderImport(2, 2, 4, 2), "begin abort fixture");
      renderer.clearScreen(0);
      if (abortPoint == 0) renderer.abortDisplayWork();
      renderer.importReaderPlane(true, sourceL.data(), 200, 2, 2);
      if (abortPoint == 1) renderer.abortDisplayWork();
      renderer.importReaderPlane(false, sourceM.data(), 200, 2, 2);
      if (abortPoint == 2) renderer.abortDisplayWork();
      renderer.restoreReaderBase(sourceB.data(), 200, 2, 2);
      if (abortPoint == 3) renderer.abortDisplayWork();
      if (abortPoint == 4) scratch.cancel();
      require(!renderer.finishReaderImport(), "cancelled reader import rejected");
    }
    renderer.displayBuffer();
    renderer.displayGrayBuffer();
    require(graySubmissions == before && renderer.liveFrameNeedsRedraw(), "cancelled tuple makes no gray submission");
  }
  renderer.beginDisplayWork();
  renderer.clearScreen();
  {
    GfxRenderer::ScopedTarget scratch(renderer, Owner::ReaderScratch);
    require(renderer.beginReaderImport(2, 2, 4, 2), "begin generation fixture");
    require(renderer.importReaderPlane(true, sourceL.data(), 200, 2, 2), "old work L imported");
    require(renderer.restoreReaderBase(sourceB.data(), 200, 2, 2), "old work B restored");
    renderer.beginDisplayWork();
    require(!renderer.importReaderPlane(false, sourceM.data(), 200, 2, 2) && !renderer.finishReaderImport(),
            "new work rejects old coverage");
  }
  const std::array<GfxRenderer::Orientation, 4> orientations = {
      GfxRenderer::LandscapeCounterClockwise, GfxRenderer::LandscapeClockwise,
      GfxRenderer::Portrait, GfxRenderer::PortraitInverted};
  const std::array<int, 4> markerOffsets = {300, 47699, 47800, 199};
  const std::array<uint8_t, 4> markerBits = {0x40, 0x02, 0x10, 0x08};
  for (size_t index = 0; index < orientations.size(); ++index) {
    renderer.beginDisplayWork();
    renderer.setOrientation(orientations[index]);
    renderer.clearScreen();
    require(renderer.drawCoverage(1, 3, 2), "seed rotated marker");
    std::array<uint8_t, 48000> white;
    white.fill(0xFF);
    {
      GfxRenderer::ScopedTarget scratch(renderer, Owner::ReaderScratch);
      require(renderer.beginReaderImport(2, 3, 4, 2), "begin rotated reader import");
      renderer.clearScreen(0);
      renderer.copyGrayscaleLsbBuffers();
      renderer.copyGrayscaleMsbBuffers();
      require(renderer.restoreReaderBase(white.data(), white.size(), 0, 480) && renderer.finishReaderImport(),
              "full reader imports complete in every orientation");
    }
    renderer.displayBuffer();
    expectedB.fill(0xFF);
    expectedL.fill(0);
    expectedM.fill(0);
    expectedB[markerOffsets[index]] &= ~markerBits[index];
    expectedL[markerOffsets[index]] = expectedM[markerOffsets[index]] = markerBits[index];
    expect(stagedB.data(), expectedB.data(), 48000, "rotated reader B marker");
    expect(stagedL.data(), expectedL.data(), 48000, "rotated reader L marker");
    expect(stagedM.data(), expectedM.data(), 48000, "rotated reader M marker");
  }
  renderer.clearScreen();
  require(renderer.setUiGrayEnabled(false), "disable retained gray for legacy control");
  renderer.clearScreen();
  const int before = graySubmissions;
  {
    GfxRenderer::ScopedTarget scratch(renderer, Owner::ReaderScratch);
    renderer.clearScreen(0);
    renderer.copyGrayscaleLsbBuffers();
    renderer.copyGrayscaleMsbBuffers();
  }
  renderer.displayGrayBuffer();
  require(graySubmissions == before + 1, "disabled reader scratch retains legacy gray submission");
  const int binaryBefore = submissions;
  renderer.displayBuffer();
  require(submissions == binaryBefore + 1 && graySubmissions == before + 1, "AA off UI submits BW only");
}

struct GlyphFixture {
  GfxRenderer& renderer;
  int id;
  EpdGlyph glyph{};
  EpdUnicodeInterval interval{};
  EpdFontData data{};
  EpdFont font{&data};

  GlyphFixture(GfxRenderer& r, int fontId, const uint8_t* bitmap, uint8_t width, uint8_t height, uint8_t bpp,
               uint32_t codepoint = 65)
      : renderer(r), id(fontId) {
    glyph.width = width;
    glyph.height = height;
    glyph.advanceX = width * 16;
    glyph.dataLength = (width * height * bpp + 7) / 8;
    interval = {codepoint, codepoint, 0};
    data.bitmap = bitmap;
    data.glyph = &glyph;
    data.intervals = &interval;
    data.intervalCount = 1;
    data.advanceY = height + 1;
    data.is2Bit = bpp == 2;
    data.glyphBitmapBpp = bpp == 8 ? 8 : 0;
    renderer.insertFont(id, EpdFontFamily(&font));
  }
  ~GlyphFixture() { renderer.removeFont(id); }
};

static void expectGlyphByte(const GfxRenderer& renderer, size_t offset, uint8_t b, uint8_t l, uint8_t m,
                            const char* name) {
  expect(physical.data() + offset, &b, 1, (std::string(name) + " B").c_str());
  expect(renderer.getLiveGrayPlane(true) + offset, &l, 1, (std::string(name) + " L").c_str());
  expect(renderer.getLiveGrayPlane(false) + offset, &m, 1, (std::string(name) + " M").c_str());
}

static void expectGlyphPixel(const GfxRenderer& renderer, int px, int py, uint8_t darkness, const char* name) {
  const uint8_t canonical[][3] = {{1, 0, 0}, {1, 0, 1}, {0, 1, 1}, {0, 0, 0}};
  const size_t offset = py * 100 + px / 8;
  const uint8_t mask = 0x80 >> (px % 8);
  const uint8_t actual[] = {uint8_t((physical[offset] & mask) != 0),
                            uint8_t((renderer.getLiveGrayPlane(true)[offset] & mask) != 0),
                            uint8_t((renderer.getLiveGrayPlane(false)[offset] & mask) != 0)};
  expect(actual, canonical[darkness], 3, name);
}

static void testPackedGlyphs(GfxRenderer& renderer) {
  const uint8_t packed[] = {0x1B};
  GlyphFixture font(renderer, 100, packed, 4, 1, 2);
  renderer.clearScreen();
  renderer.drawText(100, 0, 0, "A");
  expectGlyphByte(renderer, 0, 0xCF, 0x20, 0x60, "glyph packed");
  renderer.drawText(100, 0, 0, "A");
  expectGlyphByte(renderer, 0, 0x8F, 0x40, 0x40, "glyph overlap");
  renderer.drawText(100, 0, 0, "A", false);
  expectGlyphByte(renderer, 0, 0xFF, 0, 0x60, "glyph overlap white");
  renderer.clearScreen(0);
  renderer.drawText(100, 0, 0, "A", false);
  expectGlyphByte(renderer, 0, 0x30, 0x40, 0x60, "glyph white on black");
  renderer.clearScreen();
  require(renderer.drawCoverage(0, 0, 1), "seed glyph transparency");
  renderer.drawText(100, 0, 0, "A");
  expectGlyphByte(renderer, 0, 0xCF, 0x20, 0xE0, "glyph zero preserves destination");
  require(renderer.getTextAdvanceX(100, "AA", EpdFontFamily::REGULAR) == 8, "glyph advances unchanged");
  renderer.clearScreen();
  renderer.drawText(100, 0, 1, "A", true, EpdFontFamily::BOLD);
  expectGlyphByte(renderer, 100, 0xCF, 0x20, 0x60, "glyph missing style fallback");
}

static const uint8_t* alphaBitmap(void* context, const EpdGlyph*) { return static_cast<const uint8_t*>(context); }

static void testAlphaAndBinaryGlyphs(GfxRenderer& renderer) {
  const uint8_t alpha[] = {31, 32, 127, 128, 223, 224};
  GlyphFixture font(renderer, 101, nullptr, 6, 1, 8);
  font.data.glyphMissCtx = const_cast<uint8_t*>(alpha);
  font.data.glyphBitmapHandler = alphaBitmap;
  renderer.clearScreen();
  renderer.drawText(101, 0, 0, "A");
  expectGlyphByte(renderer, 0, 0xE3, 0x18, 0x78, "glyph alpha thresholds");
  renderer.clearScreen(0);
  renderer.drawText(101, 0, 0, "A", false);
  expectGlyphByte(renderer, 0, 0x1C, 0x60, 0x78, "glyph alpha white");
  const uint8_t binary[] = {0x50};
  GlyphFixture mono(renderer, 102, binary, 4, 1, 1);
  renderer.clearScreen();
  require(renderer.drawCoverage(0, 0, 2) && renderer.drawCoverage(1, 0, 2), "seed binary overwrite");
  renderer.drawText(102, 0, 0, "A");
  expectGlyphByte(renderer, 0, 0x2F, 0x80, 0x80, "glyph binary clears painted selectors");
  renderer.drawText(102, 0, 0, "A", false);
  expectGlyphByte(renderer, 0, 0x7F, 0x80, 0x80, "glyph binary white preserves skipped shade");
}

static void testDecodedAndFallbackGlyphs(GfxRenderer& renderer) {
  FontDecompressor decoder;
  require(decoder.init(), "glyph decoder initialized");
  FontCacheManager cache(renderer.getFontMap(), renderer.getSdCardFonts());
  cache.setFontDecompressor(&decoder);
  renderer.setFontCacheManager(&cache);
  const uint8_t compressed[] = {0x93, 0x06, 0x00};
  GlyphFixture font(renderer, 103, compressed, 4, 1, 2);
  const EpdFontGroup group{0, 3, 1, 1, 0};
  font.data.groups = &group;
  font.data.groupCount = 1;
  renderer.clearScreen();
  renderer.drawText(103, 0, 0, "A");
  expectGlyphByte(renderer, 0, 0xCF, 0x20, 0x60, "glyph real DEFLATE");
  cache.prewarmCache(103, "A");
  renderer.drawText(103, 0, 1, "A");
  expectGlyphByte(renderer, 100, 0xCF, 0x20, 0x60, "glyph decompressor page cache");
  const uint8_t packed[] = {0x1B};
  GlyphFixture cjk(renderer, 104, packed, 4, 1, 2, 0x4E00);
  renderer.setFallbackFont(103, 104);
  renderer.drawText(103, 0, 2, "\xE4\xB8\x80");
  expectGlyphByte(renderer, 200, 0xCF, 0x20, 0x60, "glyph CJK font fallback");
  GlyphFixture replacement(renderer, 105, packed, 4, 1, 2, 0xFFFD);
  renderer.drawText(105, 0, 3, "?");
  expectGlyphByte(renderer, 300, 0xCF, 0x20, 0x60, "glyph replacement fallback");
  renderer.clearFallbackFonts();
  renderer.setFontCacheManager(nullptr);
}

static void testHalfSizeGlyphs(GfxRenderer& renderer) {
  const uint8_t packed[] = {0x03};
  const uint8_t alpha[] = {0, 0, 0, 255};
  const uint8_t binary[] = {0x10};
  GlyphFixture two(renderer, 106, packed, 2, 2, 2);
  GlyphFixture eight(renderer, 107, alpha, 2, 2, 8);
  GlyphFixture one(renderer, 108, binary, 2, 2, 1);
  renderer.clearScreen();
  renderer.drawText(106, 0, 0, "A", true, EpdFontFamily::SUP);
  expectGlyphByte(renderer, 0, 0x7F, 0x80, 0x80, "glyph half packed boost");
  renderer.drawText(107, 0, 1, "A", true, EpdFontFamily::SUB);
  expectGlyphByte(renderer, 100, 0xFF, 0, 0x80, "glyph half alpha average");
  renderer.drawText(108, 0, 2, "A", true, EpdFontFamily::SUP);
  expectGlyphByte(renderer, 200, 0x7F, 0, 0, "glyph half binary sample");
  require(renderer.getTextAdvanceX(106, "AA", EpdFontFamily::SUP) == 2, "half glyph advances unchanged");
  renderer.clearScreen(0);
  renderer.drawText(106, 0, 0, "A", false, EpdFontFamily::SUP);
  expectGlyphByte(renderer, 0, 0x80, 0, 0x80, "glyph half white packed");
  renderer.drawText(107, 0, 1, "A", false, EpdFontFamily::SUB);
  expectGlyphByte(renderer, 100, 0, 0x80, 0x80, "glyph half white alpha");
}

static void testOddAndRotatedGlyphs(GfxRenderer& renderer) {
  const uint8_t packed[] = {0x0C, 0x0C, 0x40};
  const uint8_t alpha[] = {0, 0, 255, 0, 0, 0, 255, 0, 127};
  GlyphFixture two(renderer, 109, packed, 3, 3, 2);
  GlyphFixture eight(renderer, 110, alpha, 3, 3, 8);
  renderer.clearScreen();
  renderer.drawText(109, 0, 0, "A", true, EpdFontFamily::SUP);
  expectGlyphByte(renderer, 0, 0xBF, 0, 0, "glyph odd packed row zero");
  expectGlyphByte(renderer, 100, 0x7F, 0, 0x40, "glyph odd packed row one");
  renderer.drawText(110, 0, 2, "A", true, EpdFontFamily::SUB);
  expectGlyphByte(renderer, 200, 0xBF, 0x40, 0x40, "glyph odd alpha row zero");
  expectGlyphByte(renderer, 300, 0x7F, 0x80, 0xC0, "glyph odd alpha row one");
  renderer.setOrientation(GfxRenderer::Portrait);
  renderer.clearScreen();
  renderer.drawText(110, 1, 2, "A", true, EpdFontFamily::SUP);
  expectGlyphPixel(renderer, 2, 477, 2, "glyph odd portrait upper pixel");
  expectGlyphPixel(renderer, 3, 478, 2, "glyph odd portrait lower pixel");
  expectGlyphPixel(renderer, 3, 477, 1, "glyph odd portrait corner pixel");
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
}

static void testGlyphRotation(GfxRenderer& renderer) {
  const uint8_t packed[] = {0x1B};
  GlyphFixture font(renderer, 111, packed, 4, 1, 2);
  const GfxRenderer::Orientation orientations[] = {GfxRenderer::Portrait, GfxRenderer::PortraitInverted,
                                                   GfxRenderer::LandscapeClockwise,
                                                   GfxRenderer::LandscapeCounterClockwise};
  const int physicalGray[][2] = {{2, 477}, {797, 2}, {797, 477}, {2, 2}};
  for (size_t i = 0; i < 4; ++i) {
    renderer.setOrientation(orientations[i]);
    renderer.clearScreen();
    renderer.drawText(111, 1, 2, "A");
    expectGlyphPixel(renderer, physicalGray[i][0], physicalGray[i][1], 1, "glyph orientation literal pixel");
  }
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  renderer.clearScreen();
  renderer.drawTextRotated90CW(111, 10, 10, "A");
  expectGlyphPixel(renderer, 10, 10, 0, "glyph rotated transparent");
  expectGlyphPixel(renderer, 10, 9, 1, "glyph rotated light");
  expectGlyphPixel(renderer, 10, 8, 2, "glyph rotated dark");
  expectGlyphPixel(renderer, 10, 7, 3, "glyph rotated black");
  const uint8_t alpha[] = {31, 32, 127, 128, 223, 224};
  const uint8_t shades[] = {0, 1, 1, 2, 2, 3};
  GlyphFixture eight(renderer, 115, alpha, 6, 1, 8);
  renderer.drawTextRotated90CW(115, 20, 20, "A");
  for (int i = 0; i < 6; ++i) {
    expectGlyphPixel(renderer, 20, 20 - i, shades[i], "glyph rotated alpha thresholds");
  }
  const uint8_t binary[] = {0x50};
  GlyphFixture one(renderer, 116, binary, 4, 1, 1);
  renderer.drawTextRotated90CW(116, 30, 30, "A");
  expectGlyphPixel(renderer, 30, 30, 0, "glyph rotated binary transparent");
  expectGlyphPixel(renderer, 30, 29, 3, "glyph rotated binary first ink");
  expectGlyphPixel(renderer, 30, 28, 0, "glyph rotated binary gap");
  expectGlyphPixel(renderer, 30, 27, 3, "glyph rotated binary second ink");
}

static void testLegacyGlyphs(GfxRenderer& renderer) {
  const uint8_t packed[] = {0x1B};
  const uint8_t alpha[] = {31, 32, 127, 128, 223, 224};
  GlyphFixture two(renderer, 112, packed, 4, 1, 2);
  GlyphFixture eight(renderer, 113, alpha, 6, 1, 8);
  renderer.setCoveragePolicy(GfxRenderer::CoveragePolicy::Suspend);
  renderer.setRenderMode(GfxRenderer::BW);
  renderer.clearScreen();
  renderer.drawText(112, 0, 0, "A");
  renderer.drawText(113, 0, 1, "A");
  require(physical[0] == 0x8F && physical[100] == 0xE3, "legacy packed and alpha BW thresholds");
  renderer.setRenderMode(GfxRenderer::BW_GRAY_BASE);
  renderer.clearScreen();
  renderer.drawText(112, 0, 0, "A");
  require(physical[0] == 0xCF, "legacy packed gray base threshold");
  std::array<uint8_t, 100> l{}, m{};
  renderer.beginDualStripTarget(l.data(), m.data(), 0, 1);
  renderer.setRenderMode(GfxRenderer::GRAYSCALE_BOTH);
  renderer.drawText(112, 0, 0, "A");
  require(l[0] == 0x20 && m[0] == 0x60, "legacy packed dual selectors");
  l.fill(0);
  m.fill(0);
  renderer.drawText(113, 0, 0, "A");
  require(l[0] == 0x18 && m[0] == 0x78, "legacy alpha dual selectors");
  renderer.endStripTarget();
  renderer.setRenderMode(GfxRenderer::BW);
  renderer.setCoveragePolicy(GfxRenderer::CoveragePolicy::Collect);
}

static void testGlyphScan(GfxRenderer& renderer) {
  const uint8_t packed[] = {0x1B};
  GlyphFixture font(renderer, 114, packed, 4, 1, 2);
  FontCacheManager cache(renderer.getFontMap(), renderer.getSdCardFonts());
  renderer.setFontCacheManager(&cache);
  renderer.clearScreen();
  require(renderer.drawCoverage(0, 0, 2), "scan marker");
  std::array<uint8_t, 48000> b = physical, l{}, m{};
  std::memcpy(l.data(), renderer.getLiveGrayPlane(true), l.size());
  std::memcpy(m.data(), renderer.getLiveGrayPlane(false), m.size());
  {
    auto scan = cache.createPrewarmScope();
    renderer.drawText(114, 0, 0, "A");
    renderer.drawText(114, 0, 1, "A", true, EpdFontFamily::SUP);
    renderer.drawTextRotated90CW(114, 10, 10, "A");
    renderer.setCoveragePolicy(GfxRenderer::CoveragePolicy::Suspend);
    renderer.drawTextRotated90CW(114, 20, 20, "A");
  }
  expect(physical.data(), b.data(), b.size(), "glyph scan preserves B");
  expect(renderer.getLiveGrayPlane(true), l.data(), l.size(), "glyph scan preserves L");
  expect(renderer.getLiveGrayPlane(false), m.data(), m.size(), "glyph scan preserves M");
  renderer.setCoveragePolicy(GfxRenderer::CoveragePolicy::Collect);
  renderer.setFontCacheManager(nullptr);
}

static void testGlyphCoverage(HalDisplay& hal) {
  GfxRenderer renderer(hal);
  renderer.begin();
  renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "glyph tuple allocation");
  testPackedGlyphs(renderer);
  testAlphaAndBinaryGlyphs(renderer);
  testDecodedAndFallbackGlyphs(renderer);
  testHalfSizeGlyphs(renderer);
  testOddAndRotatedGlyphs(renderer);
  testGlyphRotation(renderer);
  testLegacyGlyphs(renderer);
  testGlyphScan(renderer);
}


static constexpr GfxRenderer::Orientation writerOrientations[] = {
  GfxRenderer::Portrait, GfxRenderer::LandscapeClockwise,
  GfxRenderer::PortraitInverted, GfxRenderer::LandscapeCounterClockwise
};

static void testOpaqueFills(GfxRenderer& renderer) {
  std::array<uint8_t, 48000> b{}, l{}, m{}, expectedB{}, expectedSelectors{};
  GrayFrame tuple{{b.data(), b.size()}, {l.data(), l.size()}, {m.data(), m.size()}, 800, 480, 100};
  const int rectangles[][4] = {{477, 1, 1, 22}, {777, 477, 22, 1}, {2, 777, 1, 22}, {1, 2, 22, 1}};
  const uint8_t patterns[][4] = {{0xFF, 0xFF, 0xAA, 0x55}, {0xAA, 0x55, 0xAA, 0x55}};
  bool coherent = true;
  auto live = physical;
  for (unsigned orientation = 0; orientation < 4; ++orientation) {
    renderer.setOrientation(writerOrientations[orientation]);
    GfxRenderer::ScopedTarget target(renderer, tuple, coherent);
    const int* rect = rectangles[orientation];
    for (unsigned ink = 0; ink < 4; ++ink) {
      tuple.fill(2); expectedB.fill(0); expectedSelectors.fill(0xFF);
      const uint8_t pattern = ink == 0 ? 0xFF : ink == 1 ? 0 : patterns[ink - 2][orientation];
      if (ink < 2) renderer.fillRect(rect[0], rect[1], rect[2], rect[3], ink == 1);
      else renderer.fillRectDither(rect[0], rect[1], rect[2], rect[3], ink == 2 ? LightGray : DarkGray);
      expectedB[200] = pattern & 0x7F; expectedB[201] = pattern; expectedB[202] = pattern & 0xFE;
      expectedSelectors[200] = 0x80; expectedSelectors[201] = 0; expectedSelectors[202] = 0x01;
      expect(b.data(), expectedB.data(), b.size(), "opaque oriented fill B");
      expect(l.data(), expectedSelectors.data(), l.size(), "opaque oriented fill L");
      expect(m.data(), expectedSelectors.data(), m.size(), "opaque oriented fill M");
    }
  }
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  {
    GfxRenderer::ScopedTarget target(renderer, tuple, coherent);
    tuple.fill(0);
    for (int x = 0; x < 8; ++x) renderer.drawCoverage(x, 0, x % 4);
    renderer.fillRect(1, 0, 2, 1, false);
    const uint8_t eb = 0xEC, el = 0x02, em = 0x06;
    expect(b.data(), &eb, 1, "opaque single-byte fill B");
    expect(l.data(), &el, 1, "opaque single-byte fill L");
    expect(m.data(), &em, 1, "opaque single-byte fill M");
    tuple.fill(0);
    for (int x = 0; x < 8; ++x) renderer.drawCoverage(x, 0, x % 4);
    renderer.invertScreen();
    const uint8_t ib = 0x33, il = 0x44, im = 0x66;
    expect(b.data(), &ib, 1, "renderer inversion B");
    expect(l.data(), &il, 1, "renderer inversion L");
    expect(m.data(), &im, 1, "renderer inversion M");
    renderer.invertScreen();
    require(b[0] == 0xCC && l[0] == 0x22 && m[0] == 0x66, "renderer double inversion");
    renderer.drawPixel(2, 0, false);
    require(b[0] == 0xEC && l[0] == 0x02 && m[0] == 0x46, "opaque pixel clears selectors");
  }
  expect(physical.data(), live.data(), live.size(), "opaque offscreen preserves HAL");
}

static void testOpaqueImages(GfxRenderer& renderer) {
  std::array<uint8_t, 48000> b{}, l{}, m{}, expectedB{}, expectedSelectors{};
  GrayFrame tuple{{b.data(), b.size()}, {l.data(), l.size()}, {m.data(), m.size()}, 800, 480, 100};
  const int origins[][2] = {{473, 9}, {773, 473}, {4, 773}, {9, 4}};
  const int clips[][4] = {{474, 10, 2, 11}, {779, 474, 11, 2}, {4, 779, 2, 11}, {10, 4, 11, 2}};
  const uint8_t image[] = {0xA5, 0x5A, 0xFF, 0x00};
  bool coherent = true;
  auto live = physical;
  for (unsigned i = 0; i < 4; ++i) {
    renderer.setOrientation(writerOrientations[i]);
    GfxRenderer::ScopedTarget target(renderer, tuple, coherent);
    tuple.fill(2); expectedB.fill(0); expectedSelectors.fill(0xFF);
    renderer.drawImage(image, origins[i][0], origins[i][1], 17, 2);
    expectedB[401] = 0xA5; expectedB[402] = 0x5A; expectedB[501] = 0xFF;
    expectedSelectors[401] = expectedSelectors[402] = expectedSelectors[501] = expectedSelectors[502] = 0;
    expect(b.data(), expectedB.data(), b.size(), "offscreen image B");
    expect(l.data(), expectedSelectors.data(), l.size(), "offscreen image L");
    expect(m.data(), expectedSelectors.data(), m.size(), "offscreen image M");
    expect(physical.data(), live.data(), live.size(), "offscreen image preserves HAL");
    tuple.fill(2); expectedB.fill(0); expectedSelectors.fill(0xFF);
    const int* clip = clips[i];
    renderer.setGrayscaleClipRect(clip[0], clip[1], clip[2], clip[3]);
    renderer.drawImage(image, origins[i][0], origins[i][1], 17, 2);
    expectedB[401] = 0x25; expectedB[402] = 0x58; expectedB[501] = 0x3F;
    expectedSelectors[401] = expectedSelectors[501] = 0xC0;
    expectedSelectors[402] = expectedSelectors[502] = 0x07;
    expect(b.data(), expectedB.data(), b.size(), "clipped image B");
    expect(l.data(), expectedSelectors.data(), l.size(), "clipped image L");
    expect(m.data(), expectedSelectors.data(), m.size(), "clipped image M");
  }
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
}

static void testImageBand(GfxRenderer& renderer) {
  std::array<uint8_t, 100> band{};
  auto live = physical;
  const uint8_t image[] = {0, 0x5A, 0xFF};
  GfxRenderer::ScopedTarget target(renderer, band.data(), 4, 1);
  renderer.drawImage(image, 8, 3, 8, 3);
  std::array<uint8_t, 100> expected{}; expected[1] = 0x5A;
  expect(band.data(), expected.data(), band.size(), "image clipped to band origin");
  expect(physical.data(), live.data(), live.size(), "band image preserves HAL");
}

static HalFile bitmapFile(bool binary) {
  const uint32_t paletteBytes = binary ? 8 : 16;
  std::vector<uint8_t> bytes(54 + paletteBytes + 4);
  auto put = [&](size_t offset, uint32_t value, unsigned count) {
    for (unsigned i = 0; i < count; ++i) bytes[offset + i] = value >> (8 * i);
  };
  put(0, 0x4D42, 2); put(2, bytes.size(), 4); put(10, 54 + paletteBytes, 4);
  put(14, 40, 4); put(18, 4, 4); put(22, uint32_t(-1), 4);
  put(26, 1, 2); put(28, binary ? 1 : 2, 2);
  for (unsigned i = 0; i < (binary ? 2u : 4u); ++i)
    for (unsigned channel = 0; channel < 3; ++channel) bytes[54 + i * 4 + channel] = i * (binary ? 255 : 85);
  bytes[54 + paletteBytes] = binary ? 0xA0 : 0xD8;
  return HalFile(std::move(bytes));
}

static void testTransparentWriters(GfxRenderer& renderer) {
  std::array<uint8_t, 48000> b{}, l{}, m{};
  GrayFrame tuple{{b.data(), b.size()}, {l.data(), l.size()}, {m.data(), m.size()}, 800, 480, 100};
  bool coherent = true;
  {
    GfxRenderer::ScopedTarget target(renderer, tuple, coherent);
    tuple.fill(3);
    renderer.drawCoverage(0, 0, 1, false);
    DirectPixelWriter direct; direct.init(renderer); direct.beginRow(0);
    const uint8_t samples[] = {3, 1, 2, 0};
    for (int x = 0; x < 4; ++x) direct.writePixel(x, samples[x]);
    require(b[0] == 0x20 && l[0] == 0xC0 && m[0] == 0xE0, "direct white skips and shades replace black");
    tuple.fill(3); renderer.drawCoverage(0, 0, 1, false);
    auto file = bitmapFile(false); Bitmap bitmap(file);
    require(bitmap.parseHeaders() == BmpReaderError::Ok, "real gray bitmap fixture parses");
    renderer.drawBitmap(bitmap, 0, 0, 0, 0);
    require(b[0] == 0x20 && l[0] == 0xC0 && m[0] == 0xE0, "bitmap white skips and shades replace black");
    tuple.fill(2);
    auto monoFile = bitmapFile(true); Bitmap mono(monoFile);
    require(mono.parseHeaders() == BmpReaderError::Ok, "real mono bitmap fixture parses");
    renderer.drawBitmap1Bit(mono, 0, 0, 0, 0);
    require(b[0] == 0 && l[0] == 0xAF && m[0] == 0xAF, "mono bitmap white preserves gray");
    tuple.fill(2);
    const uint8_t icon[] = {0x7F, 0xFF};
    renderer.drawIcon(icon, 0, 0, 2);
    require(b[0] == 0 && l[0] == 0xBF && m[0] == 0xBF, "icon transparent white preserves gray");
  }
  std::array<uint8_t, 100> ls{}, ms{};
  {
    GfxRenderer::ScopedTarget target(renderer, ls.data(), 0, 1, ms.data());
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_BOTH);
    auto file = bitmapFile(false); Bitmap bitmap(file);
    require(bitmap.parseHeaders() == BmpReaderError::Ok, "dual bitmap parses");
    renderer.drawBitmap(bitmap, 0, 0, 0, 0);
    require(ls[0] == 0x40 && ms[0] == 0x60, "bitmap dual-plane selectors");
  }
}


static void testImageSampleOrientations(GfxRenderer& renderer) {
  std::array<uint8_t, 48000> b{}, l{}, m{}, eb{}, el{}, em{};
  GrayFrame tuple{{b.data(), b.size()}, {l.data(), l.size()}, {m.data(), m.size()}, 800, 480, 100};
  const int offsets[][4] = {{47900, 47800, 47700, 47600}, {47999, 47999, 47999, 47999},
                           {99, 199, 299, 399}, {0, 0, 0, 0}};
  const uint8_t masks[][4] = {{0x80, 0x80, 0x80, 0x80}, {1, 2, 4, 8}, {1, 1, 1, 1}, {0x80, 0x40, 0x20, 0x10}};
  const uint8_t samples[] = {3, 1, 2, 0};
  bool coherent = true;
  for (unsigned i = 0; i < 4; ++i) {
    renderer.setOrientation(writerOrientations[i]);
    GfxRenderer::ScopedTarget target(renderer, tuple, coherent);
    for (bool bitmapSource : {false, true}) {
      tuple.fill(2); eb.fill(0); el.fill(0xFF); em.fill(0xFF);
      if (bitmapSource) {
        auto file = bitmapFile(false); Bitmap bitmap(file);
        require(bitmap.parseHeaders() == BmpReaderError::Ok, "oriented bitmap parses");
        renderer.drawBitmap(bitmap, 0, 0, 0, 0);
      } else {
        DirectPixelWriter direct; direct.init(renderer); direct.beginRow(0);
        for (int x = 0; x < 4; ++x) direct.writePixel(x, samples[x]);
      }
      eb[offsets[i][2]] |= masks[i][2];
      el[offsets[i][2]] &= ~masks[i][2];
      el[offsets[i][3]] &= ~masks[i][3]; em[offsets[i][3]] &= ~masks[i][3];
      expect(b.data(), eb.data(), b.size(), "oriented image sample B");
      expect(l.data(), el.data(), l.size(), "oriented image sample L");
      expect(m.data(), em.data(), m.size(), "oriented image sample M");
    }
  }
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
}

static void testWriterOwnerControls(GfxRenderer& renderer) {
  std::array<uint8_t, 100> strip{}, secondary{};
  for (auto mode : {GfxRenderer::BW, GfxRenderer::BW_GRAY_BASE, GfxRenderer::GRAYSCALE_LSB,
                    GfxRenderer::GRAYSCALE_MSB, GfxRenderer::GRAYSCALE_BOTH}) {
    GfxRenderer::ScopedTarget target(renderer, strip.data(), 0, 1, secondary.data());
    renderer.setRenderMode(mode);
    renderer.clearScreen(mode < GfxRenderer::GRAYSCALE_LSB ? 0xFF : 0);
    auto file = bitmapFile(false); Bitmap bitmap(file);
    require(bitmap.parseHeaders() == BmpReaderError::Ok, "legacy bitmap parses");
    renderer.drawBitmap(bitmap, 0, 0, 0, 0);
    const uint8_t expected[] = {0x8F, 0xAF, 0x40, 0x60, 0x40};
    require(strip[0] == expected[mode], "AA-off bitmap legacy modes");
  }
  require(renderer.setUiGrayEnabled(true), "opaque live enable"); renderer.clearScreen();
  renderer.drawCoverage(1, 0, 2);
  renderer.fillRect(1, 0, 1, 1, false);
  require(physical[0] == 0xFF && renderer.getLiveGrayPlane(true)[0] == 0 &&
          renderer.getLiveGrayPlane(false)[0] == 0, "live opaque fill clears selectors");
  renderer.drawCoverage(1, 0, 2);
  const uint8_t* liveL = renderer.getLiveGrayPlane(true);
  const uint8_t* liveM = renderer.getLiveGrayPlane(false);
  for (auto owner : {GfxRenderer::FrameOwner::ReaderBase, GfxRenderer::FrameOwner::ReaderScratch}) {
    GfxRenderer::ScopedTarget scratch(renderer, owner);
    renderer.clearScreen(0);
    renderer.fillRect(0, 0, 24, 1, false);
    const uint8_t image[] = {0x5A}; renderer.drawImage(image, 0, 0, 8, 1);
    DirectPixelWriter direct; direct.init(renderer); direct.beginRow(0); direct.writePixel(1, 0);
    renderer.invertScreen();
    require(liveL[0] == 0x40 && liveM[0] == 0x40, "reader opaque writes preserve live selectors");
  }
  renderer.clearScreen();
}

static void testCachedWriter(GfxRenderer& renderer) {
  std::array<uint8_t, 100> a, b;
  a.fill(0xFF); b.fill(0xFF);
  auto live = physical;
  DirectPixelWriter direct;
  direct.init(renderer); direct.beginRow(0);
  {
    GfxRenderer::ScopedTarget first(renderer, a.data(), 0, 1);
    direct.beginRow(0);
    direct.writePixel(0, 0);
    expect(physical.data(), live.data(), live.size(), "stale direct writer preserves live");
    direct.init(renderer); direct.beginRow(0); direct.writePixel(0, 0);
    require(a[0] == 0x7F, "direct reinit writes current target");
    {
      GfxRenderer::ScopedTarget second(renderer, b.data(), 0, 1);
      direct.writePixel(1, 0);
      require(a[0] == 0x7F && b[0] == 0xFF, "stale direct writer within row rejects replacement");
    }
    direct.writePixel(2, 0);
    require(a[0] == 0x7F, "stale direct writer rejects restored target");
    direct.init(renderer); direct.beginRow(0); direct.writePixel(1, 0);
    require(a[0] == 0x3F, "direct reinit after restore");
  }
}

static void testOpaqueWriters(HalDisplay& hal) {
  GfxRenderer renderer(hal); renderer.begin(); renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  renderer.clearScreen();
  testOpaqueFills(renderer);
  testOpaqueImages(renderer);
  testImageBand(renderer);
  testTransparentWriters(renderer);
  testImageSampleOrientations(renderer);
  testCachedWriter(renderer);
  testWriterOwnerControls(renderer);
}


static void testLegacySnapshot(GfxRenderer& renderer) {
  using Result = GfxRenderer::FrameResult;
  GfxRenderer::FrameSnapshot snapshot;
  renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  renderer.clearScreen();
  physical.fill(0x22);
  renderer.copyGrayscaleLsbBuffers();
  physical.fill(0x66);
  renderer.copyGrayscaleMsbBuffers();
  physical.fill(0xCC);
  const int before = graySubmissions;
  renderer.displayGrayBuffer();
  require(graySubmissions == before + 1 && stagedL[0] == 0x22 && stagedM[0] == 0x66,
          "legacy snapshot follows actual gray submission");
  require(renderer.regionSnapshotBytes(1, 2, 5, 1) == 1 && renderer.frameSnapshotBytes() == 48000,
          "legacy snapshot uses one plane");
  std::array<uint8_t, 48002> packed;
  packed.fill(0xA5);
  require(renderer.captureFrame({packed.data() + 1, 48000}, snapshot) == Result::Ok && snapshot.valid,
          "legacy full capture succeeds");
  require(snapshot.kind == GfxRenderer::SnapshotKind::LegacyBw, "legacy capture truthfully reports BW");
  require(!snapshot.planes.l().data && !snapshot.planes.m().data && !snapshot.planes.valid(),
          "legacy capture invents no selectors");
  require(snapshot.planes.b().capacity == 48000 && snapshot.planes.stride() == 100 &&
          snapshot.planes.width() == 800 && snapshot.planes.rows() == 480 && snapshot.physicalByteX == 0 &&
          snapshot.planes.y0() == 0, "legacy full capture metadata");
  std::array<uint8_t, 48000> expected;
  expected.fill(0xCC);
  expect(packed.data() + 1, expected.data(), expected.size(), "legacy B literal");
  expect(physical.data(), expected.data(), expected.size(), "legacy capture preserves live B");
  require(packed.front() == 0xA5 && packed.back() == 0xA5, "legacy packed canaries");
}

static void testSnapshots(HalDisplay& hal) {
  using Result = GfxRenderer::FrameResult;
  using Snapshot = GfxRenderer::FrameSnapshot;
  for (int failure : {0, 1, 2}) {
    GfxRenderer fallback(hal);
    fallback.begin();
    if (failure) {
      failAllocation = allocationCalls + failure;
      require(!fallback.setUiGrayEnabled(true), "snapshot pair allocation failure remains disabled");
    }
    testLegacySnapshot(fallback);
    failAllocation = 0;
  }

  GfxRenderer renderer(hal);
  renderer.begin();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "snapshot enables retained tuple");
  Snapshot snapshot;
  std::array<uint8_t, 144002> packed;
  packed.fill(0xA5);
  const auto untouched = packed;
  require(renderer.frameSnapshotBytes() == 144000, "snapshot size ignores invalid live state");
  snapshot.valid = true;
  require(renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::InvalidLive && !snapshot.valid,
          "capture rejects invalid live and invalidates output");
  expect(packed.data(), untouched.data(), packed.size(), "invalid live preserves storage");
  renderer.beginDisplayWork();
  renderer.clearScreen();
  auto tuple = renderer.getWriteTuple();
  std::memset(tuple.b().data, 0xCC, 48000);
  std::memset(tuple.l().data, 0x22, 48000);
  std::memset(tuple.m().data, 0x66, 48000);
  tuple.b().data[201] = 0x33;
  tuple.l().data[201] = 0x44;
  tuple.b().data[600] = 0x33;
  tuple.b().data[700] = 0x55;
  tuple.l().data[600] = 0x44;
  tuple.l().data[700] = 0x88;
  tuple.m().data[600] = 0x99;
  tuple.m().data[700] = 0xAA;
  std::array<uint8_t, 48000> originalB, originalL, originalM;
  std::memcpy(originalB.data(), tuple.b().data, 48000);
  std::memcpy(originalL.data(), tuple.l().data, 48000);
  std::memcpy(originalM.data(), tuple.m().data, 48000);
  const int before = submissions + graySubmissions + baseSubmissions;
  require(renderer.regionSnapshotBytes(1, 2, 5, 1) == 3, "aligned padding uses three bytes");
  require(renderer.captureRegion(1, 2, 5, 1, {packed.data() + 1, 3}, snapshot) == Result::Ok && snapshot.valid,
          "aligned padding capture succeeds");
  const uint8_t padding[] = {0xCC, 0x22, 0x66};
  expect(packed.data() + 1, padding, 3, "capture literal aligned padding");
  require(snapshot.kind == GfxRenderer::SnapshotKind::RetainedTuple && snapshot.planes.valid() &&
          snapshot.planes.width() == 8 && snapshot.planes.stride() == 1 && snapshot.planes.rows() == 1 &&
          snapshot.planes.y0() == 2 && snapshot.physicalByteX == 0 && snapshot.panelWidth == 800 &&
          snapshot.panelHeight == 480 && snapshot.panelStride == 100 && snapshot.restoreEpoch == 2,
          "retained capture metadata");
  require(snapshot.planes.b().data == packed.data() + 1 && snapshot.planes.l().data == packed.data() + 2 &&
          snapshot.planes.m().data == packed.data() + 3 && snapshot.planes.b().capacity == 1 &&
          snapshot.planes.l().capacity == 1 && snapshot.planes.m().capacity == 1,
          "retained capture packed plane views");

  for (size_t capacity : {size_t(0), size_t(1), size_t(2)}) {
    packed.fill(0xA5);
    snapshot.valid = true;
    require(renderer.captureRegion(1, 2, 5, 1, {packed.data() + 1, capacity}, snapshot) == Result::ShortCapacity &&
            !snapshot.valid, "capture short capacity rejected");
    expect(packed.data(), untouched.data(), packed.size(), "short capacity preserves storage");
  }
  require(renderer.captureRegion(1, 2, 5, 1, {packed.data() + 1, std::numeric_limits<size_t>::max()}, snapshot) ==
          Result::Ok && packed[0] == 0xA5 && packed[4] == 0xA5, "capture validates used storage extent");
  for (uint8_t* plane : {tuple.b().data, tuple.l().data, tuple.m().data}) {
    for (size_t offset : {size_t(0), size_t(1), size_t(47999)}) {
      snapshot.valid = true;
      require(renderer.captureRegion(1, 2, 5, 1, {plane + offset, 3}, snapshot) == Result::InvalidSource &&
              !snapshot.valid, "capture offset alias rejected");
    }
  }
  for (uint8_t* invalid : {static_cast<uint8_t*>(nullptr),
                          reinterpret_cast<uint8_t*>(std::numeric_limits<uintptr_t>::max() - 1)}) {
    require(renderer.captureRegion(1, 2, 5, 1, {invalid, 3}, snapshot) == Result::InvalidSource,
            "capture invalid address rejected");
  }
  const GfxRenderer::Orientation orientations[] = {GfxRenderer::LandscapeCounterClockwise,
      GfxRenderer::LandscapeClockwise, GfxRenderer::Portrait, GfxRenderer::PortraitInverted};
  const int rectangles[][4] = {{3, 5, 5, 3}, {792, 472, 5, 3}, {472, 3, 3, 5}, {5, 792, 3, 5}};
  const size_t partialByteX[] = {0, 99, 0, 99};
  const size_t partialY[] = {0, 479, 479, 0};
  const uint8_t oriented[] = {0xCC, 0x33, 0x55, 0x22, 0x44, 0x88, 0x66, 0x99, 0xAA};
  const int invalidRects[][4] = {{INT_MIN, 0, 1, 1}, {INT_MAX - 1, 0, 1, 1}, {0, INT_MIN, 1, 1},
      {0, INT_MAX - 1, 1, 1}, {INT_MIN, 0, INT_MAX, 1}, {0, INT_MIN, 1, INT_MAX},
      {INT_MAX, INT_MAX, INT_MAX, INT_MAX}, {0, 0, 0, 1}, {0, 0, 1, -1}};
  for (size_t index = 0; index < 4; ++index) {
    renderer.setOrientation(orientations[index]);
    const int* r = rectangles[index];
    packed.fill(0xA5);
    require(renderer.regionSnapshotBytes(r[0], r[1], r[2], r[3]) == 9 &&
            renderer.captureRegion(r[0], r[1], r[2], r[3], {packed.data() + 1, 9}, snapshot) == Result::Ok,
            "oriented snapshot size and capture");
    require(snapshot.physicalByteX == 0 && snapshot.planes.y0() == 5 && snapshot.planes.stride() == 1 &&
            snapshot.planes.rows() == 3 && snapshot.orientation == orientations[index],
            "oriented snapshot physical coordinates");
    expect(packed.data() + 1, oriented, 9, "oriented snapshot literal planes");
    require(packed[0] == 0xA5 && packed[10] == 0xA5, "oriented snapshot canaries");
    require(renderer.regionSnapshotBytes(1, 1, INT_MAX, INT_MAX) > 0,
            "wide endpoint clipping remains visible");
    require(renderer.captureRegion(-2, -3, 3, 4, {packed.data() + 1, 3}, snapshot) == Result::Ok &&
            snapshot.physicalByteX == partialByteX[index] && snapshot.planes.y0() == partialY[index] &&
            snapshot.planes.rows() == 1 && snapshot.planes.stride() == 1,
            "partially visible capture clips before rotation");
    expect(packed.data() + 1, padding, 3, "partial clipping literal planes");
    require(renderer.captureRegion(renderer.getScreenWidth() - 1, renderer.getScreenHeight() - 1,
            INT_MAX, INT_MAX, {packed.data() + 1, 3}, snapshot) == Result::Ok,
            "wide endpoint capture clips before rotation");
    for (const auto& invalid : invalidRects) {
      packed.fill(0xA5);
      snapshot.valid = true;
      require(renderer.regionSnapshotBytes(invalid[0], invalid[1], invalid[2], invalid[3]) == 0,
              "extreme offscreen snapshot size is zero");
      require(renderer.captureRegion(invalid[0], invalid[1], invalid[2], invalid[3],
              {packed.data() + 1, 144000}, snapshot) == Result::InvalidSource && !snapshot.valid,
              "extreme offscreen capture rejected");
      expect(packed.data(), untouched.data(), packed.size(), "extreme offscreen preserves storage");
    }
  }
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  packed.fill(0xA5);
  require(renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::Ok && snapshot.valid &&
          snapshot.planes.width() == 800 && snapshot.planes.stride() == 100 && snapshot.planes.rows() == 480 &&
          snapshot.physicalByteX == 0 && snapshot.planes.y0() == 0, "full tuple capture metadata");
  expect(packed.data() + 1, originalB.data(), 48000, "full capture B");
  expect(packed.data() + 48001, originalL.data(), 48000, "full capture L");
  expect(packed.data() + 96001, originalM.data(), 48000, "full capture M");
  require(packed.front() == 0xA5 && packed.back() == 0xA5, "full capture canaries");
  packed.fill(0xA5);
  captureAbortDestination = packed.data() + 1;
  captureAbortRenderer = &renderer;
  require(renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::Cancelled && !snapshot.valid &&
          captureAbortHits == 1, "late capture abort invalidates descriptor");
  require(packed[1] == 0xCC, "late capture abort follows a real copy");
  expect(physical.data(), originalB.data(), 48000, "captures preserve live B");
  expect(tuple.l().data, originalL.data(), 48000, "captures preserve live L");
  expect(tuple.m().data, originalM.data(), 48000, "captures preserve live M");
  packed.fill(0xA5);
  snapshot.valid = true;
  require(renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::Cancelled && !snapshot.valid,
          "pre-cancelled capture rejected");
  expect(packed.data(), untouched.data(), packed.size(), "pre-cancelled capture preserves storage");
  renderer.beginDisplayWork();
  renderer.clearScreen();
  for (auto owner : {GfxRenderer::FrameOwner::ReaderBase, GfxRenderer::FrameOwner::ReaderScratch}) {
    GfxRenderer::ScopedTarget scope(renderer, owner);
    snapshot.valid = true;
    require(renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::Unavailable && !snapshot.valid,
            "reader ownership rejects snapshot");
  }
  std::array<uint8_t, 100> strip{};
  {
    GfxRenderer::ScopedTarget scope(renderer, strip.data(), 0, 1);
    require(renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::Unavailable,
            "offscreen strip rejects snapshot");
  }
  {
    bool coherent = true;
    GrayFrame offscreen({packed.data() + 1, 100}, {packed.data() + 101, 100}, {packed.data() + 201, 100},
                        800, 1, 100);
    GfxRenderer::ScopedTarget scope(renderer, offscreen, coherent);
    require(scope.active() && renderer.captureFrame({packed.data() + 301, 144000}, snapshot) == Result::Unavailable,
            "offscreen tuple rejects snapshot");
  }
  {
    GfxRenderer::FrameBufferLoan loan(renderer);
    require(renderer.frameSnapshotBytes() == 144000 &&
            renderer.captureFrame({packed.data() + 1, 144000}, snapshot) == Result::Unavailable,
            "loan rejects capture but permits sizing");
  }
  expect(packed.data(), untouched.data(), packed.size(), "unavailable captures preserve storage");
  require(submissions + graySubmissions + baseSubmissions == before, "captures submit no display work");
}

static void testRegionSnapshots(HalDisplay& hal) {
  using Result = GfxRenderer::FrameResult;
  using Snapshot = GfxRenderer::FrameSnapshot;
  GfxRenderer renderer(hal);
  renderer.begin();
  renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "restore enables retained tuple");
  renderer.clearScreen();
  auto tuple = renderer.getWriteTuple();
  std::memset(tuple.b().data, 0xCC, 48000);
  std::memset(tuple.l().data, 0x22, 48000);
  std::memset(tuple.m().data, 0x66, 48000);
  tuple.b().data[201] = 0x33;
  tuple.l().data[201] = 0x44;
  std::array<uint8_t, 5> packed{0xA5, 0, 0, 0, 0xA5};
  Snapshot snapshot;
  require(renderer.captureRegion(1, 2, 5, 1, {packed.data() + 1, 3}, snapshot) == Result::Ok,
          "restore captures aligned byte");
  const auto savedStorage = packed;
  std::array<uint8_t, 48000> originalB, originalL, originalM;
  std::memcpy(originalB.data(), tuple.b().data, 48000);
  std::memcpy(originalL.data(), tuple.l().data, 48000);
  std::memcpy(originalM.data(), tuple.m().data, 48000);
  tuple.b().data[200] = tuple.l().data[200] = tuple.m().data[200] = 0;
  const int before = submissions + graySubmissions + baseSubmissions;
  require(renderer.restoreRegion(snapshot) == Result::Ok, "restore aligned byte accepted");
  expect(tuple.b().data, originalB.data(), 48000, "restore literal B padding and neighbor");
  expect(tuple.l().data, originalL.data(), 48000, "restore literal L padding and neighbor");
  expect(tuple.m().data, originalM.data(), 48000, "restore literal M padding and neighbor");
  expect(packed.data(), savedStorage.data(), packed.size(), "restore preserves source and canaries");

  auto reject = [&](const Snapshot& source, Result result, const char* name) {
    std::array<uint8_t, 48000> b, l, m;
    std::memcpy(b.data(), tuple.b().data, 48000);
    std::memcpy(l.data(), tuple.l().data, 48000);
    std::memcpy(m.data(), tuple.m().data, 48000);
    require(renderer.restoreRegion(source) == result, name);
    expect(tuple.b().data, b.data(), 48000, "rejected restore preserves B");
    expect(tuple.l().data, l.data(), 48000, "rejected restore preserves L");
    expect(tuple.m().data, m.data(), 48000, "rejected restore preserves M");
    expect(packed.data(), savedStorage.data(), packed.size(), "rejected restore preserves source and canaries");
  };
  for (int plane = 0; plane < 3; ++plane) {
    GrayFrame::Plane views[] = {snapshot.planes.b(), snapshot.planes.l(), snapshot.planes.m()};
    views[plane].capacity = 0;
    Snapshot bad = snapshot;
    bad.planes = GrayFrame(views[0], views[1], views[2], 8, 1, 1, 2);
    reject(bad, Result::ShortCapacity, "restore short source rejected");
    views[plane] = {nullptr, 1};
    bad.planes = GrayFrame(views[0], views[1], views[2], 8, 1, 1, 2);
    reject(bad, Result::InvalidSource, "restore null source rejected");
    views[plane] = {reinterpret_cast<uint8_t*>(std::numeric_limits<uintptr_t>::max()), 1};
    bad.planes = GrayFrame(views[0], views[1], views[2], 8, 1, 1, 2);
    reject(bad, Result::InvalidSource, "restore source address overflow rejected");
  }
  for (auto destination : {tuple.b().data, tuple.l().data, tuple.m().data}) {
    for (size_t offset : {size_t(0), size_t(17), size_t(47999)}) {
      Snapshot bad = snapshot;
      bad.planes = GrayFrame({destination + offset, 1}, snapshot.planes.l(), snapshot.planes.m(), 8, 1, 1, 2);
      reject(bad, Result::InvalidSource, "restore offset alias rejected");
    }
  }
  for (int first = 0; first < 3; ++first) {
    for (int second = first + 1; second < 3; ++second) {
      GrayFrame::Plane views[] = {snapshot.planes.b(), snapshot.planes.l(), snapshot.planes.m()};
      views[second] = views[first];
      Snapshot bad = snapshot;
      bad.planes = GrayFrame(views[0], views[1], views[2], 8, 1, 1, 2);
      reject(bad, Result::InvalidSource, "restore source planes overlap rejected");
    }
  }
  std::array<uint8_t, 16> overlap{};
  Snapshot bad = snapshot;
  bad.planes = GrayFrame({overlap.data(), 4}, {overlap.data() + 3, 4}, {overlap.data() + 8, 4}, 16, 2, 2, 2);
  reject(bad, Result::InvalidSource, "restore offset source planes overlap rejected");
  require(overlap == std::array<uint8_t, 16>{}, "overlap rejection preserves source");
  bad = snapshot; bad.valid = false;
  reject(bad, Result::InvalidSource, "restore invalid descriptor rejected");
  bad = snapshot; bad.kind = static_cast<GfxRenderer::SnapshotKind>(99);
  reject(bad, Result::InvalidSource, "restore unknown kind rejected");
  bad = snapshot; bad.kind = GfxRenderer::SnapshotKind::LegacyBw;
  reject(bad, Result::PolicyMismatch, "restore BW under retained policy rejected");
  for (int field = 0; field < 4; ++field) {
    bad = snapshot;
    if (field == 0) --bad.panelWidth;
    if (field == 1) --bad.panelHeight;
    if (field == 2) --bad.panelStride;
    if (field == 3) bad.orientation = GfxRenderer::Portrait;
    reject(bad, Result::GeometryMismatch, "restore mismatched panel geometry rejected");
  }
  for (size_t byteX : {size_t(100), std::numeric_limits<size_t>::max()}) {
    bad = snapshot; bad.physicalByteX = byteX;
    reject(bad, Result::InvalidSource, "restore invalid byte origin rejected");
  }
  const size_t invalidGeometry[][4] = {{0,1,1,2}, {801,1,101,2}, {7,1,1,2}, {8,0,1,2}, {8,479,1,2},
      {8,1,0,2}, {8,1,2,2}, {8,1,1,480}, {8,SIZE_MAX,1,2}, {8,1,SIZE_MAX,2}, {SIZE_MAX,1,1,2}};
  for (const auto& g : invalidGeometry) {
    bad = snapshot;
    bad.planes = GrayFrame(snapshot.planes.b(), snapshot.planes.l(), snapshot.planes.m(), g[0], g[1], g[2], g[3]);
    reject(bad, Result::InvalidSource, "restore malformed plane geometry rejected");
  }
  bad = snapshot;
  bad.planes = GrayFrame({packed.data() + 1, SIZE_MAX}, {packed.data() + 2, SIZE_MAX},
                        {packed.data() + 3, SIZE_MAX}, 8, 1, 1, 2);
  require(renderer.restoreRegion(bad) == Result::Ok, "restore checks used extent instead of full capacity");

  std::array<uint8_t, 8> multirow{0xA5, 0, 0, 0, 0, 0, 0, 0xA5};
  Snapshot twoRows;
  tuple.b().data[501] = 0xCC; tuple.b().data[601] = 0x33;
  tuple.l().data[501] = 0x22; tuple.l().data[601] = 0x44;
  tuple.m().data[501] = 0x66; tuple.m().data[601] = 0x77;
  require(renderer.captureRegion(9, 5, 5, 2, {multirow.data() + 1, 6}, twoRows) == Result::Ok,
          "restore captures multiple rows at nonzero byte origin");
  const uint8_t literalRows[] = {0xCC, 0x33, 0x22, 0x44, 0x66, 0x77};
  expect(multirow.data() + 1, literalRows, 6, "multirow source literal bytes");
  for (auto destination : {tuple.b().data, tuple.l().data, tuple.m().data}) {
    destination[501] = destination[601] = 0;
    destination[500] = destination[502] = destination[600] = destination[602] = 0xA5;
  }
  require(renderer.restoreRegion(twoRows) == Result::Ok, "restore multiple rows accepted");
  const uint8_t restoredRows[] = {tuple.b().data[501], tuple.b().data[601], tuple.l().data[501],
      tuple.l().data[601], tuple.m().data[501], tuple.m().data[601]};
  expect(restoredRows, literalRows, 6, "restore uses physical byte origin and panel row stride");
  for (auto destination : {tuple.b().data, tuple.l().data, tuple.m().data})
    require(destination[500] == 0xA5 && destination[502] == 0xA5 &&
            destination[600] == 0xA5 && destination[602] == 0xA5, "multirow restore preserves neighboring bytes");
  require(multirow.front() == 0xA5 && multirow.back() == 0xA5, "multirow restore preserves source canaries");

  for (auto owner : {GfxRenderer::FrameOwner::ReaderBase, GfxRenderer::FrameOwner::ReaderScratch}) {
    GfxRenderer::ScopedTarget scope(renderer, owner);
    reject(snapshot, Result::Unavailable, "reader target rejects region restore");
  }
  reject(snapshot, Result::InvalidLive, "restore rejects invalid live with matching epoch");
  renderer.clearScreen();
  require(renderer.restoreRegion(snapshot) == Result::Ok, "clear repairs live without changing epoch");
  std::array<uint8_t, 300> offscreen{};
  {
    GfxRenderer::ScopedTarget scope(renderer, offscreen.data(), 0, 1);
    reject(snapshot, Result::Unavailable, "strip target rejects region restore");
  }
  {
    bool coherent = true;
    GrayFrame frame({offscreen.data(), 100}, {offscreen.data() + 100, 100}, {offscreen.data() + 200, 100}, 800, 1, 100);
    GfxRenderer::ScopedTarget scope(renderer, frame, coherent);
    reject(snapshot, Result::Unavailable, "offscreen target rejects region restore");
    renderer.abortDisplayWork();
  }
  renderer.beginDisplayWork();
  require(renderer.restoreRegion(snapshot) == Result::Ok, "offscreen cancellation preserves live epoch");

  const uint64_t originalEpoch = snapshot.restoreEpoch;
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "restore unchanged enabled policy accepted");
  renderer.beginDisplayWork();
  renderer.clearScreen();
  require(renderer.restoreRegion(snapshot) == Result::Ok, "clear and new work preserve region epoch");
  Snapshot current;
  std::array<uint8_t, 3> currentStorage{};
  auto captureCurrent = [&]() {
    require(renderer.captureRegion(1, 2, 5, 1, {currentStorage.data(), 3}, current) == Result::Ok,
            "capture current restore epoch");
  };
  captureCurrent();
  require(current.restoreEpoch == originalEpoch, "no-op controls preserve epoch");
  renderer.setOrientation(GfxRenderer::Portrait);
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  reject(snapshot, Result::InvalidLive, "restore stale orientation epoch rejected");
  captureCurrent();
  require(current.restoreEpoch == originalEpoch + 2, "orientation away and back advances epoch twice");
  snapshot = current;
  require(renderer.setUiGrayEnabled(false) && renderer.setUiGrayEnabled(false) && renderer.setUiGrayEnabled(true),
          "policy away and back accepted");
  reject(snapshot, Result::InvalidLive, "restore cannot repair invalid live");
  renderer.clearScreen();
  reject(snapshot, Result::InvalidLive, "restore stale policy epoch rejected after clear");
  captureCurrent();
  require(current.restoreEpoch == originalEpoch + 4, "only actual policy changes advance epoch");
  snapshot = current;
  {
    GfxRenderer::FrameBufferLoan outer(renderer);
    require(outer.active(), "outer loan acquires storage");
    GfxRenderer::FrameBufferLoan inner(renderer);
    require(!inner.active(), "nested loan reports inactive");
    inner.end();
    require(!renderer.hasFrameBuffer() && outer.active(), "inactive inner loan cannot return outer storage");
    require(renderer.restoreRegion(snapshot) == Result::Unavailable, "loan rejects restore");
    outer.end(); outer.end();
    require(!outer.active() && renderer.hasFrameBuffer(), "loan end is idempotent");
  }
  for (size_t i = 0; i < 48000; ++i)
    require(tuple.b().data[i] == 0xFF && tuple.l().data[i] == 0 && tuple.m().data[i] == 0,
            "loan return restores white B and zero selectors");
  reject(snapshot, Result::InvalidLive, "loan return requires redraw before restore");
  renderer.clearScreen();
  reject(snapshot, Result::InvalidLive, "restore stale loan epoch rejected after clear");
  captureCurrent();
  require(current.restoreEpoch == originalEpoch + 5, "only successful loan acquisition advances epoch");

  snapshot = current;
  const uint64_t beforeAbortEpoch = current.restoreEpoch;
  renderer.abortDisplayWork();
  require(renderer.liveFrameValid(), "input abort leaves render-thread validity unchanged");
  reject(snapshot, Result::Cancelled, "pre-aborted restore rejected without writes");
  require(renderer.liveFrameNeedsRedraw(), "render thread accounts live cancellation");
  renderer.beginDisplayWork(); renderer.clearScreen();
  reject(snapshot, Result::InvalidLive, "restore stale cancellation epoch rejected after clear");
  captureCurrent();
  require(current.restoreEpoch == beforeAbortEpoch + 1, "live cancellation advances epoch once");

  tuple.b().data[200] = 0xCC; tuple.l().data[200] = 0x22; tuple.m().data[200] = 0x66;
  require(renderer.captureRegion(1, 2, 5, 1, {currentStorage.data(), 3}, current) == Result::Ok,
          "late restore captures source");
  tuple.b().data[200] = 0x33; tuple.l().data[200] = 0x11; tuple.m().data[200] = 0x55;
  restoreAbortL = tuple.l().data + 200; restoreAbortM = tuple.m().data + 200;
  captureAbortDestination = tuple.b().data + 200;
  captureAbortHits = 0; restoreAbortSawMixed = false; captureAbortRenderer = &renderer;
  require(renderer.restoreRegion(current) == Result::PublishedCancelled,
          "mid-copy abort reports accepted cancelled restore");
  require(captureAbortHits == 1 && restoreAbortSawMixed, "restore abort observes new B and old selectors exactly once");
  const uint8_t finalTuple[] = {tuple.b().data[200], tuple.l().data[200], tuple.m().data[200]};
  const uint8_t literalTuple[] = {0xCC, 0x22, 0x66};
  expect(finalTuple, literalTuple, 3, "accepted cancelled restore completes tuple");
  require(renderer.liveFrameNeedsRedraw(), "accepted cancelled restore invalidates live");
  renderer.displayBuffer();
  require(submissions + graySubmissions + baseSubmissions == before, "region restores never submit HAL work");
  restoreAbortL = restoreAbortM = nullptr;

  renderer.beginDisplayWork(); renderer.clearScreen();
  require(renderer.setUiGrayEnabled(false), "restore disables tuple policy");
  renderer.clearScreen();
  std::array<uint8_t, 1> legacy{0xCC};
  require(renderer.captureRegion(1, 2, 5, 1, {legacy.data(), 1}, snapshot) == Result::Ok,
          "legacy restore captures single plane");
  legacy[0] = 0xCC; tuple.b().data[200] = 0x33;
  require(renderer.restoreRegion(snapshot) == Result::Ok && tuple.b().data[200] == 0xCC,
          "legacy restore accepts absent selectors");
  snapshot.kind = GfxRenderer::SnapshotKind::RetainedTuple;
  reject(snapshot, Result::PolicyMismatch, "restore tuple under BW policy rejected");
}


static void testFrameReplacement(HalDisplay& hal) {
  using Result = GfxRenderer::FrameResult;
  using Snapshot = GfxRenderer::FrameSnapshot;
  GfxRenderer renderer(hal);
  renderer.begin(); renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "replacement enables retained tuple");
  renderer.clearScreen();
  const auto live = renderer.getWriteTuple();
  std::array<uint8_t, 144002> storage;
  storage.fill(0xA5);
  std::memset(storage.data() + 1, 0xCC, 48000);
  std::memset(storage.data() + 48001, 0x22, 48000);
  std::memset(storage.data() + 96001, 0x66, 48000);
  const auto original = storage;
  Snapshot source;
  source.planes = GrayFrame({storage.data() + 1, 48000}, {storage.data() + 48001, 48000},
                           {storage.data() + 96001, 48000}, 800, 480, 100);
  source.panelWidth = 800; source.panelHeight = 480; source.panelStride = 100;
  source.orientation = GfxRenderer::LandscapeCounterClockwise;
  source.kind = GfxRenderer::SnapshotKind::RetainedTuple;
  source.restoreEpoch = UINT64_MAX; source.valid = true;
  auto seedOld = [&]() {
    std::memset(live.b().data, 0x33, 48000);
    std::memset(live.l().data, 0x44, 48000);
    std::memset(live.m().data, 0x66, 48000);
  };
  auto expectPublished = [&]() {
    expect(live.b().data, original.data() + 1, 48000, "replacement complete B");
    expect(live.l().data, original.data() + 48001, 48000, "replacement complete L");
    expect(live.m().data, original.data() + 96001, 48000, "replacement complete M");
  };
  auto halActivity = [&]() { return submissions + graySubmissions + baseSubmissions + grayCopies + workStarts; };
  auto reject = [&](const Snapshot& candidate, Result result, const char* name) {
    std::array<uint8_t, 144000> before;
    std::memcpy(before.data(), live.b().data, 48000);
    std::memcpy(before.data() + 48000, live.l().data, 48000);
    std::memcpy(before.data() + 96000, live.m().data, 48000);
    const int calls = halActivity();
    require(renderer.replaceFrame(candidate) == result, name);
    expect(live.b().data, before.data(), 48000, "rejected replacement preserves B");
    expect(live.l().data, before.data() + 48000, 48000, "rejected replacement preserves L");
    expect(live.m().data, before.data() + 96000, 48000, "rejected replacement preserves M");
    expect(storage.data(), original.data(), storage.size(), "replacement preserves source and canaries");
    require(halActivity() == calls, "rejected replacement has no HAL activity");
  };
  seedOld();
  Snapshot region, current;
  std::array<uint8_t, 3> regionStorage{}, currentStorage{};
  require(renderer.captureRegion(0, 0, 8, 1, {regionStorage.data(), 3}, region) == Result::Ok,
          "replacement captures old region epoch");
  DirectPixelWriter stale;
  stale.init(renderer); stale.beginRow(0);
  const auto generation = renderer.getTargetGeneration();
  const int before = halActivity();
  require(renderer.replaceFrame(source) == Result::Ok, "complete replacement accepted");
  expectPublished();
  require(renderer.liveFrameValid() && renderer.getTargetGeneration() == generation + 1,
          "replacement publishes coherence and invalidates writers");
  stale.writePixel(0, 0);
  expectPublished();
  require(renderer.restoreRegion(region) == Result::InvalidLive, "replacement invalidates old region epoch");
  require(renderer.captureRegion(0, 0, 8, 1, {currentStorage.data(), 3}, current) == Result::Ok &&
          current.restoreEpoch == region.restoreEpoch + 1, "replacement advances region epoch once");
  require(halActivity() == before, "replacement makes no HAL call or new generation");
  std::memset(storage.data() + 1, 0, 144000);
  expectPublished();
  storage = original;
  renderer.displayBuffer();
  require(halActivity() == before + 4, "explicit display submits complete replacement");
  expect(stagedB.data(), original.data() + 1, 48000, "replacement submitted B");
  expect(stagedL.data(), original.data() + 48001, 48000, "replacement submitted L");
  expect(stagedM.data(), original.data() + 96001, 48000, "replacement submitted M");

  seedOld();
  Snapshot bad = source; bad.valid = false;
  reject(bad, Result::InvalidSource, "replacement invalid slot rejected");
  bad = source; bad.kind = static_cast<GfxRenderer::SnapshotKind>(99);
  reject(bad, Result::InvalidSource, "replacement unknown kind rejected");
  bad = source; bad.kind = GfxRenderer::SnapshotKind::LegacyBw;
  reject(bad, Result::PolicyMismatch, "replacement BW under tuple policy rejected");
  for (int plane = 0; plane < 3; ++plane) {
    GrayFrame::Plane views[] = {source.planes.b(), source.planes.l(), source.planes.m()};
    views[plane].capacity = 47999;
    bad = source; bad.planes = GrayFrame(views[0], views[1], views[2], 800, 480, 100);
    reject(bad, Result::ShortCapacity, "replacement short source rejected");
    views[plane] = {nullptr, 48000};
    bad.planes = GrayFrame(views[0], views[1], views[2], 800, 480, 100);
    reject(bad, Result::InvalidSource, "replacement missing source rejected");
    for (auto destination : {live.b().data, live.l().data, live.m().data}) {
      views[plane] = {destination + 17, 48000};
      bad.planes = GrayFrame(views[0], views[1], views[2], 800, 480, 100);
      reject(bad, Result::InvalidSource, "replacement offset alias rejected");
    }
  }
  bad = source;
  bad.planes = GrayFrame(source.planes.b(), {source.planes.b().data + 17, 48000}, source.planes.m(), 800, 480, 100);
  reject(bad, Result::InvalidSource, "replacement overlapping source planes rejected");
  for (int field = 0; field < 4; ++field) {
    bad = source;
    if (field == 0) --bad.panelWidth;
    if (field == 1) --bad.panelHeight;
    if (field == 2) --bad.panelStride;
    if (field == 3) bad.orientation = GfxRenderer::Portrait;
    reject(bad, Result::GeometryMismatch, "replacement saved geometry rejected");
  }
  const size_t partial[][5] = {{792, 480, 99, 0, 0}, {800, 479, 100, 0, 0},
                              {792, 480, 99, 0, 1}, {800, 479, 100, 1, 0}};
  for (const auto& dimensions : partial) {
    bad = source; bad.physicalByteX = dimensions[4];
    bad.planes = GrayFrame(source.planes.b(), source.planes.l(), source.planes.m(),
                          dimensions[0], dimensions[1], dimensions[2], dimensions[3]);
    reject(bad, Result::GeometryMismatch, "replacement requires full physical coverage");
  }
  renderer.abortDisplayWork();
  reject(source, Result::Cancelled, "pre-cancelled replacement rejected");
  require(renderer.liveFrameValid(), "rejected cancellation does not invalidate live");
  renderer.beginDisplayWork();
  require(renderer.liveFrameNeedsRedraw() && renderer.replaceFrame(source) == Result::Ok,
          "fresh replacement repairs accounted live cancellation");

  std::array<uint8_t, 300> offscreen{};
  for (auto owner : {GfxRenderer::FrameOwner::ReaderBase, GfxRenderer::FrameOwner::ReaderScratch}) {
    GfxRenderer::ScopedTarget scope(renderer, owner);
    reject(source, Result::Unavailable, "reader owner rejects replacement");
  }
  require(renderer.liveFrameNeedsRedraw() && renderer.restoreRegion(current) == Result::InvalidLive,
          "region cannot repair scratch invalidity");
  renderer.beginDisplayWork();
  require(renderer.replaceFrame(source) == Result::Ok && renderer.liveFrameValid(),
          "replacement repairs scratch invalidity");
  expectPublished();
  {
    GfxRenderer::ScopedTarget scope(renderer, offscreen.data(), 0, 1);
    reject(source, Result::Unavailable, "strip rejects replacement");
  }
  for (bool inputAbort : {false, true}) {
    bool coherent = true;
    {
      GrayFrame slot({offscreen.data(), 100}, {offscreen.data() + 100, 100},
                     {offscreen.data() + 200, 100}, 800, 1, 100);
      GfxRenderer::ScopedTarget scope(renderer, slot, coherent);
      require(scope.active(), "publication offscreen scope active");
      reject(source, Result::Unavailable, "offscreen scope rejects replacement");
      if (inputAbort) renderer.abortDisplayWork(); else scope.cancel();
    }
    require(!coherent, "cancelled offscreen slot invalid");
    bad = source; bad.valid = coherent;
    reject(bad, inputAbort ? Result::Cancelled : Result::InvalidSource,
           "cancelled offscreen slot publication rejected");
    require(renderer.liveFrameValid(), "cancelled offscreen publication preserves live validity");
    renderer.beginDisplayWork();
  }
  {
    GfxRenderer::FrameBufferLoan outer(renderer), inner(renderer);
    require(outer.active() && !inner.active(), "replacement loan is single owner");
    inner.end();
    reject(source, Result::Unavailable, "loan rejects replacement");
    require(!renderer.hasFrameBuffer(), "replacement does not return outer loan");
    outer.end();
  }
  require(renderer.liveFrameNeedsRedraw() && renderer.restoreRegion(current) == Result::InvalidLive,
          "region cannot repair loan invalidity");
  renderer.beginDisplayWork();
  require(renderer.replaceFrame(source) == Result::Ok && renderer.liveFrameValid(),
          "replacement repairs loan invalidity despite saved epoch");
  expectPublished();

  seedOld();
  std::memset(live.l().data, 0x11, 48000);
  std::memset(live.m().data, 0x55, 48000);
  restoreAbortL = live.l().data; restoreAbortM = live.m().data; restoreAbortBytes = 48000;
  captureAbortDestination = live.b().data;
  captureAbortHits = 0; restoreAbortSawMixed = false; captureAbortRenderer = &renderer;
  const int beforeAbort = halActivity();
  require(renderer.replaceFrame(source) == Result::PublishedCancelled, "late replacement reports PublishedCancelled");
  require(captureAbortHits == 1 && restoreAbortSawMixed,
          "replacement abort sees full new B and all old selectors exactly once");
  expectPublished();
  require(renderer.liveFrameNeedsRedraw(), "late replacement invalidates live");
  renderer.displayBuffer();
  require(halActivity() == beforeAbort, "late replacement and display make no HAL call");
  restoreAbortL = restoreAbortM = nullptr; restoreAbortBytes = 1;
  renderer.beginDisplayWork();
  require(renderer.replaceFrame(source) == Result::Ok, "fresh replacement recovers late cancellation");
  const int beforeDisplay = halActivity();
  renderer.abortDisplayWork(); renderer.displayBuffer();
  require(halActivity() == beforeDisplay && renderer.liveFrameNeedsRedraw() && !renderer.displayCommitted(),
          "abort after replacement Ok suppresses display");
  expectPublished();
  expect(storage.data(), original.data(), storage.size(), "cancelled replacement preserves source");

  renderer.beginDisplayWork();
  require(renderer.setUiGrayEnabled(false), "replacement disables retained policy");
  renderer.clearScreen();
  reject(source, Result::PolicyMismatch, "replacement tuple under BW policy rejected");
  Snapshot legacy = source; legacy.kind = GfxRenderer::SnapshotKind::LegacyBw;
  legacy.planes = GrayFrame(source.planes.b(), {}, {}, 800, 480, 100);
  seedOld();
  const int beforeLegacy = halActivity();
  require(renderer.replaceFrame(legacy) == Result::Ok && renderer.liveFrameValid(),
          "replacement accepts declared BW without selectors");
  expect(live.b().data, original.data() + 1, 48000, "legacy replacement complete B");
  for (size_t i = 0; i < 48000; ++i)
    require(live.l().data[i] == 0x44 && live.m().data[i] == 0x66, "legacy replacement preserves unused selectors");
  require(halActivity() == beforeLegacy, "legacy replacement makes no HAL call");
}

static void testReaderScratchOwnership(HalDisplay& hal) {
  using Owner = GfxRenderer::FrameOwner;
  GfxRenderer renderer(hal); renderer.begin(); renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "scratch enables retention");
  renderer.clearScreen();
  require(!renderer.storeReaderBwScratch() && !renderer.restoreReaderBwScratch(), "live UI refuses owned scratch methods");
  std::array<uint8_t, 48000> saved;
  for (size_t i = 0; i < saved.size(); ++i) saved[i] = static_cast<uint8_t>(0xCC + i * 13);
  physical = saved;
  const int beforeCleanup = cleanupCalls;
  {
    GfxRenderer::ScopedTarget owner(renderer, Owner::ReaderScratch);
    require(renderer.storeReaderBwScratch(), "owning scratch saves B");
    const auto chunks = scratchChunks(renderer);
    physical.fill(0x55);
    require(!renderer.storeReaderBwScratch() && scratchChunks(renderer) == chunks,
            "second save leaves original chunks owned");
    {
      GfxRenderer::ScopedTarget inner(renderer, Owner::ReaderScratch);
      require(!renderer.storeReaderBwScratch(), "nested scratch cannot replace saved B");
      require(!renderer.restoreReaderBwScratch(), "unmatched scratch restore rejected");
      require(physical[0] == 0x55 && scratchChunks(renderer) == chunks, "unmatched restore leaves outer storage intact");
    }
    {
      std::array<uint8_t, 100> band{};
      GfxRenderer::ScopedTarget offscreen(renderer, band.data(), 0, 1);
      require(!renderer.storeReaderBwScratch() && !renderer.restoreReaderBwScratch(), "offscreen cannot consume outer B");
    }
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_LSB);
    renderer.setRenderMode(GfxRenderer::GRAYSCALE_MSB);
    renderer.setRenderMode(GfxRenderer::BW);
    require(renderer.restoreReaderBwScratch(), "outer restore survives nested scopes and mode changes");
    expect(physical.data(), saved.data(), saved.size(), "owned scratch restores complete original B");
    require(!renderer.restoreReaderBwScratch(), "restored chunks cannot be consumed twice");
    require(renderer.liveFrameNeedsRedraw(), "scratch B restoration alone cannot certify tuple");
  }
  require(cleanupCalls == beforeCleanup, "CPU scratch restoration never calls cleanup");

  for (int boundary = 0; boundary < 4; ++boundary) {
    renderer.beginDisplayWork(); renderer.clearScreen(); physical = saved;
    std::array<uint8_t, 100> boundaryBand{};
    {
      GfxRenderer::ScopedTarget owner(renderer, Owner::ReaderScratch);
      require(renderer.storeReaderBwScratch(), "boundary fixture saves B");
      physical.fill(0x33);
      if (boundary == 0) {
        renderer.beginDisplayWork();
        require(!renderer.restoreReaderBwScratch(), "new work rejects stale scratch save");
        require(physical[0] == 0x33, "stale restore preserves new operation bytes");
        for (const auto* chunk : scratchChunks(renderer)) require(!chunk, "new work immediately discards saved chunks");
      }
      if (boundary == 2) renderer.beginStripTarget(boundaryBand.data(), 0, 1);
      if (boundary == 3) {
        renderer.setOrientation(GfxRenderer::Portrait);
        renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
        require(!renderer.restoreReaderBwScratch(), "orientation recovery rejects stale scratch save");
      }
    }
    for (const auto* chunk : scratchChunks(renderer)) require(!chunk, "owner exit and new work discard saved chunks");
    GfxRenderer::ScopedTarget other(renderer, Owner::ReaderScratch);
    require(!renderer.restoreReaderBwScratch(), "different owner cannot restore expired save");
    require(renderer.storeReaderBwScratch() && renderer.restoreReaderBwScratch(), "new owner can create its own save");
  }

  for (int failAt : {1, 3, 6}) {
    renderer.beginDisplayWork(); renderer.clearScreen(); physical = saved;
    GfxRenderer::ScopedTarget owner(renderer, Owner::ReaderScratch);
    failScratchAllocation = scratchAllocationCalls + failAt;
    require(!renderer.storeReaderBwScratch(), "scratch allocation failure rejects save");
    failScratchAllocation = 0;
    expect(physical.data(), saved.data(), saved.size(), "allocation failure preserves all B bytes");
    for (const auto* chunk : scratchChunks(renderer)) require(!chunk, "allocation failure frees every allocated chunk");
    physical.fill(0x33);
    const auto unchanged = physical;
    require(!renderer.restoreReaderBwScratch(), "failed allocation leaves no restorable save");
    expect(physical.data(), unchanged.data(), unchanged.size(), "failed save restore writes no B bytes");
    require(renderer.storeReaderBwScratch() && renderer.restoreReaderBwScratch(), "allocation failure permits later save");
  }
  renderer.beginDisplayWork(); renderer.clearScreen(); physical = saved;
  {
    GfxRenderer::ScopedTarget owner(renderer, Owner::ReaderScratch);
    require(renderer.storeReaderBwScratch(), "missing chunk fixture saves B");
    auto& chunks = scratchChunks(renderer);
    std::free(chunks.back()); chunks.back() = nullptr;
    physical.fill(0x33);
    const auto unchanged = physical;
    require(!renderer.restoreReaderBwScratch(), "missing final chunk rejects restore");
    expect(physical.data(), unchanged.data(), unchanged.size(), "missing final chunk rejects before any B write");
    for (const auto* chunk : chunks) require(!chunk, "missing chunk discards unusable save");
  }
  for (bool inputAbort : {false, true}) {
    renderer.beginDisplayWork(); renderer.clearScreen(); physical = saved;
    {
      GfxRenderer::ScopedTarget owner(renderer, Owner::ReaderScratch);
      require(renderer.storeReaderBwScratch(), "cancelled cleanup saves B");
      physical.fill(0x33);
      if (inputAbort) renderer.abortDisplayWork(); else owner.cancel();
      require(renderer.restoreReaderBwScratch(), "same work cancelled cleanup restores B");
      expect(physical.data(), saved.data(), saved.size(), "cancelled cleanup restores complete B");
      require(renderer.liveFrameNeedsRedraw() && !renderer.finishReaderImport(), "cancelled cleanup cannot certify tuple");
    }
  }
  require(cleanupCalls == beforeCleanup, "cancelled CPU restoration never calls cleanup");
}

static void testReaderScratchImport(HalDisplay& hal) {
  using Owner = GfxRenderer::FrameOwner;
  GfxRenderer renderer(hal); renderer.begin(); renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "scratch import enables retention");
  for (bool inputAbort : {false, true}) {
    renderer.beginDisplayWork(); renderer.clearScreen();
    require(renderer.drawCoverage(1, 2, 2), "scratch import seeds outside gray marker");
    const uint8_t* liveL = renderer.getLiveGrayPlane(true);
    const uint8_t* liveM = renderer.getLiveGrayPlane(false);
    const auto savedB = physical;
    std::array<uint8_t, 100> ls{}, ms{}; ls[0] = 0x10; ms[0] = 0x18;
    std::array<uint8_t, 48000> expectedL{}, expectedM{};
    expectedL[200] = 0x50; expectedM[200] = 0x58;
    const int callsBefore = cleanupCalls;
    const int commitsBefore = cleanupSubmissions;
    cleanupRenderer = &renderer;
    {
      GfxRenderer::ScopedTarget owner(renderer, Owner::ReaderScratch);
      require(renderer.storeReaderBwScratch() && renderer.beginReaderImport(2, 2, 4, 1), "save B before clipped scratch import");
      renderer.prepareGrayscaleTarget();
      renderer.clearScreen(0);
      require(renderer.importReaderPlane(true, ls.data(), ls.size(), 2, 1) &&
              renderer.importReaderPlane(false, ms.data(), ms.size(), 2, 1), "scratch import replaces both selectors");
      require(!renderer.finishReaderImport(), "scratch import lacks B coverage until restoration");
      if (inputAbort) renderer.abortDisplayWork();
      require(renderer.restoreReaderBwScratch(), "scratch import restores B coverage");
      require(cleanupCalls == callsBefore && cleanupSubmissions == commitsBefore && pendingCleanup,
              "scratch restore leaves pending cleanup untouched");
      require(renderer.liveFrameNeedsRedraw(), "scratch restore waits for explicit import finish");
      expect(physical.data(), savedB.data(), savedB.size(), "scratch import restores saved B exactly");
      expect(liveL, expectedL.data(), expectedL.size(), "scratch restore preserves L and gray marker");
      expect(liveM, expectedM.data(), expectedM.size(), "scratch restore preserves M and gray marker");
      require(renderer.finishReaderImport() == !inputAbort, "scratch B coverage completes only noncancelled import");
    }
    {
      GfxRenderer::ScopedTarget base(renderer, Owner::ReaderBase);
      renderer.cleanupGrayscaleWithFrameBuffer();
    }
    require(cleanupCalls == callsBefore + 1 && cleanupSubmissions == commitsBefore + !inputAbort,
            "explicit cleanup preserves pending and abort behavior");
    if (!inputAbort) require(cleanupSawComplete, "terminal cleanup observes complete import and ReaderBase ownership");
    expect(cleanupB.data(), savedB.data(), savedB.size(), "terminal cleanup receives restored B");
    cleanupRenderer = nullptr;
  }
}

static void testLegacySnapshots(HalDisplay& hal) {
  GfxRenderer renderer(hal); renderer.begin(); renderer.beginDisplayWork();
  renderer.setOrientation(GfxRenderer::LandscapeCounterClockwise);
  require(renderer.setUiGrayEnabled(true), "legacy guard enables retention");
  renderer.clearScreen();
  const uint8_t byte = 0xCC;
  const auto before = physical;
  renderer.writeFramebufferRegion(0, 0, 8, 1, &byte);
  expect(physical.data(), before.data(), before.size(), "retained rejects legacy unsized write");
  require(!renderer.copyBufferToRegion(0, 0, 8, 1, &byte, 1), "retained rejects legacy sized write");
  uint8_t read = 0xA5;
  require(renderer.readFramebufferRegion(0, 0, 8, 1, &read, 1) == 0 &&
          !renderer.copyRegionToBuffer(0, 0, 8, 1, &read, 1) && read == 0xA5, "retained rejects legacy region capture");
  require(!renderer.storeBwBuffer(), "retained rejects legacy BW save");
  renderer.restoreBwBuffer();
  expect(physical.data(), before.data(), before.size(), "retained rejects legacy BW restore");
  require(renderer.setUiGrayEnabled(false), "legacy control disables retention");
  for (bool inputAbort : {false, true}) {
    renderer.beginDisplayWork(); renderer.clearScreen();
    renderer.writeFramebufferRegion(0, 0, 8, 1, &byte);
    require(physical[0] == 0xCC && renderer.readFramebufferRegion(0, 0, 8, 1, &read, 1) == 1 && read == 0xCC,
            "disabled legacy unsized region roundtrip preserved");
    const uint8_t other = 0x33;
    require(renderer.copyBufferToRegion(8, 0, 8, 1, &other, 1) &&
            renderer.copyRegionToBuffer(8, 0, 8, 1, &read, 1) && read == 0x33,
            "disabled legacy sized region roundtrip preserved");
    const auto saved = physical;
    require(renderer.storeBwBuffer(), "disabled legacy saves B");
    renderer.prepareGrayscaleTarget(); physical.fill(0x55);
    if (inputAbort) renderer.abortDisplayWork();
    const int callsBefore = cleanupCalls, commitsBefore = cleanupSubmissions;
    renderer.restoreBwBuffer();
    expect(physical.data(), saved.data(), saved.size(), "disabled legacy restores B including abort");
    expect(cleanupB.data(), saved.data(), saved.size(), "legacy terminal cleanup receives restored B");
    require(cleanupCalls == callsBefore + 1 && cleanupSubmissions == commitsBefore + !inputAbort,
            "legacy cleanup still reached on normal and aborted restore");
  }
  renderer.beginDisplayWork(); renderer.clearScreen();
  {
    GfxRenderer::ScopedTarget owner(renderer, GfxRenderer::FrameOwner::ReaderScratch);
    require(renderer.storeReaderBwScratch(), "disabled owned scratch save accepted");
    physical.fill(0x33); renderer.restoreBwBuffer();
    require(physical[0] == 0x33 && renderer.restoreReaderBwScratch() && physical[0] == 0xFF,
            "disabled legacy wrapper cannot consume owned scratch save");
  }
}

int main() {
  HalDisplay hal;
  {
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
  {
    GfxRenderer::FrameBufferLoan loan(renderer);
    renderer.displayBuffer();
    require(submissions == 0, "loan refuses HAL submission");
  }
  testOwnership(hal, renderer);
  testUiSubmission(renderer);
  testReaderImport(renderer);
  testGlyphCoverage(hal);
  testOpaqueWriters(hal);
  testSnapshots(hal);
  testRegionSnapshots(hal);
  testFrameReplacement(hal);
  testLegacySnapshots(hal);
  testReaderScratchOwnership(hal);
  testReaderScratchImport(hal);
  std::puts("PASS: actual renderer ownership, submission, imports, glyphs, opaque writers, image clipping and stale target controls");
  }
  require(liveAllocations == 0, "renderer destruction releases pair");
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
    "lib/GfxRenderer/Bitmap.cpp",
    "lib/GfxRenderer/BitmapHelpers.cpp",
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
            common = ["-O1", "-g", "-fno-builtin-memcpy", "-ffunction-sections", "-fdata-sections", *includes]
            objects = []
            sources = [RENDERER, *(ROOT / path for path in CPP_SOURCES), harness]
            sources += sorted((ROOT / "lib/uzlib/src").glob("*.c"))
            sources.append(ROOT / "lib/MiniBidi/minibidi.c")
            for index, source in enumerate(sources):
                obj = work / f"source-{index}.o"
                compiler = cc if source.suffix == ".c" else cxx
                standard = "-std=c11" if source.suffix == ".c" else "-std=c++17"
                allocator = ["-Dmalloc=gfx_malloc"] if source == RENDERER else []
                subprocess.run([*compiler, standard, *common, *allocator, "-c", str(source), "-o", str(obj)], check=True)
                objects.append(str(obj))
            binary = work / "gfx-gray"
            dead_strip = "-Wl,-dead_strip" if sys.platform == "darwin" else "-Wl,--gc-sections"
            subprocess.run([*cxx, dead_strip, *objects, "-o", str(binary)], check=True)

            if os.environ.get("GFX_GRAY_BASELINE_ONLY"):
                subprocess.run([str(binary)], check=True)
                return

            source = RENDERER.read_text()
            mutated = work / "GfxRenderer-mutant.cpp"
            mutant_object = work / "renderer-mutant.o"
            mutant_binary = work / "gfx-gray-mutant"
            mutants = [
                ("strip clear", "    return;\n  }\n  if (readerImportCurrent()) {",
                 "  }\n  if (readerImportCurrent()) {", "strip preserves HAL: byte 0 expected FF, actual 00"),
                ("scope restoration", "  renderer_.target_ = previous_;", "  renderer_.target_ = {};",
                 "nested target restores pointer and physical band: expected true, actual false"),
                ("OR import", "    GrayFrame::copyPlaneRow(destination + y * panelWidthBytes, source + (y - yStart) * panelWidthBytes,\n                            readerImport_.x0, readerImport_.x1);",
                 "    for (int x = readerImport_.x0 / 8; x < (readerImport_.x1 + 7) / 8; ++x) destination[y * panelWidthBytes + x] |= source[(y - yStart) * panelWidthBytes + x];",
                 "import forwards whole L row with UI marker: expected true, actual false"),
                ("erase outside marker", "                            readerImport_.x0, readerImport_.x1);",
                 "                            0, panelWidth);", "import forwards whole L row with UI marker: expected true, actual false"),
                ("accept incomplete", "    if (readerImport_.coverage[y] != 7) return false;", "    if (false) return false;",
                 "incomplete selector coverage rejects completion: expected true, actual false"),
                ("accept cancelled", "bool GfxRenderer::finishReaderImport() const {\n  if (!readerImportCurrent()) return false;",
                 "bool GfxRenderer::finishReaderImport() const {\n  if (target_.owner != FrameOwner::ReaderScratch) return false;", "cancelled reader import rejected: expected true, actual false"),
                ("omit gray submission", "  if (!accountCancellation()) display.displayGrayBuffer(fadingFix);\n  return true;",
                 "  return true;", "UI marker submits exactly one gray frame: expected true, actual false"),
                ("skip glyph collector", "  renderer.drawCoverage(x, y, coverage, blackInk);", "",
                 "glyph packed B: byte 0 expected CF, actual FF"),
                ("glyph scan writes", "  if (renderer.isFontCacheScanning()) return true;", "",
                 "glyph scan preserves B: byte 1702 expected FF, actual F7"),
            ]
            mutants += [
                ("opaque masked fill", "tuple.clearSelectors(phyX0, phyX1 + 1, py);", "(void)py;",
                 "opaque oriented fill L: byte 200 expected 80, actual FF"),
                ("offscreen image bypass", "  drawPhysicalImage(bitmap, rotatedX, rotatedY, width, height);",
                 "  display.drawImage(bitmap, rotatedX, rotatedY, width, height);",
                 "offscreen image B: byte 401 expected A5, actual 00"),
            ]
            mutants += [
                ("misreport legacy snapshot", "out.kind = uiGrayEnabled_ ? SnapshotKind::RetainedTuple : SnapshotKind::LegacyBw;",
                 "out.kind = SnapshotKind::RetainedTuple;", "legacy capture truthfully reports BW: expected true, actual false"),
                ("skip snapshot capacity", "if (storage.capacity < layout.totalBytes) return FrameResult::ShortCapacity;",
                 "if (false) return FrameResult::ShortCapacity;", "capture short capacity rejected: expected true, actual false"),
                ("accept snapshot offset alias", "if (addressRangesOverlap(storage.data, layout.totalBytes, sources[plane], frameBufferSize))",
                 "if (storage.data == sources[plane])", "capture offset alias rejected: expected true, actual false"),
                ("unsafe snapshot geometry",
                 "if (x0 >= x1 || y0 >= y1) return {}; SnapshotLayout layout; layout.rect = screenRectToAlignedMemRect(orientation, x0, y0, x1 - x0, y1 - y0, panelWidth, panelHeight);",
                 "SnapshotLayout layout; layout.rect = screenRectToAlignedMemRect(orientation, x, y, width, height, panelWidth, panelHeight);",
                 "wide endpoint clipping remains visible: expected true, actual false"),
                ("publish cancelled capture", "if (accountCancellation()) return FrameResult::Cancelled; const GrayFrame::Plane b",
                 "const GrayFrame::Plane b", "late capture abort invalidates descriptor: expected true, actual false"),
            ]
            mutants += [
                ("restore only B", "const size_t planeCount = uiGrayEnabled_ ? 3 : 1; for (size_t plane = 0; plane < planeCount; ++plane)",
                 "const size_t planeCount = 1; for (size_t plane = 0; plane < planeCount; ++plane)",
                 "restore literal L padding and neighbor: byte 200 expected 22, actual 00"),
                ("skip restore capacity", "if (sources[plane].capacity < sourceBytes) return Result::ShortCapacity;",
                 "if (false) return Result::ShortCapacity;", "restore short source rejected: expected true, actual false"),
                ("ignore restore epoch", "if (!liveCoherent_ || source.restoreEpoch != restoreEpoch_) return FrameResult::InvalidLive;",
                 "if (!liveCoherent_) return FrameResult::InvalidLive;", "restore stale orientation epoch rejected: expected true, actual false"),
                ("inactive inner loan return", "void GfxRenderer::FrameBufferLoan::end() { if (!active_) return;",
                 "void GfxRenderer::FrameBufferLoan::end() {", "nested loan cannot restore outer storage: expected true, actual false"),
                ("accept restore offset alias", "if (addressRangesOverlap(sources[other].data, sourceBytes, destinations[plane], destinationBytes))",
                 "if (sources[other].data == destinations[plane])", "restore offset alias rejected: expected true, actual false"),
            ]
            mutants += [
                ("publish invalid slot",
                 "if (display.postRefreshAborted()) return FrameResult::Cancelled; const auto validation = validateSnapshot(source);",
                 "if (display.postRefreshAborted()) return FrameResult::Cancelled; auto accepted = source; accepted.valid = true; const auto validation = validateSnapshot(accepted);",
                 "replacement invalid slot rejected: expected true, actual false"),
                ("publish only B", "for (size_t plane = 0; plane < (uiGrayEnabled_ ? 3u : 1u); ++plane)",
                 "for (size_t plane = 0; plane < 1; ++plane)",
                 "replacement complete L: byte 0 expected 22, actual 44"),
                ("abort publication between planes", "memcpy(destinations[plane], sources[plane], bytes);",
                 "memcpy(destinations[plane], sources[plane], bytes); if (accountCancellation()) return FrameResult::PublishedCancelled;",
                 "replacement complete L: byte 0 expected 22, actual 11"),
                ("unmatched scratch owner", "bwScratchOwner_ != target_.identity || bwScratchWork_ != workGeneration_",
                 "bwScratchWork_ != workGeneration_", "unmatched scratch restore rejected: expected true, actual false"),
                ("scratch selector loss", "if (uiGrayEnabled_) liveCoherent_ = false; if (readerImportCurrent()) {",
                 "if (uiGrayEnabled_) { liveCoherent_ = false; memset(liveL_, 0, frameBufferSize); } if (readerImportCurrent()) {",
                 "scratch restore preserves L and gray marker: byte 200 expected 50, actual 00"),
                ("scratch premature validity", "if (uiGrayEnabled_) liveCoherent_ = false; if (readerImportCurrent()) {",
                 "if (uiGrayEnabled_) liveCoherent_ = true; if (readerImportCurrent()) {",
                 "scratch B restoration alone cannot certify tuple: expected true, actual false"),
                ("scratch premature cleanup", "if (!restoreBwBufferChunks()) return false; if (uiGrayEnabled_)",
                 "if (!restoreBwBufferChunks()) return false; cleanupGrayscaleWithFrameBuffer(); if (uiGrayEnabled_)",
                 "CPU scratch restoration never calls cleanup: expected true, actual false"),
                ("scratch write before validation", "if (!bwBufferStored_) return false; if (bwBufferChunks.size()",
                 "if (!bwBufferStored_) return false; memcpy(frameBuffer, bwBufferChunks.front(), BW_BUFFER_CHUNK_SIZE); if (bwBufferChunks.size()",
                 "missing final chunk rejects before any B write: byte 0 expected 33, actual CC"),
            ]
            for name, original, replacement, expected_error in mutants:
                pattern = r"\s+".join(re.escape(part) for part in original.split())
                matches = list(re.finditer(pattern, source))
                self.assertEqual(len(matches), 1, f"{name} mutant anchor changed")
                match = matches[0]
                mutated.write_text(source[:match.start()] + replacement + source[match.end():])
                subprocess.run([*cxx, "-std=c++17", *common, "-Dmalloc=gfx_malloc", "-c", str(mutated), "-o", str(mutant_object)], check=True)
                subprocess.run([*cxx, dead_strip, str(mutant_object), *objects[1:], "-o", str(mutant_binary)], check=True)
                result = subprocess.run([str(mutant_binary)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, f"{name} mutant survived")
                self.assertIn(expected_error, result.stderr)
                print("EXPECTED MUTANT FAILURE:", result.stderr.strip())
            direct_source = (ROOT / "lib/Epub/Epub/converters/DirectPixelWriter.h").read_text()
            original = "    if (!renderer->isTargetCurrent(generation)) return;"
            self.assertEqual(direct_source.count(original), 1, "stale direct writer mutant anchor changed")
            direct_mutants = [
                (direct_source.replace(original, ""),
                 "stale direct writer preserves live: byte 0 expected FF, actual 7F"),
                (direct_source.replace(original, "").replace("  inline void beginRow(int logicalY) {",
                 "  inline void beginRow(int logicalY) {\n    if (!renderer->isTargetCurrent(generation)) { logicalRowVisible = false; return; }"),
                 "stale direct writer within row rejects replacement: expected true, actual false"),
            ]
            for mutated_header, expected_error in direct_mutants:
                (work / "DirectPixelWriter.h").write_text(mutated_header)
                stale_object = work / "stale-direct.o"
                subprocess.run([*cxx, "-std=c++17", *common, "-c", str(harness), "-o", str(stale_object)], check=True)
                stale_objects = objects.copy()
                stale_objects[1 + len(CPP_SOURCES)] = str(stale_object)
                subprocess.run([*cxx, dead_strip, *stale_objects, "-o", str(mutant_binary)], check=True)
                result = subprocess.run([str(mutant_binary)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, "stale direct writer mutant survived")
                self.assertIn(expected_error, result.stderr)
                print("EXPECTED MUTANT FAILURE:", result.stderr.strip())
            self.assertEqual(RENDERER.read_text(), source, "production source changed during test")
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
