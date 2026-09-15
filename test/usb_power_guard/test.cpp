#include <gtest/gtest.h>

#include "UsbPowerGuard.h"

#if defined(ARDUINO) && FREEINK_DEVICE_PAPERMONO
#include <BatteryMonitor.h>

namespace {
BatteryMonitor::Status batteryStatus;
}

BatteryMonitor::BatteryMonitor() : _adcPin(PIN_NONE), _dividerMultiplier(2.0f), _chargeStatusPin(PIN_NONE) {}

BatteryMonitor::Status BatteryMonitor::readStatus() const { return batteryStatus; }
#endif

namespace {

ExternalPowerState sampleState;
unsigned int sampleCount;

ExternalPowerState readPowerState() {
  ++sampleCount;
  return sampleState;
}

class UsbPowerGuardTest : public testing::Test {
 protected:
  UsbPowerGuard guard{readPowerState};

  void SetUp() override {
    sampleState = ExternalPowerState::Unknown;
    sampleCount = 0;
  }
};

TEST_F(UsbPowerGuardTest, StartupUnknownKeepsAwake) {
  EXPECT_TRUE(guard.shouldStayAwake(0, false));
  EXPECT_FALSE(guard.consumeUnplugEvent());
  EXPECT_EQ(sampleCount, 1u);
}

TEST_F(UsbPowerGuardTest, ExternalPowerKeepsAwakePastIdleTimeout) {
  sampleState = ExternalPowerState::Connected;
  EXPECT_TRUE(guard.shouldStayAwake(0, false));
  EXPECT_TRUE(guard.shouldStayAwake(900000, false));
  EXPECT_FALSE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, BatteryOnlyControlAllowsAutomaticSleep) {
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_FALSE(guard.shouldStayAwake(0, false));
  EXPECT_TRUE(guard.consumeUnplugEvent());
  EXPECT_FALSE(guard.shouldStayAwake(900000, false));
  EXPECT_FALSE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, PollingIsCachedAtFaceDownAndIdleBoundary) {
  sampleState = ExternalPowerState::Connected;
  EXPECT_TRUE(guard.shouldStayAwake(0, false));
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_TRUE(guard.shouldStayAwake(999, false));
  EXPECT_TRUE(guard.shouldStayAwake(999, false));
  EXPECT_EQ(sampleCount, 1u);
  EXPECT_FALSE(guard.shouldStayAwake(1000, false));
  EXPECT_FALSE(guard.shouldStayAwake(1000, false));
  EXPECT_EQ(sampleCount, 2u);
}

TEST_F(UsbPowerGuardTest, UnplugResetsElapsedIdleTimeBeforeSleepIsAllowed) {
  sampleState = ExternalPowerState::Connected;
  ASSERT_TRUE(guard.shouldStayAwake(0, false));
  uint32_t lastActivityTime = 0;

  sampleState = ExternalPowerState::Disconnected;
  const uint32_t unplugTime = 900000;
  const bool stayAwake = guard.shouldStayAwake(unplugTime, false);
  if (guard.consumeUnplugEvent()) lastActivityTime = unplugTime;
  EXPECT_FALSE(stayAwake);
  EXPECT_EQ(lastActivityTime, 900000u);
  EXPECT_FALSE(!stayAwake && unplugTime - lastActivityTime >= 60000u);
  EXPECT_TRUE(!guard.shouldStayAwake(960000, false) && 960000u - lastActivityTime >= 60000u);
  EXPECT_FALSE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, FirstKnownBatterySampleAfterStartupResetsIdleTime) {
  EXPECT_TRUE(guard.shouldStayAwake(0, false));
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_FALSE(guard.shouldStayAwake(300000, false));
  EXPECT_TRUE(guard.consumeUnplugEvent());
  EXPECT_FALSE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, SingleFailedReadKeepsAwakeUntilConfirmedUnplug) {
  sampleState = ExternalPowerState::Connected;
  EXPECT_TRUE(guard.shouldStayAwake(0, false));
  sampleState = ExternalPowerState::Unknown;
  EXPECT_TRUE(guard.shouldStayAwake(1000, false));
  EXPECT_FALSE(guard.consumeUnplugEvent());
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_FALSE(guard.shouldStayAwake(2000, false));
  EXPECT_TRUE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, FailedReadOnBatteryAlsoKeepsAwake) {
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_FALSE(guard.shouldStayAwake(0, false));
  ASSERT_TRUE(guard.consumeUnplugEvent());
  sampleState = ExternalPowerState::Unknown;
  EXPECT_TRUE(guard.shouldStayAwake(1000, false));
  EXPECT_FALSE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, RepeatedFailedReadsStayAwakeWithoutUnplugEvents) {
  sampleState = ExternalPowerState::Connected;
  EXPECT_TRUE(guard.shouldStayAwake(0, false));
  sampleState = ExternalPowerState::Unknown;
  EXPECT_TRUE(guard.shouldStayAwake(1000, false));
  EXPECT_TRUE(guard.shouldStayAwake(2000, false));
  EXPECT_TRUE(guard.shouldStayAwake(300000, false));
  EXPECT_FALSE(guard.consumeUnplugEvent());
  EXPECT_EQ(sampleCount, 4u);
}

TEST_F(UsbPowerGuardTest, PollIntervalSurvivesMillisWraparound) {
  sampleState = ExternalPowerState::Connected;
  EXPECT_TRUE(guard.shouldStayAwake(0xFFFFFF00u, false));
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_TRUE(guard.shouldStayAwake(743, false));
  EXPECT_EQ(sampleCount, 1u);
  EXPECT_FALSE(guard.shouldStayAwake(744, false));
  EXPECT_EQ(sampleCount, 2u);
  EXPECT_TRUE(guard.consumeUnplugEvent());
}

TEST_F(UsbPowerGuardTest, ActiveTransferSuppressesAutomaticSleepOnBattery) {
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_TRUE(guard.shouldStayAwake(0, true));
  EXPECT_TRUE(guard.shouldStayAwake(900000, true));
}

TEST_F(UsbPowerGuardTest, TransferCompletionOrAbortReleasesHoldWithoutWaitingForPoll) {
  sampleState = ExternalPowerState::Disconnected;
  EXPECT_TRUE(guard.shouldStayAwake(0, true));
  EXPECT_FALSE(guard.shouldStayAwake(1, false));
  EXPECT_EQ(sampleCount, 1u);
}

TEST_F(UsbPowerGuardTest, TransferEndRetainsExternalPowerHold) {
  sampleState = ExternalPowerState::Connected;
  EXPECT_TRUE(guard.shouldStayAwake(0, true));
  EXPECT_TRUE(guard.shouldStayAwake(1, false));
}

#if defined(ARDUINO) && FREEINK_DEVICE_PAPERMONO
TEST(UsbPowerGuardAdapterTest, FullBatteryExternalPowerKeepsAwakeWithoutActiveCharging) {
  batteryStatus = {};
  batteryStatus.percentageKnown = true;
  batteryStatus.percentage = 100;
  batteryStatus.chargingKnown = true;
  batteryStatus.charging = false;
  batteryStatus.externalPowerKnown = true;
  batteryStatus.externalPower = true;
  UsbPowerGuard guard{externalPowerState};
  EXPECT_EQ(externalPowerState(), ExternalPowerState::Connected);
  EXPECT_TRUE(guard.shouldStayAwake(900000, false));
}

TEST(UsbPowerGuardAdapterTest, UnknownPowerRegisterIsUnknownEvenWhenOtherTelemetryIsValid) {
  batteryStatus = {};
  batteryStatus.millivoltsKnown = true;
  batteryStatus.millivolts = 4200;
  batteryStatus.chargingKnown = true;
  batteryStatus.charging = true;
  EXPECT_EQ(externalPowerState(), ExternalPowerState::Unknown);
}

TEST(UsbPowerGuardAdapterTest, KnownBatterySupplyIsDisconnectedEvenWithStaleCharging) {
  batteryStatus = {};
  batteryStatus.externalPowerKnown = true;
  batteryStatus.externalPower = false;
  batteryStatus.chargingKnown = true;
  batteryStatus.charging = true;
  EXPECT_EQ(externalPowerState(), ExternalPowerState::Disconnected);
}
#endif

}  // namespace
