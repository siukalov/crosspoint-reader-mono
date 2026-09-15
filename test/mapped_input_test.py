#!/usr/bin/env python3
"""Exercise frame-scoped taps through the actual MappedInputManager."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
STUBS = {
    "HalGPIO.h": r'''
#pragma once
#include <cstdint>
inline unsigned long hostNow = 1000;
inline unsigned long millis() { return hostNow; }
class HalGPIO {
 public:
  enum : uint8_t { BTN_BACK, BTN_CONFIRM, BTN_LEFT, BTN_RIGHT, BTN_UP, BTN_DOWN, BTN_POWER };
  bool pendingTap = false;
  bool tap = false;
  float tapX = 0.25f;
  float tapY = 0.5f;
  unsigned updates = 0;
  unsigned long heldMs = 123;
  int pressed = -1;
  int released = -1;
  void update() { ++updates; tap = pendingTap; pendingTap = false; }
  bool hasTouch() const { return true; }
  bool wasTouchTap(float& x, float& y) const { x = tapX; y = tapY; return tap; }
  bool wasTouchReleased() const { return tap; }
  bool wasTouchDown(float&, float&) const { return false; }
  bool isTouchTapCandidate(float&, float&, unsigned long&) const { return false; }
  bool isTouchHeldAt(float&, float&) const { return false; }
  bool wasSwipe(float&, float&, float&, float&) const { return false; }
  unsigned long lastTouchHeldMs() const { return heldMs; }
  bool wasPressed(uint8_t button) const { return pressed == button; }
  bool wasReleased(uint8_t button) const { return released == button; }
  bool isPressed(uint8_t button) const { return pressed == button; }
  bool wasAnyPressed() const { return pressed >= 0; }
  bool wasAnyReleased() const { return released >= 0; }
  unsigned long getHeldTime() const { return heldMs; }
};
''',
    "GfxRenderer.h": r'''
#pragma once
class GfxRenderer {
 public:
  enum Orientation { Portrait, PortraitInverted, LandscapeClockwise, LandscapeCounterClockwise };
  Orientation orientation = Portrait;
  int width = 480;
  int height = 800;
  Orientation getOrientation() const { return orientation; }
  int getScreenWidth() const { return width; }
  int getScreenHeight() const { return height; }
  void tapToLogical(float nx, float ny, int& x, int& y) const {
    x = static_cast<int>(nx * width);
    y = static_cast<int>(ny * height);
  }
};
''',
    "CrossPointSettings.h": r'''
#pragma once
#include <cstdint>
struct CrossPointSettings {
  enum SideButtonLayout { PREV_NEXT, NEXT_PREV, SIDE_BUTTONS_DISABLED };
  SideButtonLayout sideButtonLayout = PREV_NEXT;
  bool frontButtonFollowOrientation = false;
  uint8_t frontButtonBack = 0;
  uint8_t frontButtonConfirm = 1;
  uint8_t frontButtonLeft = 2;
  uint8_t frontButtonRight = 3;
};
inline CrossPointSettings SETTINGS;
''',
    "components/UITheme.h": r'''
#pragma once
class UITheme {
 public:
  static UITheme& getInstance() { static UITheme theme; return theme; }
  const UITheme& getTheme() const { return *this; }
  int getListRowStep(bool subtitle) const { return subtitle ? 80 : 60; }
  int getListPageItems(int height, bool subtitle) const { return height / getListRowStep(subtitle); }
};
''',
}

HARNESS = r'''
#include "MappedInputManager.h"
#include <GfxRenderer.h>
#include <CrossPointSettings.h>
#include <cstdlib>
#include <iostream>
#include <string>

void require(bool value, const char* message) {
  if (!value) { std::cerr << message << '\n'; std::exit(1); }
}

int main(int argc, char** argv) {
  require(argc == 2, "scenario required");
  const std::string scenario(argv[1]);
  HalGPIO gpio;
  GfxRenderer renderer;
  MappedInputManager input(gpio, renderer);
  int x = -1;
  int y = -1;
  input.update();
  if (scenario == "same_frame") {
    input.injectDebugTap(151, 503);
    require(input.wasScreenTapped(x, y) && x == 151 && y == 503,
            "first query: expected tap at (151,503)");
    x = y = -1;
    require(input.wasScreenTapped(x, y) && x == 151 && y == 503,
            "second query: expected tap at (151,503), got no tap or changed coordinates");
    require(input.wasScreenTapped(x, y) && x == 151 && y == 503,
            "third query: expected unchanged tap at (151,503)");
  } else if (scenario == "tab_then_list") {
    input.injectDebugTap(151, 503);
    require(!input.wasTapInRect(0, 60, 480, 60), "tab rectangle must miss the row tap");
    require(input.wasTapInRect(100, 480, 280, 60), "list rectangle after tab miss must receive the same tap");
    int row = -1;
    require(input.wasListItemTapped(row, 10, 0, 200, 480, false) && row == 5,
            "list query after tab miss: expected row 5");
    require(input.wasScreenTapped(x, y) && x == 151 && y == 503,
            "list query must preserve literal tap coordinates");
  } else if (scenario == "expiry") {
    input.injectDebugTap(151, 503);
    require(input.wasScreenTapped(x, y), "injected tap must be visible in its frame");
    input.update();
    require(!input.wasScreenTapped(x, y), "next frame: expected no tap, got stale injected tap");
    input.update();
    require(!input.wasScreenTapped(x, y), "second later frame must contain no tap");
    input.injectDebugTap(240, 360);
    require(input.wasScreenTapped(x, y) && x == 240 && y == 360,
            "fresh injection after two updates: expected (240,360)");
    require(gpio.updates == 3, "every mapper update must sample hardware exactly once");
  } else if (scenario == "unqueried_expiry") {
    input.injectDebugTap(151, 503);
    input.update();
    require(!input.wasScreenTapped(x, y), "unqueried tap must expire when activity dispatch is skipped");
  } else if (scenario == "clamps") {
    input.injectDebugTap(-1, -20);
    require(input.wasScreenTapped(x, y) && x == 0 && y == 0, "negative coordinates must clamp to (0,0)");
    input.update();
    input.injectDebugTap(480, 800);
    require(input.wasScreenTapped(x, y) && x == 479 && y == 799, "portrait coordinates must clamp to (479,799)");
    input.update();
    renderer.width = 800;
    renderer.height = 480;
    input.injectDebugTap(1000, 1000);
    require(input.wasScreenTapped(x, y) && x == 799 && y == 479, "clamps must follow current renderer geometry");
  } else if (scenario == "held_time") {
    input.injectDebugTap(151, 503);
    require(input.wasScreenTapped(x, y), "held override requires a tap query");
    require(input.getHeldTime() == 40, "debug tap held time must remain 40 ms");
    hostNow += 250;
    require(input.getHeldTime() == 40, "held override must remain valid through 250 ms");
    ++hostNow;
    require(input.getHeldTime() == 123, "held override must expire after 250 ms");
    input.wasScreenTapped(x, y);
    gpio.pressed = HalGPIO::BTN_CONFIRM;
    require(input.getHeldTime() == 123, "physical button edges must take precedence over tap held time");
  } else if (scenario == "native") {
    gpio.pendingTap = true;
    input.update();
    require(input.wasScreenTapped(x, y) && x == 120 && y == 400, "native tap must map to (120,400)");
    require(input.wasScreenTapped(x, y) && x == 120 && y == 400, "native tap must survive repeated frame queries");
    require(input.getHeldTime() == 123, "native tap must preserve measured held time");
    input.update();
    require(!input.wasScreenTapped(x, y), "native tap must expire at hardware sampling");
    input.suppressTouchTapOnce();
    gpio.pendingTap = true;
    input.update();
    require(!input.wasScreenTapped(x, y), "suppressed native release must remain suppressed");
    gpio.pendingTap = true;
    input.update();
    require(input.wasScreenTapped(x, y), "suppression must not swallow the next native contact");
    SETTINGS.frontButtonConfirm = HalGPIO::BTN_RIGHT;
    gpio.pressed = HalGPIO::BTN_RIGHT;
    require(input.wasPressed(MappedInputManager::Button::Confirm), "front button remapping must remain active");
    SETTINGS.sideButtonLayout = CrossPointSettings::NEXT_PREV;
    gpio.pressed = HalGPIO::BTN_DOWN;
    require(input.wasPressed(MappedInputManager::Button::PageBack), "side button remapping must remain active");
  } else {
    require(false, "unknown scenario");
  }
}
'''


class MappedInputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mapped-input-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        for name, content in STUBS.items():
            path = cls.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        cls.executable = cls.build("production")

    @classmethod
    def build(cls, name, header=None, source=None):
        directory = cls.directory / name
        directory.mkdir()
        for filename, replacement in (("MappedInputManager.h", header), ("MappedInputManager.cpp", source)):
            if replacement is None:
                shutil.copyfile(ROOT / "src" / filename, directory / filename)
            else:
                (directory / filename).write_text(replacement)
        harness = directory / "test.cpp"
        harness.write_text(HARNESS)
        executable = directory / "test"
        result = subprocess.run(
            [os.environ.get("CXX", "c++"), "-std=c++20", "-Wall", "-Wextra", "-pedantic",
             "-I", str(cls.directory), "-I", str(directory), str(harness),
             str(directory / "MappedInputManager.cpp"), "-o", str(executable)],
            capture_output=True, text=True,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return executable

    def run_scenario(self, scenario, executable=None):
        return subprocess.run([str(executable or self.executable), scenario], capture_output=True, text=True)

    def test_same_frame_queries(self):
        result = self.run_scenario("same_frame")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frame_lifetime_and_native_controls(self):
        for scenario in ("tab_then_list", "expiry", "unqueried_expiry", "clamps", "held_time", "native"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_consume_on_query_mutant(self):
        source = (ROOT / "src/MappedInputManager.cpp").read_text()
        target = "    y = debugTapY;\n"
        self.assertEqual(source.count(target), 1)
        mutant = self.build("consume-on-query", source=source.replace(target, target + "    debugTapPending = false;\n"))
        result = self.run_scenario("same_frame", mutant)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("second query: expected tap at (151,503), got no tap or changed coordinates", result.stderr)

    def test_missing_expiry_mutant(self):
        header = (ROOT / "src/MappedInputManager.h").read_text()
        target = "    debugTapPending = false;\n"
        self.assertEqual(header.count(target), 1)
        mutant = self.build("missing-expiry", header=header.replace(target, ""))
        result = self.run_scenario("expiry", mutant)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("next frame: expected no tap, got stale injected tap", result.stderr)

    def test_production_loop_order(self):
        main = (ROOT / "src/main.cpp").read_text()
        loop = main[main.index("\nvoid loop() {"):]
        sampling = loop.index("mappedInputManager.update();")
        dispatch = loop.index("activityManager.loop();")
        injections = list(re.finditer(r"mappedInputManager\.injectDebugTap\(", loop))
        self.assertEqual(len(injections), 2)
        for injection in injections:
            self.assertLess(sampling, injection.start())
            self.assertLess(injection.start(), dispatch)


if __name__ == "__main__":
    unittest.main()
