#include "PanelDiagnostic.h"

#include <HalDisplay.h>
#include <MinizConfig.h>
#include <esp_heap_caps.h>

#include <algorithm>
#include <cstring>

namespace {
using namespace PanelDiagnostic;

Metadata captured;
uint8_t* captureBytes = nullptr;
uint32_t capturedBytes[2] = {};
bool capturing = false;
bool captureError = false;
bool capturingGray = false;
bool bwTargetKnown = false;
bool previousBwChanged = false;
TransitionMetadata transition;
Activation pending;

struct Glyph {
  char character;
  uint8_t rows[7];
};

constexpr Glyph GLYPHS[] = {
    {'A', {14, 17, 17, 31, 17, 17, 17}}, {'B', {30, 17, 17, 30, 17, 17, 30}}, {'C', {14, 17, 16, 16, 16, 17, 14}},
    {'D', {30, 17, 17, 17, 17, 17, 30}}, {'E', {31, 16, 16, 30, 16, 16, 31}}, {'G', {14, 17, 16, 23, 17, 17, 15}},
    {'H', {17, 17, 17, 31, 17, 17, 17}}, {'I', {31, 4, 4, 4, 4, 4, 31}},      {'K', {17, 18, 20, 24, 20, 18, 17}},
    {'L', {16, 16, 16, 16, 16, 16, 31}}, {'R', {30, 17, 17, 30, 20, 18, 17}}, {'T', {31, 4, 4, 4, 4, 4, 4}},
    {'W', {17, 17, 17, 21, 21, 21, 10}},
};

void fillRectangle(uint8_t* plane, uint16_t x, uint16_t y, uint16_t width, uint16_t height, bool value) {
  if (!plane) return;
  for (uint16_t row = y; row < y + height; ++row) {
    for (uint16_t column = x; column < x + width; ++column) {
      uint8_t& byte = plane[row * (WIDTH / 8) + column / 8];
      const uint8_t bit = 0x80u >> (column % 8);
      byte = value ? static_cast<uint8_t>(byte | bit) : static_cast<uint8_t>(byte & ~bit);
    }
  }
}

void drawLabel(uint8_t* bw, uint16_t x, uint16_t y, const char* text) {
  for (; *text; ++text, x += 18) {
    for (const auto& glyph : GLYPHS) {
      if (glyph.character != *text) continue;
      for (uint8_t row = 0; row < 7; ++row) {
        for (uint8_t column = 0; column < 5; ++column) {
          if (glyph.rows[row] & (16u >> column)) fillRectangle(bw, x + column * 3, y + row * 3, 3, 3, false);
        }
      }
      break;
    }
  }
}

int planeIndex(uint8_t command) { return command == 0x24 ? 0 : command == 0x26 ? 1 : -1; }

void startCapture(bool gray, bool changed = false) {
  const uint32_t next = transition.generation + 1;
  transition = TransitionMetadata{};
  transition.generation = next == 0 ? 1 : next;
  transition.changed = changed;
  pending = Activation{};
  captureError = false;
  capturingGray = gray;
  capturing = true;
}

void finishCapture(uint32_t started, bool expectedNoop) {
  capturing = false;
  transition.renderMs = millis() - started;
  transition.committed = display.displayCommitted();
  transition.noActivation = transition.activationCount == 0;
  transition.captureError = captureError || pending.bytes24 != 0 || pending.bytes26 != 0;
  transition.completed = !transition.noActivation || expectedNoop;
  for (uint32_t i = 0; i < std::min<uint32_t>(transition.activationCount, MAX_ACTIVATIONS); ++i) {
    const auto& activation = transition.activations[i];
    transition.completed &= activation.completionSeen && activation.completed;
  }
  transition.valid = !transition.captureError && !transition.overflow && transition.completed &&
                     (transition.noActivation ? !transition.committed : transition.committed);
}

}  // namespace

