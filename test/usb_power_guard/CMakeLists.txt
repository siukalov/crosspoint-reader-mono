add_executable(UsbPowerGuardTest
  test.cpp
  ${REPO_ROOT}/lib/UsbPowerGuard/UsbPowerGuard.cpp
)

target_include_directories(UsbPowerGuardTest PRIVATE
  ${REPO_ROOT}/lib/UsbPowerGuard
  ${REPO_ROOT}/freeink-sdk/libs/hardware/BatteryMonitor/include
)

target_compile_definitions(UsbPowerGuardTest PRIVATE
  ARDUINO=1
  FREEINK_DEVICE_PAPERMONO=1
)

target_link_libraries(UsbPowerGuardTest PRIVATE
  crosspoint_test_common
  GTest::gtest_main
)

gtest_discover_tests(UsbPowerGuardTest)
