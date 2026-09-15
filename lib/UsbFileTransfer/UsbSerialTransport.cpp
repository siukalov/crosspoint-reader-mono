#include "UsbSerialTransport.h"

#include <Logging.h>

namespace usb_transfer {

bool SerialTransport::sendLine(const std::string& line) {
  if (line.size() > UsbFileTransfer::kMaxLineBytes || line.find_first_of("\r\n") != std::string::npos) return false;
  const std::string frame = "\n" + line + "\n";
  SerialTxLock lock(1000);
  return lock && writeSerialTx(reinterpret_cast<const uint8_t*>(frame.data()), frame.size(), 1000) == frame.size();
}

}  // namespace usb_transfer
