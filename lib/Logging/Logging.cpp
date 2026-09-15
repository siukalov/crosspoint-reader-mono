#include "Logging.h"

#include <BoardConfig.h>
#include <UsbSerialTransport.h>
#include <esp_rom_sys.h>
#include <freertos/semphr.h>
#if LOG_SERIAL_HAS_TX_TIMEOUT
#include <hal/usb_serial_jtag_ll.h>
#endif

#include <string>

#define MAX_ENTRY_LEN 256
#define MAX_LOG_LINES 16

RTC_NOINIT_ATTR char logMessages[MAX_LOG_LINES][MAX_ENTRY_LEN];
RTC_NOINIT_ATTR size_t logHead = 0;
RTC_NOINIT_ATTR uint32_t rtcLogMagic;
static constexpr uint32_t LOG_RTC_MAGIC = 0xDEADBEEF;

void addToLogRingBuffer(const char* message) {
  if (rtcLogMagic != LOG_RTC_MAGIC || logHead >= MAX_LOG_LINES) {
    memset(logMessages, 0, sizeof(logMessages));
    logHead = 0;
    rtcLogMagic = LOG_RTC_MAGIC;
  }
  strncpy(logMessages[logHead], message, MAX_ENTRY_LEN - 1);
  logMessages[logHead][MAX_ENTRY_LEN - 1] = '\0';
  logHead = (logHead + 1) % MAX_LOG_LINES;
}

void logPrintf(const char* level, const char* origin, const char* format, ...) {
  va_list args;
  va_start(args, format);
  char buf[MAX_ENTRY_LEN];
  char* c = buf;
  {
    unsigned long ms = millis();
    int len = snprintf(c, sizeof(buf), "[%lu] [%s] [%s] ", ms, level, origin);
    if (len < 0) {
      va_end(args);
      return;
    }
    c += std::min(len, MAX_ENTRY_LEN - 1);
  }
  {
    int len = vsnprintf(c, sizeof(buf) - (c - buf), format, args);
    if (len < 0) {
      va_end(args);
      return;
    }
  }
  va_end(args);
  const size_t length = strnlen(buf, sizeof(buf));
  if (length == sizeof(buf) - 1) buf[length - 1] = '\n';
  SerialTxLock lock(25);
  if (lock) {
#if FREEINK_LOG_TRANSPORT == FREEINK_LOG_TRANSPORT_ROM_PRINTF
    esp_rom_printf("%s", buf);
#else
    if (logSerial) writeSerialTx(reinterpret_cast<const uint8_t*>(buf), length, 25);
#endif
  }
  addToLogRingBuffer(buf);
}

std::string getLastLogs() {
  if (rtcLogMagic != LOG_RTC_MAGIC) {
    return {};
  }
  std::string output;
  for (size_t i = 0; i < MAX_LOG_LINES; i++) {
    size_t idx = (logHead + i) % MAX_LOG_LINES;
    if (logMessages[idx][0] != '\0') {
      const size_t len = strnlen(logMessages[idx], MAX_ENTRY_LEN);
      output.append(logMessages[idx], len);
    }
  }
  return output;
}

bool sanitizeLogHead() {
  if (rtcLogMagic != LOG_RTC_MAGIC || logHead >= MAX_LOG_LINES) {
    logHead = 0;
    return true;
  }
  return false;
}

void clearLastLogs() {
  for (size_t i = 0; i < MAX_LOG_LINES; i++) {
    logMessages[i][0] = '\0';
  }
  logHead = 0;
  rtcLogMagic = LOG_RTC_MAGIC;
}

namespace {
SemaphoreHandle_t serialTxMutex = xSemaphoreCreateMutex();

struct SerialWriter {
  uint32_t now() const { return millis(); }
  size_t write(const uint8_t* data, size_t size) { return logSerial ? logSerial.write(data, size) : 0; }
  void flush() { flushSerialTx(); }
  void idle() { delay(1); }
};
}  // namespace

SerialTxLock::SerialTxLock(uint32_t timeoutMs)
    : locked_(serialTxMutex && xSemaphoreTake(serialTxMutex, pdMS_TO_TICKS(timeoutMs)) == pdTRUE) {}

SerialTxLock::~SerialTxLock() {
  if (locked_) xSemaphoreGive(serialTxMutex);
}

size_t writeSerialTx(const uint8_t* data, size_t size, uint32_t timeoutMs) {
  SerialWriter writer;
  return usb_transfer::writeSerialChunks(writer, data, size, timeoutMs);
}

void flushSerialTx() {
#if LOG_SERIAL_HAS_TX_TIMEOUT
  usb_serial_jtag_ll_txfifo_flush();
#else
  logSerial.flush();
#endif
}

MySerialImpl MySerialImpl::instance;

size_t MySerialImpl::printf(const char* format, ...) {
  char buffer[MAX_ENTRY_LEN];
  va_list args;
  va_start(args, format);
  const int length = vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  return length < 0 ? 0 : write(reinterpret_cast<const uint8_t*>(buffer), strnlen(buffer, sizeof(buffer)));
}

size_t MySerialImpl::write(uint8_t byte) { return write(&byte, 1); }

size_t MySerialImpl::write(const uint8_t* buffer, size_t size) {
  SerialTxLock lock(25);
  return lock ? writeSerialTx(buffer, size, 25) : 0;
}

void MySerialImpl::flush() {
  SerialTxLock lock(25);
  if (lock) flushSerialTx();
}
