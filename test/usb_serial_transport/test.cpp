#include <UsbSerialTransport.h>

#include <cstdlib>
#include <iostream>
#include <limits>
#include <string>

using usb_transfer::SerialLineBuffer;
using usb_transfer::writeSerialChunks;

static void check(bool condition, const char* message) {
  if (!condition) {
    std::cerr << "FAIL: " << message << '\n';
    std::exit(1);
  }
}

static void pending(SerialLineBuffer& buffer, const std::string& text) {
  for (unsigned char byte : text) {
    check(buffer.push(byte) == SerialLineBuffer::Result::Pending, "line completes only at newline");
  }
}

struct Writer {
  std::string output;
  uint32_t time = 0;
  size_t limit = 7;
  size_t calls = 0;
  size_t flushes = 0;
  size_t largest = 0;
  bool overreports = false;

  uint32_t now() { return time++; }
  size_t write(const uint8_t* data, size_t size) {
    ++calls;
    if (size > largest) largest = size;
    if (overreports) return size + 1;
    const size_t count = size < limit ? size : limit;
    output.append(reinterpret_cast<const char*>(data), count);
    return count;
  }
  void flush() { ++flushes; }
  void idle() { ++time; }
};

static void framing() {
  SerialLineBuffer buffer;
  pending(buffer, "CP1 STATUS\r");
  check(buffer.push('\n') == SerialLineBuffer::Result::Complete, "CRLF completes a valid line");
  check(std::string(buffer.line()) == "CP1 STATUS", "CRLF removes only trailing carriage return");

  pending(buffer, std::string(1100, 'a'));
  check(buffer.push('\n') == SerialLineBuffer::Result::Complete, "1100-byte line is accepted");
  check(std::string(buffer.line()) == std::string(1100, 'a'), "maximum line retains every byte");

  pending(buffer, std::string(1101, 'a') + "CMD:SLEEP");
  check(buffer.push('\n') == SerialLineBuffer::Result::Malformed, "1101-byte line and command suffix are rejected");
  pending(buffer, "CP1 STATUS");
  check(buffer.push('\n') == SerialLineBuffer::Result::Complete, "parser recovers at next line after overflow");
  check(std::string(buffer.line()) == "CP1 STATUS", "recovered line excludes discarded bytes");

  for (const std::string& invalid : {std::string("\0x", 2), std::string("a\tb"), std::string("a\rb"),
                                     std::string(1, static_cast<char>(127)), std::string(1, static_cast<char>(255))}) {
    pending(buffer, invalid);
    check(buffer.push('\n') == SerialLineBuffer::Result::Malformed, "invalid control or non-ASCII byte is rejected");
    pending(buffer, "CP1 ABORT");
    check(buffer.push('\n') == SerialLineBuffer::Result::Complete, "parser recovers after malformed input");
    check(std::string(buffer.line()) == "CP1 ABORT", "valid command survives recovery exactly");
  }
  check(buffer.push('\n') == SerialLineBuffer::Result::Complete, "empty line completes");
  check(std::string(buffer.line()).empty(), "empty line contains no stale bytes");
}

static void writes() {
  std::string payload;
  for (size_t index = 0; index < 1030; ++index) payload.push_back(static_cast<char>(index % 256));
  const auto* data = reinterpret_cast<const uint8_t*>(payload.data());
  Writer writer;
  check(writeSerialChunks(writer, data, payload.size(), 1000) == payload.size(), "short writes complete full payload");
  check(writer.output == payload, "short writes preserve byte content and order");
  check(writer.largest == 64, "USB write request is bounded to 64 bytes");
  check(writer.calls == 148, "seven-byte writer advances by actual write count");
  check(writer.flushes == writer.calls, "every USB write is flushed");

  Writer stalled;
  stalled.limit = 0;
  check(writeSerialChunks(stalled, data, payload.size(), 20) == 0, "zero writes send no bytes");
  check(stalled.calls == 10 && stalled.time == 22, "zero writes stop at timeout without spinning forever");
  check(stalled.flushes == stalled.calls, "stalled writes still flush");

  Writer invalid;
  invalid.overreports = true;
  check(writeSerialChunks(invalid, data, payload.size(), 20) == 0, "overreported write count is rejected");
  check(invalid.calls == 1, "invalid writer is not retried");

  Writer wrapped;
  wrapped.time = std::numeric_limits<uint32_t>::max() - 2;
  check(writeSerialChunks(wrapped, data, 21, 10) == 21, "timeout remains valid across clock wrap");
  check(wrapped.output == payload.substr(0, 21), "clock wrap preserves payload");

  Writer empty;
  check(writeSerialChunks(empty, data, 0, 20) == 0, "empty payload sends zero bytes");
  check(empty.calls == 0 && empty.flushes == 0, "empty payload does not touch USB");
}

int main() {
  framing();
  writes();
  std::cout << "PASS: production serial framing, recovery, short writes, timeout and clock wrap\n";
}
