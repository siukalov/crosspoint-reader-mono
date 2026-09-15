#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace usb_transfer {

class StorageBackend {
 public:
  virtual ~StorageBackend() = default;
  virtual bool exists(const std::string& path, bool& result) = 0;
  virtual bool makeDirectory(const std::string& path) = 0;
  virtual bool createExclusive(const std::string& path) = 0;
  virtual bool openRead(const std::string& path) = 0;
  virtual bool size(uint32_t& result) = 0;
  virtual bool write(const uint8_t* data, size_t length, size_t& written) = 0;
  virtual bool read(uint8_t* data, size_t length, size_t& received) = 0;
  virtual bool flush() = 0;
  // Always releases the handle, including when reporting a flush or close error.
  virtual bool close() = 0;
  virtual bool remove(const std::string& path) = 0;
  virtual bool renameNoReplace(const std::string& from, const std::string& to) = 0;
};

class Tx {
 public:
  virtual ~Tx() = default;
  // Sends one complete line plus newline under the shared serial TX lock.
  virtual bool sendLine(const std::string& line) = 0;
};

class UsbFileTransfer {
 public:
  static constexpr size_t kChunkBytes = 512;
  static constexpr size_t kMaxLineBytes = 1100;
  static constexpr uint32_t kMaxFileBytes = 32U * 1024U * 1024U;
  static constexpr uint32_t kIdleTimeoutMs = 30000;

  UsbFileTransfer(StorageBackend& storage, Tx& tx) : storage_(storage), tx_(tx) {}
  // Call tick before dispatch; pass one line without its newline. No other consumer may read payload.
  bool pollLine(const std::string& line);
  // Performs at most one 512-byte verification read per call; never waits for host input.
  void tick(uint32_t nowMs);
  bool active() const { return state_ != State::Idle; }
  bool abort();

 private:
  enum class State { Idle, Upload, Verify, Download, Cleanup };
  StorageBackend& storage_;
  Tx& tx_;
  State state_ = State::Idle;
  bool handleOpen_ = false;
  bool ownsTemporary_ = false;
  bool lastChunkValid_ = false;
  bool downloadEof_ = false;
  uint32_t now_ = 0;
  uint32_t lastActivity_ = 0;
  uint32_t length_ = 0;
  uint32_t position_ = 0;
  uint32_t nextSequence_ = 0;
  uint32_t expectedCrc_ = 0;
  uint32_t storedCrc_ = 0xffffffffU;
  size_t lastChunkLength_ = 0;
  std::array<uint8_t, kChunkBytes> lastChunk_{};
  std::string destination_;
  std::string temporary_;
  std::string terminal_ = "CP1 IDLE";

  bool send(const std::string& response);
  void error(const std::string& reason, bool terminate = false);
  void fail(const std::string& reason);
  bool cleanup();
  bool closeHandle();
  void beginUpload(const std::array<std::string_view, 6>& fields);
  void acceptData(const std::array<std::string_view, 6>& fields);
  void commitUpload();
  void verifyStored();
  void beginDownload(const std::array<std::string_view, 6>& fields);
  void readChunk(const std::array<std::string_view, 6>& fields);
  std::string status() const;
};

}  // namespace usb_transfer
