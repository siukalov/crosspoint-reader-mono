#include "UsbPowerGuard.h"

#if defined(ARDUINO) && FREEINK_DEVICE_PAPERMONO
#include <BatteryMonitor.h>
#endif

bool UsbPowerGuard::shouldStayAwake(uint32_t now, bool transferActive) {
  if (!hasPolled_ || static_cast<uint32_t>(now - lastPollMs_) >= POLL_INTERVAL_MS) {
    const ExternalPowerState nextState = reader_ ? reader_() : ExternalPowerState::Unknown;
    if (nextState == ExternalPowerState::Disconnected && state_ != ExternalPowerState::Disconnected) {
      unplugEvent_ = true;
    }
    state_ = nextState;
    lastPollMs_ = now;
    hasPolled_ = true;
  }
  return transferActive || state_ != ExternalPowerState::Disconnected;
}

bool UsbPowerGuard::consumeUnplugEvent() {
  const bool unplugged = unplugEvent_;
  unplugEvent_ = false;
  return unplugged;
}

#ifdef ARDUINO
ExternalPowerState externalPowerState() {
#if FREEINK_DEVICE_PAPERMONO
  static const BatteryMonitor battery;
  const BatteryMonitor::Status status = battery.readStatus();
  if (!status.externalPowerKnown) return ExternalPowerState::Unknown;
  return status.externalPower ? ExternalPowerState::Connected : ExternalPowerState::Disconnected;
#else
  return ExternalPowerState::Disconnected;
#endif
}
#endif
