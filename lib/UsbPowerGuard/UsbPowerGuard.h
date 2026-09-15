#pragma once

#include <cstdint>

enum class ExternalPowerState { Unknown, Connected, Disconnected };

#ifdef ARDUINO
ExternalPowerState externalPowerState();
#endif

class UsbPowerGuard {
 public:
  using PowerStateReader = ExternalPowerState (*)();
  static constexpr uint32_t POLL_INTERVAL_MS = 1000;

  explicit UsbPowerGuard(PowerStateReader reader) : reader_(reader) {}

  bool shouldStayAwake(uint32_t now, bool transferActive);
  bool consumeUnplugEvent();

 private:
  PowerStateReader reader_;
  ExternalPowerState state_ = ExternalPowerState::Unknown;
  uint32_t lastPollMs_ = 0;
  bool hasPolled_ = false;
  bool unplugEvent_ = false;
};
