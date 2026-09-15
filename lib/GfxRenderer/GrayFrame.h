#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>

class GrayFrame {
 public:
  struct Plane {
    uint8_t* data = nullptr;
    size_t capacity = 0;
  };

  GrayFrame() = default;
  GrayFrame(Plane b, Plane l, Plane m, size_t width, size_t rows, size_t stride, size_t y0 = 0)
      : b_(b), l_(l), m_(m), width_(width), rows_(rows), stride_(stride), y0_(y0) {}

  Plane b() const { return b_; }
  Plane l() const { return l_; }
  Plane m() const { return m_; }
  size_t width() const { return width_; }
  size_t rows() const { return rows_; }
  size_t stride() const { return stride_; }
  size_t y0() const { return y0_; }

  bool valid() const {
    return width_ != 0 && rows_ != 0 && stride_ >= width_ / 8 + (width_ % 8 != 0) &&
           y0_ <= std::numeric_limits<size_t>::max() - rows_ && fits(b_) && fits(l_) && fits(m_);
  }

  bool shade(size_t x, size_t y, uint8_t& darkness) const {
    if (!containsPixel(x, y)) return false;
    const size_t offset = (y - y0_) * stride_ + x / 8;
    const uint8_t mask = 0x80 >> (x % 8);
    darkness = (m_.data[offset] & mask) ? ((l_.data[offset] & mask) ? 2 : 1) : ((b_.data[offset] & mask) ? 0 : 3);
    return true;
  }

  bool writeShade(size_t x, size_t y, uint8_t darkness) const {
    return x < width_ && writeMasked(x / 8, y, 0x80 >> (x % 8), darkness);
  }

  bool writeCoverage(size_t x, size_t y, uint8_t coverage, bool blackInk) const {
    uint8_t destination;
    if (coverage > 3 || !shade(x, y, destination)) return false;
    if (coverage == 0) return true;
    const uint8_t blended = (destination * (3 - coverage) + (blackInk ? 3 * coverage : 0) + 1) / 3;
    return writeShade(x, y, blended);
  }

  bool writeMasked(size_t byteX, size_t y, uint8_t mask, uint8_t darkness) const {
    if (darkness > 3 || !containsByte(byteX, y)) return false;
    const size_t offset = (y - y0_) * stride_ + byteX;
    replace(b_.data[offset], mask, darkness < 2 ? 0xFF : 0);
    replace(l_.data[offset], mask, darkness == 2 ? 0xFF : 0);
    replace(m_.data[offset], mask, darkness == 1 || darkness == 2 ? 0xFF : 0);
    return true;
  }

  bool copyMasked(const GrayFrame& source, size_t sourceByteX, size_t sourceY, size_t byteX, size_t y,
                  uint8_t mask) const {
    if (!containsByte(byteX, y) || !source.containsByte(sourceByteX, sourceY)) return false;
    const size_t destinationOffset = (y - y0_) * stride_ + byteX;
    const size_t sourceOffset = (sourceY - source.y0_) * source.stride_ + sourceByteX;
    const uint8_t b = source.b_.data[sourceOffset];
    const uint8_t l = source.l_.data[sourceOffset];
    const uint8_t m = source.m_.data[sourceOffset];
    replace(b_.data[destinationOffset], mask, b);
    replace(l_.data[destinationOffset], mask, l);
    replace(m_.data[destinationOffset], mask, m);
    return true;
  }

  bool fill(uint8_t darkness) const {
    if (darkness > 3 || !valid()) return false;
    const size_t size = rows_ * stride_;
    std::memset(b_.data, darkness < 2 ? 0xFF : 0, size);
    std::memset(l_.data, darkness == 2 ? 0xFF : 0, size);
    std::memset(m_.data, darkness == 1 || darkness == 2 ? 0xFF : 0, size);
    return true;
  }

  bool invert() const {
    if (!valid()) return false;
    for (size_t i = 0; i < rows_ * stride_; ++i) {
      b_.data[i] = ~b_.data[i];
      l_.data[i] = m_.data[i] & ~l_.data[i];
    }
    return true;
  }

 private:
  Plane b_;
  Plane l_;
  Plane m_;
  size_t width_ = 0;
  size_t rows_ = 0;
  size_t stride_ = 0;
  size_t y0_ = 0;

  bool fits(Plane plane) const { return plane.data && rows_ <= plane.capacity / stride_; }

  bool containsByte(size_t byteX, size_t y) const { return valid() && byteX < stride_ && y >= y0_ && y - y0_ < rows_; }

  bool containsPixel(size_t x, size_t y) const { return x < width_ && containsByte(x / 8, y); }

  static void replace(uint8_t& destination, uint8_t mask, uint8_t source) {
    destination = (destination & ~mask) | (source & mask);
  }
};
