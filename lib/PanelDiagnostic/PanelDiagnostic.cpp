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

}  // namespace

namespace PanelDiagnostic {

void writeFixture(uint8_t* bw, uint8_t* lsb, uint8_t* msb) {
  memset(bw, 0xFF, PLANE_BYTES);
  memset(lsb, 0, PLANE_BYTES);
  memset(msb, 0, PLANE_BYTES);
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
  const uint32_t next = captured.generation + 1;
  captured = Metadata{};
  captured.generation = next == 0 ? 1 : next;
  capturedBytes[0] = capturedBytes[1] = 0;
  captureError = false;
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
  capturing = true;
  display.displayGrayCalibration(0, 0, WIDTH, HEIGHT);
  capturing = false;
  captured.renderMs = millis() - started;
  captured.valid = !captureError && display.displayCommitted() && captured.activationCount != 0 &&
                   capturedBytes[0] == PLANE_BYTES && capturedBytes[1] == PLANE_BYTES;
  if (captured.valid) {
    captured.crc24 = mz_crc32(MZ_CRC32_INIT, captureBytes, PLANE_BYTES);
    captured.crc26 = mz_crc32(MZ_CRC32_INIT, captureBytes + PLANE_BYTES, PLANE_BYTES);
  }
  return captured.valid;
}

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
      !data || (captured.driverGeneration != 0 && captured.driverGeneration != generation)) {
    captureError = true;
    return;
  }
  if (offset == 0) capturedBytes[index] = 0;
  if (offset != capturedBytes[index]) {
    captureError = true;
    return;
  }
  captured.driverGeneration = generation;
  memcpy(captureBytes + index * PLANE_BYTES + offset, data, count);
  capturedBytes[index] += count;
}

extern "C" void freeink_panel_capture_activation(uint8_t control, uint32_t generation, uint32_t busyMs) {
  if (!capturing) return;
  if (generation != captured.driverGeneration) captureError = true;
  captured.control = control;
  captured.busyMs += busyMs;
  ++captured.activationCount;
}
