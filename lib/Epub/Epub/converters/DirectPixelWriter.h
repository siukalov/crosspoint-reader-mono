#pragma once

#include <GfxRenderer.h>
#include <HalDisplay.h>
#include <stdint.h>

#include <cassert>

struct DirectPixelWriter {
  const GfxRenderer* renderer = nullptr;
  uint32_t generation = 0;
  GrayFrame tuple;
  bool hasTuple;
  uint8_t* fb;
  uint8_t* fbSecondary;
  GfxRenderer::RenderMode mode;
  uint16_t displayWidthBytes;  // Runtime framebuffer stride (X4: 100, X3: 99)
  int originY;
  int clipRows;
  bool logicalClipEnabled;
  bool logicalRowVisible;
  int logicalClipX0, logicalClipY0, logicalClipX1, logicalClipY1;

  int phyXBase, phyYBase;
  int phyXStepX, phyYStepX;  // per logical-X step
  int phyXStepY, phyYStepY;  // per logical-Y step

  int rowPhyXBase, rowPhyYBase;

  void init(GfxRenderer& renderer) {
    this->renderer = &renderer;
    generation = renderer.getTargetGeneration();
    tuple = renderer.getWriteTuple();
    hasTuple = tuple.valid();
    fb = renderer.getWriteTarget();
    fbSecondary = renderer.getSecondaryWriteTarget();
    originY = renderer.getWriteOriginY();
    clipRows = renderer.getWriteRows();
    mode = renderer.getRenderMode();
    displayWidthBytes = renderer.getDisplayWidthBytes();
    logicalClipEnabled = renderer.grayscaleClipEnabled() && (hasTuple || mode >= GfxRenderer::GRAYSCALE_LSB);
    logicalRowVisible = true;
    logicalClipX0 = renderer.grayscaleClipX0();
    logicalClipY0 = renderer.grayscaleClipY0();
    logicalClipX1 = renderer.grayscaleClipX1();
    logicalClipY1 = renderer.grayscaleClipY1();

    const int phyW = renderer.getDisplayWidth();
    const int phyH = renderer.getDisplayHeight();

    switch (renderer.getOrientation()) {
      case GfxRenderer::Portrait:
        phyXBase = 0;
        phyYBase = phyH - 1;
        phyXStepX = 0;
        phyYStepX = -1;
        phyXStepY = 1;
        phyYStepY = 0;
        break;
      case GfxRenderer::LandscapeClockwise:
        phyXBase = phyW - 1;
        phyYBase = phyH - 1;
        phyXStepX = -1;
        phyYStepX = 0;
        phyXStepY = 0;
        phyYStepY = -1;
        break;
      case GfxRenderer::PortraitInverted:
        phyXBase = phyW - 1;
        phyYBase = 0;
        phyXStepX = 0;
        phyYStepX = 1;
        phyXStepY = -1;
        phyYStepY = 0;
        break;
      case GfxRenderer::LandscapeCounterClockwise:
        phyXBase = 0;
        phyYBase = 0;
        phyXStepX = 1;
        phyYStepX = 0;
        phyXStepY = 0;
        phyYStepY = 1;
        break;
      default:
        phyXBase = 0;
        phyYBase = 0;
        phyXStepX = 1;
        phyYStepX = 0;
        phyXStepY = 0;
        phyYStepY = 1;
        break;
    }
  }

  inline void beginRow(int logicalY) {
    rowPhyXBase = phyXBase + logicalY * phyXStepY;
    rowPhyYBase = phyYBase + logicalY * phyYStepY;
    logicalRowVisible = !logicalClipEnabled || (logicalY >= logicalClipY0 && logicalY < logicalClipY1);
  }

  inline void bandColRange(int xBase, int width, int& colStart, int& colEnd) const {
    assert(phyYStepX == 0 || phyYStepX == 1 || phyYStepX == -1);
    colStart = 0;
    colEnd = width;
    if (!logicalRowVisible) {
      colEnd = 0;
      return;
    }
    if (logicalClipEnabled) {
      const int clipStart = logicalClipX0 - xBase;
      const int clipEnd = logicalClipX1 - xBase;
      if (clipStart > colStart) colStart = clipStart;
      if (clipEnd < colEnd) colEnd = clipEnd;
      if (colStart < 0) colStart = 0;
      if (colEnd > width) colEnd = width;
      if (colStart >= colEnd) {
        colEnd = colStart;
        return;
      }
    }
    if (phyYStepX == 0) {
      const int sy = rowPhyYBase - originY;
      if (static_cast<unsigned>(sy) >= static_cast<unsigned>(clipRows)) colEnd = 0;
      return;
    }
    const int loY = originY;
    const int hiY = originY + clipRows - 1;
    int xLo, xHi;
    if (phyYStepX > 0) {
      xLo = loY - rowPhyYBase;
      xHi = hiY - rowPhyYBase;
    } else {
      xLo = rowPhyYBase - hiY;
      xHi = rowPhyYBase - loY;
    }
    const int cs = xLo - xBase;
    const int ce = xHi - xBase + 1;  // exclusive
    if (cs > colStart) colStart = cs;
    if (ce < colEnd) colEnd = ce;
    if (colStart < 0) colStart = 0;
    if (colEnd > width) colEnd = width;
    if (colStart > colEnd) colStart = colEnd;
  }

