#!/usr/bin/env python3
"""Capture and verify Paper Mono gray/BW transport evidence; no optical assessment."""

import argparse
import json
from pathlib import Path
import sys
import time
import zlib

from papermono_usb import Link, ProtocolError

PLANE_BYTES = 48000
MAX_ACTIVATIONS = 4
# BWCHANGED toggles logical pixel (0, 0): framebuffer byte 0 XOR 0x80.
FIXTURE_CRCS = {
    "rotated": {"gray24": 0xE291674B, "gray26": 0x39F79F14, "bw": 0xD74BB015,
                "inverted": 0x39F79F14, "changed": 0xA04C8083},
    "native": {"gray24": 0x33A561C2, "gray26": 0xB523FB38, "bw": 0x5B9FD439,
               "inverted": 0xB523FB38, "changed": 0xB0F6EF3A},
}


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def decode_metadata(fields):
    require(len(fields) == 14 and fields[:2] == ["GRAYCAP", "META"], "Malformed gray metadata")
    names = ("generation", "driver_generation", "width", "height", "plane_bytes", "crc24", "crc26",
             "render_ms", "busy_ms", "activation_count", "control", "valid")
    return {name: int(value, 16 if name.startswith("crc") else 10) for name, value in zip(names, fields[2:])}


def decode_transition(lines):
    fields = lines[0]
    require(len(fields) == 12 and fields[:2] == ["GRAYTRANS", "META"], "Malformed transition metadata")
    names = ("generation", "target", "render_ms", "activation_count", "committed", "completed",
             "no_activation", "capture_error", "overflow", "valid")
    result = {name: value if name == "target" else int(value) for name, value in zip(names, fields[2:])}
    count = result["activation_count"]
    require(0 <= count <= MAX_ACTIVATIONS and len(lines) == count + 2, "Unbounded or incomplete activation history")
    require(lines[-1] == ["GRAYTRANS", "END", str(result["generation"])], "Transition END generation mismatch")
    names = ("generation", "index", "control", "driver_generation", "bytes24", "crc24", "bytes26", "crc26",
             "busy_ms", "completion_seen", "completed")
    result["activations"] = []
    for index, fields in enumerate(lines[1:-1]):
        require(len(fields) == 13 and fields[:2] == ["GRAYTRANS", "ACT"], "Malformed activation metadata")
        activation = {name: int(value, 16 if name.startswith("crc") or name == "control" else 10)
                      for name, value in zip(names, fields[2:])}
        require(activation["generation"] == result["generation"] and activation["index"] == index,
                "Activation order or generation mismatch")
        result["activations"].append(activation)
    return result


def check_transition(result, expected, target):
    require(result["target"] == target and result["valid"] == 1 and result["completed"] == 1 and
            result["capture_error"] == 0 and result["overflow"] == 0, "Transition failed or capture invalid")
    require(result["activation_count"] == len(expected) and result["no_activation"] == int(not expected) and
            result["committed"] == int(bool(expected)), "Activation count, no-op or final commit mismatch")
    for actual, (control, bytes24, crc24, bytes26, crc26) in zip(result["activations"], expected):
        require((actual["control"], actual["bytes24"], actual["crc24"], actual["bytes26"], actual["crc26"]) ==
                (control, bytes24, crc24, bytes26, crc26), "Activation control or literal plane CRC/length mismatch")
        require(actual["completion_seen"] == 1 and actual["completed"] == 1 and actual["driver_generation"] > 0 and
                0 <= actual["busy_ms"] <= result["render_ms"], "Activation completion or wait measurement invalid")
    require(sum(a["busy_ms"] for a in result["activations"]) <= result["render_ms"], "Wait total exceeds render time")


