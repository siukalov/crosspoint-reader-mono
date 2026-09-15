#pragma once

#include <algorithm>

#include "UsbFileTransfer.h"

namespace usb_transfer {

class SerialLineBuffer {
 public:
  enum class Result { Pending, Complete, Malformed };
  Result push(uint8_t byte) {
    if (byte == '\n') {
      const bool malformed = discard_;
      if (length_ && bytes_[length_ - 1] == '\r') --length_;
      bytes_[length_] = '\0';
      discard_ = false;
      length_ = 0;
      return malformed ? Result::Malformed : Result::Complete;
    }
    if (discard_) return Result::Pending;
    if (length_ >= UsbFileTransfer::kMaxLineBytes || (length_ && bytes_[length_ - 1] == '\r') ||
        (byte != '\r' && (byte < 32 || byte > 126))) {
      discard_ = true;
      return Result::Pending;
    }
    bytes_[length_++] = static_cast<char>(byte);
    return Result::Pending;
  }
  const char* line() const { return bytes_; }

 private:
  char bytes_[UsbFileTransfer::kMaxLineBytes + 1]{};
  size_t length_ = 0;
  bool discard_ = false;
};

template <typename Writer>
size_t writeSerialChunks(Writer& writer, const uint8_t* data, size_t size, uint32_t timeoutMs) {
  const uint32_t started = writer.now();
  size_t sent = 0;
  while (sent < size && writer.now() - started < timeoutMs) {
    const size_t chunk = std::min<size_t>(size - sent, 64);
    const size_t written = writer.write(data + sent, chunk);
    if (written > chunk) return sent;
    sent += written;
    writer.flush();
    if (!written) writer.idle();
  }
  return sent;
}

class SerialTransport final : public Tx {
 public:
  bool sendLine(const std::string& line) override;
};

}  // namespace usb_transfer
