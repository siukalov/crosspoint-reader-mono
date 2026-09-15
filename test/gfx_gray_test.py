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
#include <FontCacheManager.h>
#include <FontDecompressor.h>
#include <esp_heap_caps.h>

static std::array<uint8_t, 48000> physical;
static bool lent = false;
static bool aborted = false;
static int submissions = 0;
static int graySubmissions = 0;
static int baseSubmissions = 0;
static int asyncSubmissions = 0;
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
void HalDisplay::displayBuffer(RefreshMode, bool) { ++submissions; }
void HalDisplay::displayBufferAsync(RefreshMode) { ++submissions; ++asyncSubmissions; }
void HalDisplay::displayGrayscaleBase(RefreshMode fallback, bool fading) {
  ++baseSubmissions; stagedB = physical; lastFallback = fallback; lastFadingFix = fading;
}
void HalDisplay::copyGrayscaleLsbBuffers(const uint8_t* source) { std::memcpy(stagedL.data(), source, 48000); }
void HalDisplay::copyGrayscaleMsbBuffers(const uint8_t* source) { std::memcpy(stagedM.data(), source, 48000); }
void HalDisplay::writeGrayscalePlaneStrip(bool lsb, const uint8_t* source, uint16_t y, uint16_t rows) {
  std::memcpy((lsb ? stagedL : stagedM).data() + y * 100, source, rows * 100);
}
void HalDisplay::displayGrayBuffer(bool fading) { ++graySubmissions; lastFadingFix = fading; }
bool HalDisplay::supportsAsyncRefresh() const { return true; }
bool HalDisplay::refreshBusy() { return false; }
void HalDisplay::waitRefreshComplete() {}
void HalDisplay::beginDisplayWork() { aborted = false; }
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
  require(renderer.liveFrameValid() && renderer.readFramebufferRegion(0, 0, 8, 1, &rebuilt, 1) == 1 && rebuilt == 0xFF,
          "secondary alias full rebuild restores usable live frame");
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
  std::puts("PASS: actual renderer ownership, submission, imports, glyph coverage, font decoding, rotation and scan controls");
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
            for name, original, replacement, expected_error in mutants:
                pattern = r"\s+".join(re.escape(part) for part in original.split())
                matches = list(re.finditer(pattern, source))
                self.assertEqual(len(matches), 1, f"{name} mutant anchor changed")
                match = matches[0]
                mutated.write_text(source[:match.start()] + replacement + source[match.end():])
                subprocess.run([*cxx, "-std=c++17", *common, "-c", str(mutated), "-o", str(mutant_object)], check=True)
                subprocess.run([*cxx, dead_strip, str(mutant_object), *objects[1:], "-o", str(mutant_binary)], check=True)
                result = subprocess.run([str(mutant_binary)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, f"{name} mutant survived")
                self.assertIn(expected_error, result.stderr)
                print("EXPECTED MUTANT FAILURE:", result.stderr.strip())
            self.assertEqual(RENDERER.read_text(), source, "production source changed during test")
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
