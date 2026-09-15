#include "UsbFileTransfer.h"

#include <algorithm>
#include <limits>

namespace usb_transfer {
namespace {

int nibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool decode(std::string_view input, uint8_t* output, size_t capacity, size_t& length) {
  if (input.empty() || input.size() % 2 || input.size() / 2 > capacity) return false;
  length = input.size() / 2;
  for (size_t i = 0; i < length; ++i) {
    const int high = nibble(input[i * 2]);
    const int low = nibble(input[i * 2 + 1]);
    if (high < 0 || low < 0) return false;
    output[i] = static_cast<uint8_t>((high << 4) | low);
  }
  return true;
}

std::string encode(const uint8_t* data, size_t length) {
  const char* digits = "0123456789abcdef";
  std::string result(length * 2, '0');
  for (size_t i = 0; i < length; ++i) {
    result[i * 2] = digits[data[i] >> 4];
    result[i * 2 + 1] = digits[data[i] & 15];
  }
  return result;
}

std::string crcText(uint32_t value) {
  const uint8_t bytes[] = {static_cast<uint8_t>(value >> 24), static_cast<uint8_t>(value >> 16),
                           static_cast<uint8_t>(value >> 8), static_cast<uint8_t>(value)};
  return encode(bytes, sizeof(bytes));
}

bool decimal(std::string_view text, uint32_t& value) {
  if (text.empty() || text.size() > 10) return false;
  value = 0;
  for (char c : text) {
    if (c < '0' || c > '9') return false;
    const uint32_t digit = static_cast<uint32_t>(c - '0');
    if (value > (std::numeric_limits<uint32_t>::max() - digit) / 10) return false;
    value = value * 10 + digit;
  }
  return true;
}

bool parseCrc(std::string_view text, uint32_t& value) {
  if (text.size() != 8) return false;
  value = 0;
  for (char c : text) {
    const int digit = nibble(c);
    if (digit < 0) return false;
    value = (value << 4) | static_cast<uint32_t>(digit);
  }
  return true;
}

bool asciiAlphaNumeric(char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9'); }

bool validName(const std::string& name) {
  if (name.empty() || name.size() > 128 || name.front() == '.' || name.back() == '.' || name.back() == ' ' ||
      name.find("..") != std::string::npos)
    return false;
  for (char c : name) {
    if (!asciiAlphaNumeric(c) && c != ' ' && c != '-' && c != '_' && c != '.' && c != '(' && c != ')') return false;
  }
  return true;
}

bool validRoot(const std::string& root) {
  if (root == "/Classics" || root == "/fonts") return true;
  if (root.compare(0, 7, "/fonts/") != 0 || root.size() <= 7 || root.size() > 135) return false;
  const std::string family = root.substr(7);
  if (!asciiAlphaNumeric(family.front())) return false;
  for (char c : family) {
    if (!asciiAlphaNumeric(c) && c != '-' && c != '_') return false;
  }
  return true;
}

bool endsWith(const std::string& value, const std::string& suffix) {
  return value.size() > suffix.size() && value.compare(value.size() - suffix.size(), suffix.size(), suffix) == 0;
}

bool decodePath(std::string_view rootHex, std::string_view nameHex, std::string& root, std::string& path) {
  std::array<uint8_t, 135> buffer{};
  size_t count = 0;
  if (!decode(rootHex, buffer.data(), buffer.size(), count)) return false;
  root.assign(reinterpret_cast<const char*>(buffer.data()), count);
  if (!validRoot(root) || !decode(nameHex, buffer.data(), 128, count)) return false;
  const std::string name(reinterpret_cast<const char*>(buffer.data()), count);
  if (!validName(name)) return false;
  const bool extension = root == "/Classics" ? endsWith(name, ".epub")
                                             : endsWith(name, ".cpfont") || name == "OFL.txt" ||
                                                   name == "LICENSE.txt" || name == "LICENSE.md";
  if (!extension) return false;
  path = root + "/" + name;
  return true;
}

size_t split(std::string_view line, std::array<std::string_view, 6>& fields) {
  size_t start = 0;
  size_t count = 0;
  while (start < line.size()) {
    const size_t end = line.find(' ', start);
    if (end == start || count == fields.size()) return 0;
    fields[count++] = line.substr(start, end == std::string_view::npos ? end : end - start);
    if (end == std::string_view::npos) return count;
    start = end + 1;
  }
  return 0;
}

uint32_t updateCrc(uint32_t crc, const uint8_t* bytes, size_t count) {
  for (size_t i = 0; i < count; ++i) {
    crc ^= bytes[i];
    for (int bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
  }
  return crc;
}

}  // namespace

bool UsbFileTransfer::closeHandle() {
  if (!handleOpen_) return true;
  handleOpen_ = false;
  return storage_.close();
}

bool UsbFileTransfer::cleanup() {
  const bool closed = closeHandle();
  bool removed = true;
  if (ownsTemporary_) {
    removed = storage_.remove(temporary_);
    if (removed) ownsTemporary_ = false;
  }
  state_ = ownsTemporary_ ? State::Cleanup : State::Idle;
  return closed && removed;
}

bool UsbFileTransfer::abort() {
  const bool result = cleanup();
  terminal_ = result ? "CP1 ABORTED" : "CP1 ERROR IO";
  return result;
}

void UsbFileTransfer::fail(const std::string& reason) {
  terminal_ = cleanup() ? "CP1 ERROR " + reason : "CP1 ERROR IO";
}

bool UsbFileTransfer::send(const std::string& response) {
  if (tx_.sendLine(response)) return true;
  fail("TX");
  return false;
}

void UsbFileTransfer::error(const std::string& reason, bool terminate) {
  if (terminate) {
    fail(reason);
    send(terminal_);
  } else {
    send("CP1 ERROR " + reason);
  }
}

std::string UsbFileTransfer::status() const {
  if (state_ == State::Idle || state_ == State::Cleanup) return terminal_;
  const std::string progress = std::to_string(position_) + " " + std::to_string(length_);
  if (state_ == State::Verify) return "CP1 VERIFY " + progress;
  return std::string(state_ == State::Upload ? "CP1 UPLOAD " : "CP1 READBACK ") + progress + " " +
         std::to_string(nextSequence_);
}

bool UsbFileTransfer::pollLine(const std::string& raw) {
  const bool protocol = raw == "CP1" || raw.compare(0, 4, "CP1 ") == 0;
  if (!protocol) {
    if (!active()) return false;
    error("BUSY");
    return true;
  }
  if (raw.size() > kMaxLineBytes) {
    error("OVERSIZE", active());
    return true;
  }
  std::string_view line(raw);
  if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
  std::array<std::string_view, 6> fields{};
  const size_t count = split(line, fields);
  if (count < 2) {
    error("MALFORMED", active());
    return true;
  }
  lastActivity_ = now_;
  const auto& command = fields[1];
  if (command == "PUT" && count == 6)
    beginUpload(fields);
  else if (command == "DATA" && count == 4)
    acceptData(fields);
  else if (command == "GET" && count == 4)
    beginDownload(fields);
  else if (command == "READ" && count == 3)
    readChunk(fields);
  else if (command == "COMMIT" && count == 2)
    commitUpload();
  else if (command == "STATUS" && count == 2)
    send(status());
  else if (command == "ABORT" && count == 2) {
    abort();
    send(terminal_);
  } else if (command == "DONE" && count == 2) {
    if (state_ == State::Idle && terminal_ == "CP1 CLOSED")
      send(terminal_);
    else if (state_ != State::Download)
      error("STATE");
    else if (!downloadEof_)
      error("LENGTH", true);
    else {
      terminal_ = cleanup() ? "CP1 CLOSED" : "CP1 ERROR IO";
      send(terminal_);
    }
  } else
    error("MALFORMED", active());
  return true;
}

void UsbFileTransfer::beginUpload(const std::array<std::string_view, 6>& fields) {
  std::string root;
  std::string path;
  uint32_t length = 0;
  uint32_t crc = 0;
  if (!decodePath(fields[2], fields[3], root, path)) return error("PATH");
  if (!decimal(fields[4], length) || !length || length > kMaxFileBytes) return error("SIZE", active());
  if (!parseCrc(fields[5], crc)) return error("CRC");
  if (active()) {
    if (state_ == State::Upload && !position_ && path == destination_ && length == length_ && crc == expectedCrc_)
      send("CP1 READY 512");
    else
      error("BUSY");
    return;
  }
  bool exists = false;
  if (!storage_.exists(path, exists)) return error("IO");
  if (exists) return error("EXISTS");
  const std::string base = root == "/Classics" ? root : "/fonts";
  if (!storage_.makeDirectory(base) || (root != base && !storage_.makeDirectory(root))) return error("IO");
  destination_ = path;
  temporary_ = path + ".cp1.part";
  if (!storage_.createExclusive(temporary_)) return error("IO");
  ownsTemporary_ = true;
  handleOpen_ = true;
  state_ = State::Upload;
  length_ = length;
  expectedCrc_ = crc;
  position_ = nextSequence_ = 0;
  lastChunkValid_ = false;
  terminal_ = "CP1 IDLE";
  send("CP1 READY 512");
}

void UsbFileTransfer::acceptData(const std::array<std::string_view, 6>& fields) {
  if (state_ != State::Upload) return error("STATE");
  std::array<uint8_t, kChunkBytes> data{};
  size_t count = 0;
  uint32_t sequence = 0;
  if (!decimal(fields[2], sequence) || !decode(fields[3], data.data(), data.size(), count)) return error("DATA", true);
  if (lastChunkValid_ && sequence == nextSequence_ - 1) {
    if (count == lastChunkLength_ && std::equal(data.begin(), data.begin() + count, lastChunk_.begin()))
      send("CP1 ACK " + std::to_string(sequence));
    else
      error("SEQUENCE");
    return;
  }
  if (sequence != nextSequence_) return error("SEQUENCE");
  if (count > length_ - position_) return error("LENGTH", true);
  size_t written = 0;
  if (!storage_.write(data.data(), count, written) || written != count) return error("IO", true);
  lastChunk_ = data;
  lastChunkLength_ = count;
  lastChunkValid_ = true;
  position_ += static_cast<uint32_t>(count);
  ++nextSequence_;
  send("CP1 ACK " + std::to_string(sequence));
}

void UsbFileTransfer::commitUpload() {
  if (state_ == State::Verify || (state_ == State::Idle && terminal_.compare(0, 13, "CP1 COMPLETE ") == 0)) {
    send(status());
    return;
  }
  if (state_ != State::Upload) return error("STATE");
  if (position_ != length_) return error("LENGTH", true);
  if (!storage_.flush()) return error("IO", true);
  if (!closeHandle()) return error("IO", true);
  if (!storage_.openRead(temporary_)) return error("IO", true);
  handleOpen_ = true;
  uint32_t actual = 0;
  if (!storage_.size(actual)) return error("IO", true);
  if (actual != length_) return error("LENGTH", true);
  position_ = 0;
  storedCrc_ = 0xffffffffU;
  state_ = State::Verify;
  send(status());
}

void UsbFileTransfer::verifyStored() {
  std::array<uint8_t, kChunkBytes> data{};
  const size_t wanted = std::min<uint32_t>(kChunkBytes, length_ - position_);
  size_t received = 0;
  if (!storage_.read(data.data(), wanted, received) || received != wanted) return fail("IO");
  storedCrc_ = updateCrc(storedCrc_, data.data(), received);
  position_ += static_cast<uint32_t>(received);
  if (position_ != length_) return;
  uint32_t actual = 0;
  if (!storage_.size(actual)) return fail("IO");
  if (actual != length_) return fail("LENGTH");
  if ((storedCrc_ ^ 0xffffffffU) != expectedCrc_) return fail("CHECKSUM");
  if (!closeHandle()) return fail("IO");
  if (!storage_.renameNoReplace(temporary_, destination_)) return fail("IO");
  ownsTemporary_ = false;
  state_ = State::Idle;
  terminal_ = "CP1 COMPLETE " + std::to_string(length_) + " " + crcText(expectedCrc_);
}

void UsbFileTransfer::beginDownload(const std::array<std::string_view, 6>& fields) {
  std::string root;
  std::string path;
  if (!decodePath(fields[2], fields[3], root, path)) return error("PATH");
  if (active()) {
    if (state_ == State::Download && !position_ && path == destination_)
      send("CP1 READABLE " + std::to_string(length_));
    else
      error("BUSY");
    return;
  }
  if (!storage_.openRead(path)) return error("IO");
  handleOpen_ = true;
  if (!storage_.size(length_)) return error("IO", true);
  if (!length_ || length_ > kMaxFileBytes) return error("SIZE", true);
  state_ = State::Download;
  destination_ = path;
  position_ = nextSequence_ = 0;
  lastChunkValid_ = downloadEof_ = false;
  terminal_ = "CP1 IDLE";
  send("CP1 READABLE " + std::to_string(length_));
}

void UsbFileTransfer::readChunk(const std::array<std::string_view, 6>& fields) {
  if (state_ != State::Download) return error("STATE");
  uint32_t sequence = 0;
  if (!decimal(fields[2], sequence)) return error("SEQUENCE");
  if (lastChunkValid_ && sequence == nextSequence_ - 1) {
    send("CP1 CHUNK " + std::to_string(sequence) + " " + encode(lastChunk_.data(), lastChunkLength_));
    return;
  }
  if (sequence != nextSequence_) return error("SEQUENCE");
  if (position_ == length_) {
    uint8_t extra = 0;
    size_t received = 0;
    if (!storage_.read(&extra, 1, received)) return error("IO", true);
    if (received) return error("LENGTH", true);
    downloadEof_ = true;
    send("CP1 EOF " + std::to_string(sequence));
    return;
  }
  const size_t wanted = std::min<uint32_t>(kChunkBytes, length_ - position_);
  size_t received = 0;
  if (!storage_.read(lastChunk_.data(), wanted, received) || received != wanted) return error("IO", true);
  lastChunkLength_ = received;
  lastChunkValid_ = true;
  position_ += static_cast<uint32_t>(received);
  ++nextSequence_;
  send("CP1 CHUNK " + std::to_string(sequence) + " " + encode(lastChunk_.data(), received));
}

void UsbFileTransfer::tick(uint32_t nowMs) {
  now_ = nowMs;
  if (!active()) return;
  if (static_cast<uint32_t>(now_ - lastActivity_) >= kIdleTimeoutMs) {
    fail("TIMEOUT");
    return;
  }
  if (state_ == State::Verify) verifyStored();
}

}  // namespace usb_transfer