class GrayLink(Link):
    def __init__(self, serial_port, transcript, timeout):
        super().__init__(serial_port, timeout=timeout, retries=0)
        self.transcript = transcript

    def send(self, command):
        self.transcript.write("> " + command + "\n")
        self.transcript.flush()
        self._write((command + "\n").encode("ascii"))

    def receive(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self._readline(deadline)
            if raw is None:
                break
            self.transcript.write("< " + raw.decode("ascii", errors="replace") + "\n")
            self.transcript.flush()
            if not raw.startswith((b"GRAYCAP ", b"GRAYTRANS ", b"GRAYTEST ", b"CP1 ERROR ")):
                continue
            fields = raw.decode("ascii").split()
            require("ERROR" not in fields, "Device rejected diagnostic request: " + " ".join(fields))
            return fields
        raise ProtocolError("Diagnostic response timed out; command was not retried")

    def request(self, command):
        self.send(command)
        return self.receive()


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def run_checks(link, output, orientation):
    expected = FIXTURE_CRCS[orientation]
    meta = decode_metadata(link.request("CMD:GRAYTEST"))
    save_json(output / "gray.json", meta)
    require(meta["valid"] == 1 and meta["generation"] > 0 and meta["driver_generation"] > 0 and
            (meta["width"], meta["height"], meta["plane_bytes"]) == (800, 480, PLANE_BYTES) and
            (meta["activation_count"], meta["control"]) == (1, 0xD7) and
            0 <= meta["busy_ms"] <= meta["render_ms"], "Gray capture dimensions, activation or timing invalid")
    for ram in (0x24, 0x26):
        crc = 0
        offset = 0
        with (output / f"gray-{ram:02x}.bin").open("wb") as stream:
            while offset < PLANE_BYTES:
                fields = link.request(f"CMD:GRAYCAP {meta['generation']} {ram:02X} {offset}")
                require(len(fields) == 7 and fields[:2] == ["GRAYCAP", "DATA"], "Malformed capture chunk")
                require((int(fields[2]), int(fields[3], 16), int(fields[4])) == (meta["generation"], ram, offset),
                        "Capture chunk generation, plane or offset mismatch")
                chunk = bytes.fromhex(fields[6])
                require(int(fields[5]) == len(chunk) == min(256, PLANE_BYTES-offset), "Capture chunk length mismatch")
                stream.write(chunk)
                crc = zlib.crc32(chunk, crc)
                offset += len(chunk)
        require(crc == meta[f"crc{ram:02x}"] == expected[f"gray{ram:02x}"], "Gray capture literal CRC32 mismatch")
    sequences = (
        ("BW", [(0xF8, PLANE_BYTES, expected["inverted"], 0, 0),
                (0x14, PLANE_BYTES, expected["bw"], PLANE_BYTES, expected["bw"])]),
        ("BWCHANGED", [(0xFC, PLANE_BYTES, expected["changed"], PLANE_BYTES, expected["bw"])]),
        ("BWCHANGED", []),
    )
    results = []
    for index, (target, activations) in enumerate(sequences):
        lines = [link.request("CMD:GRAYTEST " + target)]
        for _ in range(MAX_ACTIVATIONS + 1):
            lines.append(link.receive())
            if lines[-1][:2] == ["GRAYTRANS", "END"]:
                break
        save_json(output / f"transition-{index}-raw.json", lines)
        result = decode_transition(lines)
        save_json(output / f"transition-{index}.json", result)
        check_transition(result, activations, target)
        if results:
            require(result["generation"] == results[-1]["generation"] + 1, "Transition generation did not advance")
        results.append(result)
    after = decode_metadata(link.request("CMD:GRAYCAP"))
    save_json(output / "gray-after.json", after)
    require(after == meta, "BW transitions changed frozen gray metadata")
    summary = {"status": "PASS", "orientation": orientation, "gray": meta, "transitions": results,
               "optical_assessment": "not performed"}
    save_json(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--orientation", choices=FIXTURE_CRCS, default="rotated")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    require(args.timeout > 0, "Timeout must be positive")
    import serial

    args.output.mkdir(parents=True, exist_ok=False)
    port = serial.Serial(baudrate=115200, timeout=0.2, write_timeout=5)
    port.dtr = True
    port.rts = True
    port.port = args.port
    port.open()
    try:
        with (args.output / "transcript.txt").open("w") as transcript:
            link = GrayLink(port, transcript, args.timeout)
            checks_completed = False
            try:
                run_checks(link, args.output, args.orientation)
                checks_completed = True
            finally:
                try:
                    require(link.request("CMD:GRAYTEST END") == ["GRAYTEST", "END"], "Diagnostic session did not release")
                except (ProtocolError, ValueError, OSError) as error:
                    if checks_completed:
                        raise
                    print(f"FAIL diagnostic cleanup: {error}", file=sys.stderr)
    finally:
        port.close()
    print(f"PASS gray capture and BW transitions; evidence: {args.output}")


if __name__ == "__main__":
    try:
        main()
    except (ProtocolError, ValueError, OSError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        sys.exit(1)