namespace PanelDiagnostic {

void writeFixture(uint8_t* bw, uint8_t* lsb, uint8_t* msb) {
  memset(bw, 0xFF, PLANE_BYTES);
  if (lsb) memset(lsb, 0, PLANE_BYTES);
  if (msb) memset(msb, 0, PLANE_BYTES);
  constexpr const char* labels[] = {"WHITE", "LIGHT", "DARK", "BLACK"};
  for (uint8_t swatch = 0; swatch < 4; ++swatch) {
    const uint16_t x = 48 + swatch * 176;
    fillRectangle(bw, x, 80, 144, 224, swatch < 2);
    fillRectangle(lsb, x, 80, 144, 224, swatch == 2);
    fillRectangle(msb, x, 80, 144, 224, swatch == 1 || swatch == 2);
    drawLabel(bw, x, 320, labels[swatch]);
  }
  fillRectangle(bw, 8, 8, 28, 4, false);
  fillRectangle(bw, 8, 8, 4, 20, false);
  fillRectangle(bw, 760, 8, 12, 12, false);
  fillRectangle(bw, 780, 8, 12, 20, false);
  fillRectangle(bw, 8, 444, 4, 28, false);
  fillRectangle(bw, 8, 468, 32, 4, false);
  fillRectangle(bw, 20, 452, 8, 8, false);
  fillRectangle(bw, 756, 444, 36, 4, false);
  fillRectangle(bw, 788, 444, 4, 28, false);
  fillRectangle(bw, 756, 456, 20, 4, false);
  fillRectangle(bw, 756, 468, 12, 4, false);
}

bool renderFixture() {
  capturing = false;
  bwTargetKnown = false;
  const uint32_t next = captured.generation + 1;
  captured = Metadata{};
  captured.generation = next == 0 ? 1 : next;
  capturedBytes[0] = capturedBytes[1] = 0;
  if (display.getDisplayWidth() != WIDTH || display.getDisplayHeight() != HEIGHT || !display.getFrameBuffer()) {
    return false;
  }
  if (!captureBytes) {
    captureBytes = static_cast<uint8_t*>(heap_caps_malloc(2 * PLANE_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  }
  if (!captureBytes) return false;

  display.waitRefreshComplete();
  const uint32_t started = millis();
  writeFixture(display.getFrameBuffer(), captureBytes, captureBytes + PLANE_BYTES);
  display.beginDisplayWork();
  display.copyGrayscaleBuffers(captureBytes, captureBytes + PLANE_BYTES);
  startCapture(true);
  display.displayGrayCalibration(0, 0, WIDTH, HEIGHT);
  finishCapture(started, false);
  captured.renderMs = transition.renderMs;
  captured.valid = transition.valid && capturedBytes[0] == PLANE_BYTES && capturedBytes[1] == PLANE_BYTES;
  if (captured.valid) {
    captured.crc24 = mz_crc32(MZ_CRC32_INIT, captureBytes, PLANE_BYTES);
    captured.crc26 = mz_crc32(MZ_CRC32_INIT, captureBytes + PLANE_BYTES, PLANE_BYTES);
  }
  return captured.valid;
}

bool renderBwFixture(bool changed) {
  if (!captured.valid || !display.getFrameBuffer()) return false;
  display.waitRefreshComplete();
  const uint32_t started = millis();
  writeFixture(display.getFrameBuffer(), nullptr, nullptr);
  if (changed) display.getFrameBuffer()[BW_CHANGED_OFFSET] ^= BW_CHANGED_MASK;
  display.beginDisplayWork();
  startCapture(false, changed);
  display.displayBuffer(HalDisplay::FAST_REFRESH);
  finishCapture(started, bwTargetKnown && previousBwChanged == changed);
  if (transition.valid && transition.committed) {
    bwTargetKnown = true;
    previousBwChanged = changed;
  } else if (!transition.valid) {
    bwTargetKnown = false;
  }
  return transition.valid;
}

const TransitionMetadata& transitionMetadata() { return transition; }

const Metadata& metadata() { return captured; }

size_t readCapture(uint8_t ramCommand, uint32_t generation, uint32_t offset, uint8_t* output, size_t maxBytes) {
  const int index = planeIndex(ramCommand);
  if (capturing || !captured.valid || generation != captured.generation || index < 0 || !output ||
      offset >= PLANE_BYTES) {
    return 0;
  }
  const size_t count = std::min<size_t>(maxBytes, PLANE_BYTES - offset);
  memcpy(output, captureBytes + index * PLANE_BYTES + offset, count);
  return count;
}

void releaseCapture() {
  capturing = false;
  bwTargetKnown = false;
  captured.valid = false;
  heap_caps_free(captureBytes);
  captureBytes = nullptr;
}

}  // namespace PanelDiagnostic

extern "C" bool freeink_panel_capture_enabled() { return capturing; }

extern "C" void freeink_panel_capture_plane(uint8_t command, uint32_t generation, uint16_t width, uint16_t height,
                                            uint32_t offset, const uint8_t* data, uint16_t count) {
  if (!capturing) return;
  const int index = planeIndex(command);
  if (index < 0 || width != WIDTH || height != HEIGHT || offset > PLANE_BYTES || count > PLANE_BYTES - offset ||
      !data || (pending.driverGeneration != 0 && pending.driverGeneration != generation) ||
      (transition.activationCount != 0 && transition.activations[0].driverGeneration != generation)) {
    captureError = true;
    return;
  }
  uint32_t& length = index == 0 ? pending.bytes24 : pending.bytes26;
  uint32_t& crc = index == 0 ? pending.crc24 : pending.crc26;
  if (offset != length) {
    captureError = true;
    return;
  }
  pending.driverGeneration = generation;
  crc = mz_crc32(crc, data, count);
  length += count;
  if (capturingGray) {
    captured.driverGeneration = generation;
    memcpy(captureBytes + index * PLANE_BYTES + offset, data, count);
    capturedBytes[index] = length;
  }
}

extern "C" void freeink_panel_capture_activation(uint8_t control, uint32_t generation, uint32_t busyMs) {
  if (!capturing) return;
  if (generation != pending.driverGeneration || (pending.bytes24 != 0 && pending.bytes24 != PLANE_BYTES) ||
      (pending.bytes26 != 0 && pending.bytes26 != PLANE_BYTES))
    captureError = true;
  pending.control = control;
  pending.busyMs = busyMs;
  if (transition.activationCount < MAX_ACTIVATIONS) {
    transition.activations[transition.activationCount] = pending;
  } else {
    transition.overflow = true;
    captureError = true;
  }
  ++transition.activationCount;
  pending = Activation{};
  if (capturingGray) {
    captured.control = control;
    captured.busyMs += busyMs;
    ++captured.activationCount;
  }
}

extern "C" void freeink_panel_capture_completion(uint8_t control, uint32_t generation, uint32_t busyMs,
                                                 bool completed) {
  if (!capturing) return;
  if (transition.activationCount == 0 || transition.activationCount > MAX_ACTIVATIONS) {
    captureError = true;
    return;
  }
  auto& activation = transition.activations[transition.activationCount - 1];
  if (activation.control != control || activation.driverGeneration != generation || activation.busyMs != busyMs ||
      activation.completionSeen) {
    captureError = true;
    return;
  }
  activation.completionSeen = true;
  activation.completed = completed;
}
