#pragma once

#include <Arduino.h>
#include <HardwareSerial.h>
#if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
#include <HWCDC.h>
#endif

#include <string>

#ifndef LOG_LEVEL
#define LOG_LEVEL 0
#endif

#if defined(ARDUINO_USB_CDC_ON_BOOT) && ARDUINO_USB_CDC_ON_BOOT
static HWCDC& logSerial = Serial;
#define LOG_SERIAL_HAS_TX_TIMEOUT 1
#else
static HardwareSerial& logSerial = Serial;
#define LOG_SERIAL_HAS_TX_TIMEOUT 0
#endif

class SerialTxLock {
 public:
  explicit SerialTxLock(uint32_t timeoutMs);
  ~SerialTxLock();
  explicit operator bool() const { return locked_; }
  SerialTxLock(const SerialTxLock&) = delete;
  SerialTxLock& operator=(const SerialTxLock&) = delete;

 private:
  bool locked_;
};

// Caller must hold SerialTxLock.
size_t writeSerialTx(const uint8_t* data, size_t size, uint32_t timeoutMs);
void flushSerialTx();

void logPrintf(const char* level, const char* origin, const char* format, ...);

#ifdef ENABLE_SERIAL_LOG
#if LOG_LEVEL >= 0
#define LOG_ERR(origin, format, ...) logPrintf("ERR", origin, format "\n", ##__VA_ARGS__)
#else
#define LOG_ERR(origin, format, ...)
#endif

#if LOG_LEVEL >= 1
#define LOG_INF(origin, format, ...) logPrintf("INF", origin, format "\n", ##__VA_ARGS__)
#else
#define LOG_INF(origin, format, ...)
#endif

#if LOG_LEVEL >= 2
#define LOG_DBG(origin, format, ...) logPrintf("DBG", origin, format "\n", ##__VA_ARGS__)
#else
#define LOG_DBG(origin, format, ...)
#endif
#else
#define LOG_DBG(origin, format, ...)
#define LOG_ERR(origin, format, ...)
#define LOG_INF(origin, format, ...)
#endif

std::string getLastLogs();
void clearLastLogs();
bool sanitizeLogHead();

class MySerialImpl : public Print {
 public:
  void begin(unsigned long baud) { logSerial.begin(baud); }

  operator bool() const { return logSerial; }

  __attribute__((deprecated("Use LOG_* macro instead"))) size_t printf(const char* format, ...);
  size_t write(uint8_t b) override;
  size_t write(const uint8_t* buffer, size_t size) override;
  void flush() override;
  static MySerialImpl instance;
};

#ifdef Serial
#undef Serial
#endif
#define Serial MySerialImpl::instance
