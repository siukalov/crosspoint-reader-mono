#!/usr/bin/env python3
"""Exercise gray transition USB dispatch through the real diagnostic and driver."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import panel_diagnostic_test as base

sys.path.insert(0, str(base.ROOT / "tools"))
import papermono_gray_check as checker


DISPATCH_STUBS = r'''
#include <string>
#include <iostream>
#include <MinizConfig.h>
struct Transport {
  std::vector<std::string> lines;
  void sendLine(const std::string& line) { lines.push_back(line); }
} usbTransport;
struct Transfer {
  bool active() const { return false; }
  void pollLine(const std::string&) {}
} usbTransfer;
struct RenderLock {};
bool diagnosticActive = false;
bool acquireSerialSession() { return true; }
void releaseSerialSessionIfIdle() {}
'''

CASES = r'''
extern "C" void freeink_panel_capture_activation(uint8_t,uint32_t,uint32_t);
extern "C" void freeink_panel_capture_completion(uint8_t,uint32_t,uint32_t,bool);
bool injectOverflow = false;
void hostAfterDisplay() {
  if (!injectOverflow) return;
  for (unsigned i=0; i<PanelDiagnostic::MAX_ACTIVATIONS; ++i) {
    freeink_panel_capture_activation(0xFC, 9, 0);
    freeink_panel_capture_completion(0xFC, 9, 0, true);
  }
}
void checkActivation(unsigned index, uint8_t control, uint32_t bytes24, uint32_t crc24,
                     uint32_t bytes26, uint32_t crc26) {
  const auto& actual = PanelDiagnostic::transitionMetadata().activations[index];
  require(actual.control == control && actual.driverGeneration != 0, "ordered activation control and generation");
  require(actual.bytes24 == bytes24 && actual.crc24 == crc24 && actual.bytes26 == bytes26 && actual.crc26 == crc26,
          "per-activation lengths and independently specified CRC32");
  require(actual.completionSeen && actual.completed && actual.busyMs == (control == 0xFC ? 138u : 137u), "activation completed with measured wait");
}
void checkResult(unsigned count, bool commit) {
  const auto& meta = PanelDiagnostic::transitionMetadata();
  require(meta.valid && meta.completed && !meta.captureError && !meta.overflow, "valid completed transition capture");
  require(meta.activationCount == count && meta.committed == commit && meta.noActivation == (count == 0),
          "activation count, final logical commit and explicit no-activation result");
  require(!usbTransport.lines.empty() && usbTransport.lines[0].rfind("GRAYTRANS META ", 0) == 0 &&
          usbTransport.lines.size() == count + 2 && usbTransport.lines.back().rfind("GRAYTRANS END ", 0) == 0,
          "framed transition USB response");
}
void command(const char* value) { usbTransport.lines.clear(); resetRecording(); require(dispatchUsbCommand(value), "command handled"); }
int main(int argc, char** argv) {
  require(argc == 2, "missing scenario");
  const std::string scenario(argv[1]);
  const bool rotated = scenario != "native" && scenario != "cli_native";
  BoardConfig::ACTIVE.orientation.mirrorX = rotated;
  BoardConfig::ACTIVE.orientation.mirrorY = rotated;
  const uint32_t original = rotated ? 0xD74BB015 : 0x5B9FD439;
  const uint32_t inverted = rotated ? 0x39F79F14 : 0xB523FB38;
  const uint32_t changed = rotated ? 0xA04C8083 : 0xB0F6EF3A;
  freeink::ssd1683Driver().begin(hostBus);
  if (scenario.rfind("cli_",0)==0) {
    std::string input;
    while (std::getline(std::cin,input)) {
      usbTransport.lines.clear();
      require(dispatchUsbCommand(input),"CLI command dispatched");
      for (const auto& line : usbTransport.lines) std::cout << line << std::endl;
    }
    return 0;
  }
  command("CMD:GRAYTEST BW");
  require(usbTransport.lines == std::vector<std::string>{"GRAYTRANS ERROR NO_GRAY"}, "BW requires frozen gray fixture");
  command("CMD:GRAYTEST");
  const auto frozen = PanelDiagnostic::metadata();
  require(frozen.valid && frozen.crc24 == (rotated ? 0xE291674B : 0x33A561C2) && frozen.crc26 == inverted,
          "frozen gray fixture matches independent literal CRC32");
  const auto graySummary = PanelDiagnostic::transitionMetadata();
  require(graySummary.valid && graySummary.committed && graySummary.activationCount == 1, "gray completion metadata");
  checkActivation(0,0xD7,48000,rotated ? 0xE291674B : 0x33A561C2,48000,inverted);
  if (scenario == "fail_reset" || scenario == "fail_recovery1" || scenario == "fail_recovery2") {
    resetRecording();
    failWait = scenario == "fail_reset" ? 1 : scenario == "fail_recovery1" ? 3 : 4;
    usbTransport.lines.clear();
    require(dispatchUsbCommand("CMD:GRAYTEST BW"), "failed operation still dispatched");
    const auto& failure = PanelDiagnostic::transitionMetadata();
    const unsigned expected = scenario == "fail_reset" ? 0 : scenario == "fail_recovery1" ? 1 : 2;
    require(!failure.valid && !failure.committed && !failure.completed && !failure.captureError &&
            failure.activationCount == expected && failure.noActivation == (expected == 0),
            "failed readiness or activation cannot be a valid no-op or logical commit");
    if (expected) require(failure.activations[expected-1].completionSeen && !failure.activations[expected-1].completed,
                          "failed activation explicitly records incomplete BUSY");
    if (expected == 2) require(failure.activations[0].completed, "phase one completion does not commit recovery");
    require(PanelDiagnostic::metadata().generation == frozen.generation && PanelDiagnostic::metadata().valid,
            "failed transition preserves frozen gray capture");
    return 0;
  }
  if (scenario == "overflow") {
    injectOverflow = true;
    command("CMD:GRAYTEST BW");
    const auto& overflow = PanelDiagnostic::transitionMetadata();
    require(!overflow.valid && overflow.overflow && overflow.captureError && overflow.activationCount > PanelDiagnostic::MAX_ACTIVATIONS,
            "activation history overflow is explicitly invalid");
    require(usbTransport.lines.size() == PanelDiagnostic::MAX_ACTIVATIONS+2, "overflow response remains bounded and framed");
    return 0;
  }
  command("CMD:GRAYTEST BW");
  checkResult(2,true);
  checkActivation(0,0xF8,48000,inverted,0,0);
  checkActivation(1,0x14,48000,original,48000,original);
  require(controls == std::vector<uint8_t>({0xF8,0x14}), "real bus recovery controls");
  const auto recoveryGeneration = PanelDiagnostic::transitionMetadata().generation;
  require(PanelDiagnostic::transitionMetadata().renderMs == 388, "whole recovery time excludes no phases");
  command("CMD:GRAYTEST BWCHANGED");
  checkResult(1,true);
  checkActivation(0,0xFC,48000,changed,48000,original);
  require(mz_crc32(0,display.frame,48000) == 0xB0F6EF3A, "changed fixture toggles byte zero mask 80 exactly once");
  require(PanelDiagnostic::transitionMetadata().generation == recoveryGeneration+1, "new transition generation");
  command("CMD:GRAYTEST BWCHANGED");
  checkResult(0,false);
  require(events.empty(), "unchanged repeat has zero controller commands");
  require(PanelDiagnostic::transitionMetadata().generation == recoveryGeneration+2, "no-op retains a separate result");
  require(PanelDiagnostic::metadata().generation == frozen.generation && PanelDiagnostic::metadata().crc24 == frozen.crc24 &&
          PanelDiagnostic::metadata().activationCount == 1, "BW operations preserve frozen gray metadata");
  for (uint8_t ram : {0x24,0x26}) {
    std::array<uint8_t,48000> bytes;
    for (uint32_t offset=0; offset<48000; offset+=256) {
      const auto count=PanelDiagnostic::readCapture(ram,frozen.generation,offset,bytes.data()+offset,256);
      require(count == std::min<uint32_t>(256,48000-offset), "frozen readback remains complete after transitions");
    }
    require(mz_crc32(0,bytes.data(),48000) == (ram==0x24 ? frozen.crc24 : frozen.crc26), "frozen bytes isolated from BW captures");
  }
  command("CMD:GRAYCAP");
  require(usbTransport.lines[0].rfind("GRAYCAP META ",0)==0, "legacy gray metadata remains available");
  command("CMD:GRAYTEST END");
  require(!diagnosticActive && !PanelDiagnostic::metadata().valid, "END releases diagnostic session");
  command("CMD:GRAYTEST BWCHANGED");
  require(usbTransport.lines == std::vector<std::string>{"GRAYTRANS ERROR NO_GRAY"}, "END rejects further transition requests");
}
'''



class PanelTransitionTest(unittest.TestCase):
    def test_diagnostic_transition_commands(self):
        main = (base.ROOT / "src/main.cpp").read_text()
        dispatch = main[main.index("static void sendCaptureMetadata()") :]
        dispatch = dispatch[:dispatch.index("\n#endif")]
        source = base.HARNESS[:base.HARNESS.index("int main(")] + DISPATCH_STUBS + dispatch + CASES
        with tempfile.TemporaryDirectory(prefix="panel-transition-") as temporary:
            directory = Path(temporary)
            for name, content in base.STUBS.items():
                if name == "HalDisplay.h":
                    content = content.replace("inline freeink::EpdBus hostBus;", "inline freeink::EpdBus hostBus;\nvoid hostAfterDisplay();")
                    content = content.replace("freeink::RefreshMode::Fast, false);", "freeink::RefreshMode::Fast, false); hostAfterDisplay();")
                (directory / name).write_text(content)
            harness = directory / "test.cpp"
            harness.write_text(source)
            miniz = directory / "miniz.o"
            result = subprocess.run([os.environ.get("CC", "cc"), "-c",
                                     str(base.ROOT / "lib/miniz/src/miniz_impl.c"), "-o", str(miniz)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            executable = directory / "test"
            command = [os.environ.get("CXX", "c++"), "-std=c++17", "-DENABLE_SERIAL_LOG=1",
                       "-I", str(directory), "-I", str(base.DRIVER),
                       "-I", str(base.ROOT / "lib/PanelDiagnostic"), "-I", str(base.ROOT / "lib/miniz/src"),
                       str(harness), str(base.ROOT / "lib/PanelDiagnostic/PanelDiagnostic.cpp"),
                       str(base.DRIVER / "Ssd1683Driver.cpp"), str(miniz), "-o", str(executable)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for scenario in ("rotated", "native", "fail_reset", "fail_recovery1", "fail_recovery2", "overflow"):
                with self.subTest(scenario=scenario):
                    result = subprocess.run([str(executable), scenario], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
            for orientation in ("native", "rotated"):
                with self.subTest(cli=orientation):
                    output = directory / orientation
                    output.mkdir()
                    process = subprocess.Popen([str(executable), "cli_" + orientation], stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    class HostLink:
                        def request(self, command):
                            process.stdin.write(command + "\n")
                            process.stdin.flush()
                            return self.receive()

                        def receive(self):
                            line = process.stdout.readline()
                            if not line:
                                raise AssertionError("Host diagnostic process stopped")
                            return line.split()
                    try:
                        link = HostLink()
                        summary = checker.run_checks(link, output, orientation)
                        self.assertEqual(summary["status"], "PASS")
                        self.assertEqual(summary["transitions"][-1]["activation_count"], 0)
                        self.assertEqual((output / "gray-24.bin").stat().st_size, 48000)
                        self.assertEqual(link.request("CMD:GRAYTEST END"), ["GRAYTEST", "END"])
                        corrupted = dict(summary["transitions"][1], committed=0)
                        with self.assertRaisesRegex(checker.ProtocolError, "final commit mismatch"):
                            expected = checker.FIXTURE_CRCS[orientation]
                            checker.check_transition(corrupted, [(0xFC,48000,expected["changed"],48000,expected["bw"])], "BWCHANGED")
                    finally:
                        process.stdin.close()
                        process.wait(timeout=5)
                        stderr = process.stderr.read()
                        process.stdout.close()
                        process.stderr.close()
                    self.assertEqual(process.returncode, 0, stderr)


if __name__ == "__main__":
    unittest.main()
