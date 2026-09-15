#include "UsbStorage.h"

#include <limits>

namespace usb_transfer {

bool SdStorage::exists(const std::string& path, bool& result) { return Storage.exists(path.c_str(), result); }

bool SdStorage::makeDirectory(const std::string& path) {
  bool present = false;
  if (!exists(path, present)) return false;
  if (!present && !Storage.mkdir(path.c_str(), false)) return false;
  HalFile directory = Storage.open(path.c_str(), O_RDONLY);
  if (!directory) return false;
  const bool synced = directory.isDirectory() && directory.sync();
  const bool closed = directory.close();
  return synced && closed;
}

bool SdStorage::createExclusive(const std::string& path) {
  if (file_ || !Storage.ready()) return false;
  file_ = Storage.open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL);
  return file_.isOpen();
}

bool SdStorage::openRead(const std::string& path) {
  if (file_ || !Storage.ready()) return false;
  file_ = Storage.open(path.c_str(), O_RDONLY);
  if (!file_) return false;
  if (!file_.isDirectory()) return true;
  file_.close();
  return false;
}

bool SdStorage::size(uint32_t& result) {
  result = 0;
  if (!file_) return false;
  const uint64_t bytes = file_.fileSize64();
  if (bytes > std::numeric_limits<uint32_t>::max()) return false;
  result = static_cast<uint32_t>(bytes);
  return true;
}

bool SdStorage::write(const uint8_t* data, size_t length, size_t& written) {
  written = 0;
  if (!file_) return false;
  written = file_.write(data, length);
  return written == length;
}

bool SdStorage::read(uint8_t* data, size_t length, size_t& received) {
  received = 0;
  if (!file_) return false;
  const int count = file_.read(data, length);
  if (count < 0) return false;
  received = static_cast<size_t>(count);
  return true;
}

bool SdStorage::flush() { return file_ && file_.sync(); }

bool SdStorage::close() {
  if (!file_) return true;
  const bool synced = file_.sync();
  const bool closed = file_.close();
  return synced && closed;
}

bool SdStorage::remove(const std::string& path) { return Storage.ready() && Storage.remove(path.c_str()); }

bool SdStorage::renameNoReplace(const std::string& from, const std::string& to) {
  return Storage.renameNoReplace(from.c_str(), to.c_str());
}

}  // namespace usb_transfer
