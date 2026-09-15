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
void hostDelay(unsigned long ms);
int hostRead();
inline void delay(unsigned long ms) { hostDelay(ms); }
inline int digitalRead(int) { return hostRead(); }
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
  enum RefreshMode { FAST_REFRESH };
  uint8_t frame[48000];
  uint16_t getDisplayWidth() const { return 800; }
  uint16_t getDisplayHeight() const { return 480; }
  uint8_t* getFrameBuffer() { return frame; }
  void waitRefreshComplete() {}
  void beginDisplayWork() { freeink::ssd1683Driver().beginDisplayWork(); }
  void displayBuffer(RefreshMode) {
    freeink::ssd1683Driver().display(hostBus, frame, nullptr, freeink::RefreshMode::Fast, false);
  }
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
struct Event { char kind; unsigned value; unsigned long time; std::vector<uint8_t> bytes; };
struct Activation { uint8_t control; std::vector<uint8_t> p24, p26; unsigned long time; };
std::vector<Event> events;
std::vector<Activation> activations;
struct Completion { uint8_t control; uint32_t busyMs; bool completed; };
std::vector<Completion> completions;
extern "C" __attribute__((weak)) void freeink_panel_capture_completion(uint8_t control,uint32_t,uint32_t busyMs,bool completed) {
  completions.push_back({control,busyMs,completed});
}
void require(bool condition, const char* message);
unsigned long busyStart = 0, busyEnd = 0;
unsigned waitCount = 0, failWait = 0, cancelWait = 0;
bool cancelTransfer = false;
void hostDelay(unsigned long ms) { events.push_back({'d', static_cast<unsigned>(ms), hostMillis, {}}); hostMillis += ms; }
int hostRead() { return hostMillis >= busyStart && hostMillis < busyEnd ? HIGH : LOW; }
void resetRecording() {
  events.clear(); activations.clear(); completions.clear(); controls.clear(); ram24.clear(); ram26.clear(); chunks = 0;
  busyStart = busyEnd = 0; waitCount = failWait = cancelWait = 0; cancelTransfer = false;
}
void waitForPin(bool refresh) {
  events.push_back({'w', refresh ? 1u : 0u, hostMillis, {}});
  if (refresh && (controls.back() == 0xF8 || controls.back() == 0x14))
    require(!freeink::ssd1683Driver().displayCommitted(), "recovery must not publish commit between phases");
  ++waitCount;
  if (waitCount == cancelWait) freeink::ssd1683Driver().abortPostRefresh();
  if (waitCount == failWait) { busyStart = hostMillis; busyEnd = hostMillis + 10000; }
  const auto deadline = hostMillis + 500;
  while (hostRead() == HIGH && hostMillis < deadline) ++hostMillis;
}


void require(bool condition, const char* message) {
  if (!condition) { std::fprintf(stderr, "FAIL %s\n", message); std::exit(1); }
}

