#pragma once

#include <HalStorage.h>

#include "UsbFileTransfer.h"

namespace usb_transfer {

class SdStorage final : public StorageBackend {
 public:
  bool exists(const std::string& path, bool& result) override;
  bool makeDirectory(const std::string& path) override;
  bool createExclusive(const std::string& path) override;
  bool openRead(const std::string& path) override;
  bool size(uint32_t& result) override;
  bool write(const uint8_t* data, size_t length, size_t& written) override;
  bool read(uint8_t* data, size_t length, size_t& received) override;
  bool flush() override;
  bool close() override;
  bool remove(const std::string& path) override;
  bool renameNoReplace(const std::string& from, const std::string& to) override;

 private:
  HalFile file_;
};

}  // namespace usb_transfer
