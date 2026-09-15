#pragma once

#include <stddef.h>
#include <stdint.h>

namespace PanelDiagnostic {

constexpr uint16_t WIDTH = 800;
constexpr uint16_t HEIGHT = 480;
constexpr uint32_t PLANE_BYTES = WIDTH * HEIGHT / 8;

struct Metadata {
  uint16_t width = WIDTH;
  uint16_t height = HEIGHT;
  uint32_t generation = 0;
  uint32_t driverGeneration = 0;
  uint32_t planeBytes = PLANE_BYTES;
  uint32_t crc24 = 0;
  uint32_t crc26 = 0;
  uint32_t renderMs = 0;
  uint32_t busyMs = 0;
  uint32_t activationCount = 0;
  uint8_t control = 0;
  bool valid = false;
};

void writeFixture(uint8_t* bw, uint8_t* lsb, uint8_t* msb);
// Caller holds the rendering lock and keeps activity rendering paused until diagnostic exit.
bool renderFixture();
const Metadata& metadata();
size_t readCapture(uint8_t ramCommand, uint32_t generation, uint32_t offset, uint8_t* output, size_t maxBytes);
void releaseCapture();

}  // namespace PanelDiagnostic