  inline void writePixel(int logicalX, uint8_t pixelValue) const {
    if (!renderer->isTargetCurrent(generation)) return;
    if (!logicalRowVisible || (logicalClipEnabled && (logicalX < logicalClipX0 || logicalX >= logicalClipX1))) {
      return;
    }
    if (hasTuple) {
      if (pixelValue < 3) {
        tuple.writeShade(rowPhyXBase + logicalX * phyXStepX, rowPhyYBase + logicalX * phyYStepX, 3 - pixelValue);
      }
      return;
    }
    const bool dual = mode == GfxRenderer::GRAYSCALE_BOTH;
    bool draw;
    bool state;
    switch (mode) {
      case GfxRenderer::BW:
        draw = (pixelValue < 3);
        state = true;
        break;
      case GfxRenderer::BW_GRAY_BASE:
        draw = (pixelValue < 2);
        state = true;
        break;
      case GfxRenderer::GRAYSCALE_MSB:
        draw = (pixelValue == 1 || pixelValue == 2);
        state = false;
        break;
      case GfxRenderer::GRAYSCALE_LSB:
        draw = (pixelValue == 1);
        state = false;
        break;
      case GfxRenderer::GRAYSCALE_BOTH:
        draw = (pixelValue == 1 || pixelValue == 2);
        state = false;
        break;
      default:
        return;
    }

    if (!draw) return;

    const int phyX = rowPhyXBase + logicalX * phyXStepX;
    const int phyY = rowPhyYBase + logicalX * phyYStepX;

    const int sy = phyY - originY;
    if (static_cast<unsigned>(sy) >= static_cast<unsigned>(clipRows)) return;

    const uint16_t byteIndex = static_cast<uint16_t>(sy * displayWidthBytes + (phyX >> 3));
    const uint8_t bitMask = 1 << (7 - (phyX & 7));

    if (dual) {
      assert(fbSecondary != nullptr);
      if (pixelValue == 1) fb[byteIndex] |= bitMask;
      fbSecondary[byteIndex] |= bitMask;
      return;
    }

    if (state) {
      fb[byteIndex] &= ~bitMask;  // Clear bit (draw black)
    } else {
      fb[byteIndex] |= bitMask;  // Set bit (draw white)
    }
  }
};

struct DirectCacheWriter {
  uint8_t* buffer;
  int bytesPerRow;
  int bandRows;
  int originX;
  uint8_t* rowPtr;  // Pre-computed for current row; nullptr if row is out of band

  void init(uint8_t* cacheBuffer, int cacheBytesPerRow, int cacheBandRows, int cacheOriginX) {
    buffer = cacheBuffer;
    bytesPerRow = cacheBytesPerRow;
    bandRows = cacheBandRows;
    originX = cacheOriginX;
    rowPtr = nullptr;
  }

  inline void beginRow(int screenY, int cacheOriginY) {
    const int localRow = screenY - cacheOriginY;
    rowPtr = (static_cast<unsigned>(localRow) < static_cast<unsigned>(bandRows))
                 ? buffer + (size_t)localRow * bytesPerRow
                 : nullptr;
  }

  inline void writePixel(int screenX, uint8_t value) const {
    if (!rowPtr) return;
    const int localX = screenX - originX;
    const int byteIdx = localX >> 2;  // localX / 4
    if (static_cast<unsigned>(byteIdx) >= static_cast<unsigned>(bytesPerRow)) return;
    const int bitShift = 6 - (localX & 3) * 2;  // MSB first: pixel 0 at bits 6-7
    rowPtr[byteIdx] = (rowPtr[byteIdx] & ~(0x03 << bitShift)) | ((value & 0x03) << bitShift);
  }
};