namespace freeink {
void EpdBus::reset(uint16_t) { events.push_back({'r', 0, hostMillis, {}}); }
void EpdBus::cmd(uint8_t command) {
  events.push_back({'c', command, hostMillis, {}});
  currentCommand = command;
  if (command == 0x20) {
    activations.push_back({controls.back(), ram24, ram26, hostMillis});
    busyStart = hostMillis + 1; busyEnd = hostMillis + 138;
  }
  if (command == 0x24) ram24.clear();
  if (command == 0x26) ram26.clear();
}
void EpdBus::data(uint8_t value) {
  events.back().bytes.push_back(value);
  if (currentCommand == 0x22) controls.push_back(value);
}
void EpdBus::data(const uint8_t* bytes, uint16_t count) {
  if (currentCommand != 0x24 && currentCommand != 0x26) {
    events.back().bytes.insert(events.back().bytes.end(), bytes, bytes + count); return;
  }
  if (cancelTransfer) { freeink::ssd1683Driver().abortPostRefresh(); cancelTransfer = false; }
  auto& ram = currentCommand == 0x24 ? ram24 : ram26;
  ram.insert(ram.end(), bytes, bytes + count);
  ++chunks;
}
void EpdBus::cmdData(uint8_t command, const uint8_t* bytes, uint16_t count) { cmd(command); data(bytes, count); }
void EpdBus::waitBusy(const char*) { waitForPin(false); }
void EpdBus::waitRefreshComplete(const char*) {
  if (controls.back() != 0xD7 && controls.back() != 0xF8 && controls.back() != 0x14) ++hostMillis;
  waitForPin(true);
}
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
    resetRecording();
    const unsigned allocationCalls = hostAllocationCalls;
    require(PanelDiagnostic::renderFixture(), "diagnostic capture valid: expected true, actual false");
    require(hostAllocationCalls - allocationCalls == (rotated ? 1u : 0u),
            "capture storage: expected one initial allocation and no repeat allocations");
    const auto meta = PanelDiagnostic::metadata();
    require(meta.valid && meta.width == 800 && meta.height == 480 && meta.planeBytes == 48000, "capture dimensions");
    require(meta.generation == (rotated ? 1u : 2u) && meta.driverGeneration != 0, "capture generations");
    require(meta.activationCount == 1 && meta.busyMs == 137 && meta.renderMs == 250, "activation and render milliseconds");
    require(controls.size() == 1 && controls[0] == 0xD7 && meta.control == controls[0],
            "manufacturer four-gray update control");
    require(chunks == (rotated ? 6u : 2u) && !inTransaction, "original plane chunk/transaction boundaries");
    require(ram24.size() == 48000 && ram26.size() == 48000, "complete bus planes");
    const bool encoded24[] = {false, true, false, true};
    const bool encoded26[] = {false, false, true, true};
    for (unsigned i = 0; i < 4; ++i) {
      checkPixel(ram24, 64 + i * 176, 100, encoded24[i], rotated);
      checkPixel(ram26, 64 + i * 176, 100, encoded26[i], rotated);
    }
    for (unsigned y = 0; y < 480; ++y) {
      for (unsigned x = 0; x < 800; ++x) {
        const bool black = !bit(bw.data(), x, y) && !bit(msb.data(), x, y);
        checkPixel(ram24, x, y, black || (bit(msb.data(), x, y) && !bit(lsb.data(), x, y)), rotated);
        checkPixel(ram26, x, y, black || bit(lsb.data(), x, y), rotated);
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


STATE_CASES = r"""
extern "C" bool freeink_panel_capture_enabled() { return true; }
extern "C" void freeink_panel_capture_plane(uint8_t,uint32_t,uint16_t,uint16_t,uint32_t,const uint8_t*,uint16_t) {}
extern "C" void freeink_panel_capture_activation(uint8_t,uint32_t,uint32_t) {}

void stage(freeink::Ssd1683Driver& d, const uint8_t* b, const uint8_t* l, const uint8_t* m) {
  d.displayGrayscaleBase(hostBus, b, freeink::RefreshMode::Fast, false);
  d.copyGrayscaleLsb(hostBus, l); d.copyGrayscaleMsb(hostBus, m);
}
void expectControls(std::initializer_list<uint8_t> expected, const char* message) {
  std::vector<uint8_t> actual;
  for (const auto& activation : activations) actual.push_back(activation.control);
  require(actual == std::vector<uint8_t>(expected), message);
}
void expectPlane(const std::vector<uint8_t>& p, uint8_t byte, const char* message) {
  if (!p.empty() && p.front() != byte) std::fprintf(stderr,"expected byte %02X, actual %02X\n",byte,p.front());
  require(p.size() == 48000 && std::all_of(p.begin(), p.end(), [byte](uint8_t b) { return b == byte; }), message);
}
void checkInit(bool gray) {
  std::vector<std::pair<unsigned, std::vector<uint8_t>>> expected;
  if (!gray) expected.push_back({0x18,{0x80}});
  expected.push_back({0x0C,{0xAE,0xC7,0xC3,0xC0,0x80}});
  expected.push_back({0x01,{0xDF,0x01,0x02}});
  if (!gray) { expected.push_back({0x3C,{0x01}}); expected.push_back({0x21,{0x00}}); }
  expected.insert(expected.end(), {{0x11,{0x02}},{0x44,{0x1F,0x03,0,0}},{0x45,{0,0,0xDF,0x01}},
                                  {0x4E,{0x1F,0x03}},{0x4F,{0,0}}});
  if (gray) expected.insert(expected.end(), {{0x3C,{0x01}},{0x18,{0x80}},{0x1A,{0x5A}}});
  require(events.size() > 7 && events[0].kind == 'r' && events[1].kind == 'd' && events[1].value >= 1 &&
          events[2].kind == 'w' && events[3].kind == 'c' && events[3].value == 0x12 &&
          events[4].kind == 'd' && events[4].value == 10 && events[5].kind == 'd' && events[5].value >= 1 &&
          events[6].kind == 'w', "vendor reset, settle, software-reset delay and checked waits in literal order");
  for (unsigned i = 0; i < expected.size(); ++i)
    require(events[7+i].kind == 'c' && events[7+i].value == expected[i].first && events[7+i].bytes == expected[i].second,
            "vendor initialization register payload/order");
  std::vector<unsigned> commands;
  for (const auto& e : events) if (e.kind == 'c') commands.push_back(e.value);
  std::vector<unsigned> expectedCommands{0x12};
  for (const auto& e : expected) expectedCommands.push_back(e.first);
  if (gray) expectedCommands.insert(expectedCommands.end(), {0x4E,0x4F,0x24,0x4E,0x4F,0x26,0x22,0x20,0x10});
  else expectedCommands.insert(expectedCommands.end(), {0x22,0x4E,0x4F,0x24,0x20,0x22,0x4E,0x4F,0x26,0x4E,0x4F,0x24,0x20,0x10});
  require(commands == expectedCommands, "complete ordered vendor register and RAM command sequence");
  for (const auto& e : events) require(e.kind != 'c' || (e.value != 0x32 && e.value != 0x03 && e.value != 0x04 && e.value != 0x2C),
                                     "vendor path must not write custom LUT or voltages");
}
void checkSleep() {
  require(events.size() >= 2 && events[events.size()-2].kind == 'c' && events[events.size()-2].value == 0x10 &&
          events[events.size()-2].bytes == std::vector<uint8_t>{1} && events.back().kind == 'd' && events.back().value == 100,
          "vendor deep sleep key 01 and 100 ms delay");
  for (unsigned i = 0; i < events.size(); ++i) if (events[i].kind == 'c' && events[i].value == 0x20) {
    require(events[i+1].kind == 'd' && events[i+1].value >= 1 && events[i+2].kind == 'w', "activation settles before BUSY wait");
    require(events[i+3].time >= events[i].time + 138, "no command before scheduled BUSY completion edge");
  }
}
int main(int argc, char** argv) {
  require(argc == 2, "missing state scenario");
  const std::string scenario = argv[1];
  auto& d = freeink::ssd1683Driver();
  std::array<uint8_t,48000> b,l,m,other;
  b.fill(0xCC); l.fill(0x22); m.fill(0x66); other.fill(0xF0);
  d.begin(hostBus); resetRecording(); d.beginDisplayWork();
  if (scenario == "committed_cancel" || scenario == "replace_fallback") {
    d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
    d.copyGrayscaleLsb(hostBus,l.data()); d.copyGrayscaleMsb(hostBus,m.data());
    resetRecording();
    if (scenario == "committed_cancel") d.abortPostRefresh();
    else d.display(hostBus,other.data(),nullptr,freeink::RefreshMode::Fast,false);
    d.displayGray(hostBus,m.data(),false,nullptr,false);
    expectControls(scenario == "committed_cancel" ? std::initializer_list<uint8_t>{} : std::initializer_list<uint8_t>{0x0C},
                   "cancelled or replaced fallback cannot activate gray");
    require(d.displayCommitted(), "later skipped gray cannot erase earlier BW commit"); return 0;
  }
  if (scenario == "ordinary") {
    d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
    expectControls({0xFC}, "fresh BW uses existing cold FC");
    expectPlane(ram24,0x33,"ordinary target transform"); expectPlane(ram26,0xCC,"ordinary initial inverted OLD");
    d.beginDisplayWork(); resetRecording(); d.display(hostBus,other.data(),nullptr,freeink::RefreshMode::Fast,false);
    expectControls({0x0C}, "ordinary warm BW remains 0C"); expectPlane(ram26,0x33,"ordinary OLD is prior target"); return 0;
  }
  if (scenario == "stale" || scenario == "fallback" || scenario == "identical_fallback" || scenario == "seed") {
    d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
    if (scenario != "fallback") d.beginDisplayWork();
    if (scenario == "identical_fallback") {
      d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
      require(!d.displayCommitted(),"identical submission binds fallback without display commit");
    }
    if (scenario == "seed") {
      d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
      d.seedPreviousFrame(hostBus,other.data());
    }
    resetRecording(); d.copyGrayscaleLsb(hostBus,l.data()); d.copyGrayscaleMsb(hostBus,m.data());
    d.displayGray(hostBus,m.data(),false,nullptr,false);
    if (scenario == "stale" || scenario == "seed") {
      expectControls({},"previous-render or seeded target cannot bind fallback"); require(!d.displayCommitted(),"stale fallback not committed");
    } else {
      expectControls({0xD7},"same-render BW fallback activates absolute gray");
      expectPlane(ram24,0xAA,"fallback uses saved BW target, not selector bytes"); expectPlane(ram26,0xCC,"fallback gray RAM26 literal");
    }
    return 0;
  }
  if (scenario.rfind("replace_",0) == 0 || scenario == "restage") {
    stage(d,b.data(),l.data(),m.data());
    const uint8_t* target = scenario == "restage" ? b.data() : other.data();
    if (scenario == "replace_calibration") d.displayGrayCalibration(hostBus,target,0,0,800,480);
    else if (scenario == "replace_cleanup") d.cleanupGrayscaleBuffers(hostBus,target);
    else if (scenario == "replace_prepare") { d.prepareGrayscaleTarget(target); d.displayGray(hostBus,target,false,nullptr,false); }
    else if (scenario == "replace_display") d.display(hostBus,target,nullptr,freeink::RefreshMode::Fast,false);
    else { d.displayGrayscaleBase(hostBus,target,freeink::RefreshMode::Fast,false); d.displayGray(hostBus,target,false,nullptr,false); }
    expectControls(scenario == "restage" ? std::initializer_list<uint8_t>{0xD7} : std::initializer_list<uint8_t>{0xFC},
                   "changed owned target discards selectors; identical restaging preserves them"); return 0;
  }
  if (scenario == "partial" || scenario == "overlap" || scenario == "missing") {
    d.displayGrayscaleBase(hostBus,b.data(),freeink::RefreshMode::Fast,false);
    d.copyGrayscaleLsb(hostBus,l.data());
    if (scenario != "missing") d.writeGrayscalePlaneStrip(hostBus,freeink::GrayPlane::Msb,m.data(),0,479);
    if (scenario == "overlap") for (unsigned i=0;i<3;++i) d.writeGrayscalePlaneStrip(hostBus,freeink::GrayPlane::Msb,m.data(),0,479);
    d.displayGray(hostBus,b.data(),false,nullptr,false); expectControls({0xFC},"incomplete selector coverage must fall back to BW"); return 0;
  }
  if (scenario.rfind("cancel_",0) == 0 && scenario != "cancel_gray_wait" && scenario != "cancel_recovery_wait") {
    stage(d,b.data(),l.data(),m.data());
    if (scenario == "cancel_init") cancelWait = 1;
    else if (scenario == "cancel_transfer") cancelTransfer = true;
    else d.abortPostRefresh();
    if (scenario == "cancel_calibration") d.displayGrayCalibration(hostBus,b.data(),0,0,800,480);
    else if (scenario == "cancel_cleanup") d.cleanupGrayscaleBuffers(hostBus,b.data());
    else d.displayGray(hostBus,b.data(),false,nullptr,false);
    expectControls({},"cancelled work must not activate"); require(!d.displayCommitted(),"cancelled work cannot commit"); return 0;
  }
  if (scenario == "calibration") {
    for (unsigned i=0;i<2;++i) {
      if (i) { d.beginDisplayWork(); resetRecording(); b.fill(0xF0); }
      d.copyGrayscaleLsb(hostBus,l.data()); d.copyGrayscaleMsb(hostBus,m.data());
      d.displayGrayCalibration(hostBus,b.data(),0,0,800,480);
      expectControls({0xD7},"first selectors-before-target calibration in successive renders");
      expectPlane(ram24,i ? 0xB2 : 0xAA,"calibration preserves first target selectors");
    } return 0;
  }
  stage(d,b.data(),l.data(),m.data());
  if (scenario == "fail_hardware") failWait = 1;
  if (scenario == "fail_software") failWait = 2;
  if (scenario == "fail_gray") failWait = 3;
  if (scenario == "cancel_gray_wait") cancelWait = 3;
  d.displayGray(hostBus,b.data(),false,nullptr,false);
  if (scenario.rfind("fail_",0) == 0 && scenario != "fail_recovery1" && scenario != "fail_recovery2") {
    expectControls(scenario == "fail_gray" ? std::initializer_list<uint8_t>{0xD7} : std::initializer_list<uint8_t>{},
                   "failed vendor readiness stops activation");
    require(!d.displayCommitted(),"still-BUSY vendor operation cannot commit");
    if (scenario == "fail_gray") require(completions.size() == 1 && !completions[0].completed && completions[0].busyMs == 500,
                                         "checked capture reports BUSY timeout separately from settle");
    require(events.back().kind == 'w',"readiness failure stops all later commands including sleep");
    if (scenario == "fail_hardware") require(events.size() == 3,"hardware failure stops before software reset");
    if (scenario == "fail_software") require(events.size() == 7,"software failure stops before registers");
  } else {
    expectControls({0xD7},"absolute gray must activate D7 exactly once"); checkInit(true); checkSleep();
    require(completions.size() == 1 && completions[0].control == 0xD7 && completions[0].completed && completions[0].busyMs == 137,
            "checked completion callback matches gray activation and wait milliseconds");
    expectPlane(ram24,0xAA,"literal four-tone rotated RAM24 AA"); expectPlane(ram26,0xCC,"literal four-tone rotated RAM26 CC");
    require(d.displayCommitted(),"completed gray operation commits");
  }
  if (scenario == "gray") return 0;
  if (scenario == "reset") d.resetGray();
  if (scenario == "resync") d.requestResync(1);
  if (scenario == "cleanup") d.cleanupGrayscaleBuffers(hostBus,b.data());
  d.beginDisplayWork(); resetRecording();
  if (scenario == "staging") {
    stage(d,b.data(),l.data(),m.data()); require(events.empty(),"staging after vendor sleep must issue zero bus commands");
    d.displayGray(hostBus,b.data(),false,nullptr,false); checkInit(true); expectControls({0xD7},"one vendor init after staging"); return 0;
  }
  if (scenario == "cancel_recovery_wait") cancelWait = 3;
  if (scenario == "fail_recovery1") failWait = 3;
  if (scenario == "fail_recovery2") failWait = 4;
  d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
  if (scenario == "fail_recovery1" || scenario == "fail_recovery2") {
    expectControls(scenario == "fail_recovery1" ? std::initializer_list<uint8_t>{0xF8} : std::initializer_list<uint8_t>{0xF8,0x14},
                   "failed recovery stops before later phases");
    require(!d.displayCommitted() && events.back().kind == 'w',"failed recovery cannot commit or sleep");
    d.beginDisplayWork(); resetRecording(); d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
  }
  expectControls({0xF8,0x14},"first BW after gray requires F8 then 14 recovery"); checkInit(false); checkSleep();
  require(completions.size() == 2 && completions[0].completed && completions[1].completed,
          "both recovery activations report checked completion");
  require(activations[0].p26.empty(), "first recovery activation writes only RAM24");
  expectPlane(activations[0].p24,0xCC,"recovery first plane is inverted target");
  expectPlane(activations[1].p24,0x33,"recovery second RAM24 is BW target");
  expectPlane(activations[1].p26,0x33,"recovery second RAM26 is BW target");
  require(d.displayCommitted(),"BW recovery commits only after both phases");
  d.beginDisplayWork(); resetRecording(); d.display(hostBus,b.data(),nullptr,freeink::RefreshMode::Fast,false);
  expectControls({},"identical recovered BW does not activate"); require(!d.displayCommitted(),"no-op new render is not committed");
  d.beginDisplayWork(); resetRecording(); d.display(hostBus,other.data(),nullptr,freeink::RefreshMode::Fast,false);
  expectControls({0xFC},"BW after vendor sleep reloads cold FC OTP"); expectPlane(ram26,0x33,"changed BW OLD equals recovered target");
}
"""

SCENARIOS = ("ordinary", "gray", "recovery", "reset", "resync", "cleanup", "staging", "partial", "missing", "overlap",
             "replace_base", "replace_calibration", "replace_cleanup", "replace_display", "replace_prepare", "restage", "calibration",
             "stale", "fallback", "identical_fallback", "seed", "cancel_gray", "cancel_calibration", "cancel_cleanup", "cancel_init",
             "cancel_transfer", "cancel_gray_wait", "cancel_recovery_wait", "fail_hardware", "fail_software", "fail_gray",
             "fail_recovery1", "fail_recovery2", "committed_cancel", "replace_fallback")


class PanelDiagnosticTest(unittest.TestCase):
    def test_state_sequences(self):
        source = HARNESS[:HARNESS.index("int main(")] + STATE_CASES
        self.compile_and_run(source, SCENARIOS)

    def compile_and_run(self, source, scenarios, driver_source=None, failure=None):
        with tempfile.TemporaryDirectory(prefix="panel-state-") as temporary:
            directory = Path(temporary)
            for name, content in STUBS.items():
                (directory / name).write_text(content)
            harness = directory / "test.cpp"
            harness.write_text(source)
            executable = directory / "test"
            driver_file = DRIVER / "Ssd1683Driver.cpp"
            if driver_source is not None:
                driver_file = directory / "Ssd1683Driver.cpp"
                driver_file.write_text(driver_source)
            command = [os.environ.get("CXX", "c++"), "-std=c++17", "-DENABLE_SERIAL_LOG=1", "-I", str(directory),
                       "-I", str(DRIVER), "-I", str(ROOT / "lib/PanelDiagnostic"), str(harness),
                       str(driver_file), "-o", str(executable)]
            compiled = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for scenario in scenarios:
                with self.subTest(scenario=scenario):
                    result = subprocess.run([str(executable), scenario], capture_output=True, text=True)
                    if failure is None:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, "semantic mutant survived")
                        self.assertIn(failure, result.stderr)

    def test_semantic_mutants(self):
        source = (DRIVER / "Ssd1683Driver.cpp").read_text()
        mutants = (
            ("merge selectors", "black | _grayLsb[i]", "black | _grayMsb[i]", "gray", "literal four-tone rotated RAM26 CC"),
            ("swap planes", "writePlane(bus, CMD_WRITE_NEW, _sel24);\n  writePlane(bus, CMD_WRITE_OLD, _sel26);",
             "writePlane(bus, CMD_WRITE_NEW, _sel26);\n  writePlane(bus, CMD_WRITE_OLD, _sel24);", "gray", "literal four-tone rotated RAM24 AA"),
            ("skip recovery", "else if (_needsBwRecovery)", "else if (false && _needsBwRecovery)", "recovery", "first BW after gray requires F8 then 14 recovery"),
            ("clear cleanup recovery", "void Ssd1683Driver::cleanupGrayscaleBuffers(EpdBus& bus, const uint8_t* bw) {",
             "void Ssd1683Driver::cleanupGrayscaleBuffers(EpdBus& bus, const uint8_t* bw) { _needsBwRecovery = false;",
             "cleanup", "first BW after gray requires F8 then 14 recovery"),
            ("incomplete coverage", "return covered == GRAY_ROWS;", "return covered >= GRAY_ROWS - 1;", "partial", "incomplete selector coverage must fall back to BW"),
            ("ignore cancellation", "return _abortGeneration.load() != _displayWorkGeneration;", "return false;", "cancel_gray", "cancelled work must not activate"),
            ("accept busy", "const bool completed = !bus.isBusy();", "const bool completed = true;", "fail_gray", "still-BUSY vendor operation cannot commit"),
            ("remove settle", "  delay(1);", "  delay(0);", "gray", "vendor reset, settle, software-reset delay and checked waits in literal order"),
            ("eager initialization", "  stashTarget(fb, fallback);", "  if (!_initialized) { bus.reset(); initController(bus); }\n  stashTarget(fb, fallback);",
             "staging", "staging after vendor sleep must issue zero bus commands"),
        )
        harness = HARNESS[:HARNESS.index("int main(")] + STATE_CASES
        for name, old, new, scenario, failure in mutants:
            with self.subTest(mutant=name):
                self.assertEqual(source.count(old), 1, "mutant must have one exact production site")
                self.compile_and_run(harness, (scenario,), source.replace(old, new), failure)

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
