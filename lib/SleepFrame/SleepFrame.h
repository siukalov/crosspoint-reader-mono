#pragma once

#include <GfxRenderer.h>

#include <memory>

namespace SleepFrame {

struct Geometry {
  uint16_t width;
  uint16_t height;
  uint16_t stride;
  GfxRenderer::Orientation orientation;
};

class LoadedFrame {
 public:
  LoadedFrame() = default;
  const GfxRenderer::FrameSnapshot& snapshot() const { return snapshot_; }

 private:
  LoadedFrame& operator=(LoadedFrame&&) = default;
  struct Deleter {
    void operator()(uint8_t* bytes) const;
  };
  std::unique_ptr<uint8_t, Deleter> storage_;
  GfxRenderer::FrameSnapshot snapshot_;
  friend bool load(const char* path, Geometry expected, bool retentionEnabled, LoadedFrame& out);
};

bool save(const char* path, const GfxRenderer::FrameSnapshot& source, bool retentionEnabled);
// Failure leaves out unchanged. Its owned storage remains valid until destruction or replacement.
bool load(const char* path, Geometry expected, bool retentionEnabled, LoadedFrame& out);

}  // namespace SleepFrame
