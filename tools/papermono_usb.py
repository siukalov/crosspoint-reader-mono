#!/usr/bin/env python3
"""Transfer Classics EPUBs and reading fonts over the CP1 USB serial protocol."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import time
import zlib

CHUNK_BYTES = 512
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 1100
LICENSE_NAMES = {"OFL.txt", "LICENSE.txt", "LICENSE.md"}
RESPONSE_FIELD_COUNTS = {
    "IDLE": 2, "ABORTED": 2, "CLOSED": 2, "ERROR": 3, "READY": 3,
    "ACK": 3, "READABLE": 3, "EOF": 3, "UPLOAD": 5, "READBACK": 5,
    "VERIFY": 4, "COMPLETE": 4, "CHUNK": 4,
}
RESPONSE_NUMERIC_COUNTS = {
    "ACK": 1, "READABLE": 1, "EOF": 1, "UPLOAD": 3, "READBACK": 3,
    "VERIFY": 2, "COMPLETE": 1, "CHUNK": 1,
}


class ProtocolError(Exception):
    pass


def remote_fields(remote):
    directory, separator, name = remote.rpartition("/")
    if not separator or not name:
        raise ProtocolError("Remote path must include /Classics/ or /fonts/ and a filename")
    valid_root = directory in {"/Classics", "/fonts"} or re.fullmatch(
        r"/fonts/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", directory
    )
    valid_name = (
        re.fullmatch(r"[A-Za-z0-9 _().-]{1,128}", name)
        and not name.startswith(".")
        and not name.endswith((".", " "))
        and ".." not in name
    )
    valid_extension = (
        name.endswith(".epub") if directory == "/Classics"
        else name.endswith(".cpfont") or name in LICENSE_NAMES
    )
    if not valid_root or not valid_name or not valid_extension:
        raise ProtocolError("Remote path is outside the supported library/font scope")
    return f"{directory.encode('ascii').hex()} {name.encode('ascii').hex()}"


def unsigned(text, maximum=MAX_FILE_BYTES):
    if not re.fullmatch(r"[0-9]{1,10}", text):
        raise ProtocolError("Malformed integer in CP1 response")
    value = int(text)
    if value > maximum:
        raise ProtocolError("CP1 integer exceeds its limit")
    return value


def response_fields(raw):
    if len(raw) > MAX_LINE_BYTES or any(byte < 32 or byte > 126 for byte in raw):
        raise ProtocolError("Invalid bytes in CP1 response")
    fields = raw.decode("ascii").split(" ")
    if len(fields) < 2 or any(not field for field in fields):
        raise ProtocolError("Malformed CP1 response")
    tag = fields[1]
    if RESPONSE_FIELD_COUNTS.get(tag) != len(fields):
        raise ProtocolError("Invalid CP1 response shape")
    if tag == "READY" and fields[2] != str(CHUNK_BYTES):
        raise ProtocolError("Unsupported chunk size")
    for field in fields[2:2 + RESPONSE_NUMERIC_COUNTS.get(tag, 0)]:
        unsigned(field)
    if tag == "COMPLETE" and not re.fullmatch(r"[0-9a-f]{8}", fields[3]):
        raise ProtocolError("Malformed completion CRC")
    if tag == "CHUNK" and not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,512}", fields[3]):
        raise ProtocolError("Malformed readback chunk")
    if tag == "ERROR" and not re.fullmatch(r"[A-Z]+", fields[2]):
        raise ProtocolError("Malformed device error")
    return fields


class Link:
    def __init__(self, serial_port, timeout=3.0, retries=3):
        if timeout <= 0 or retries < 0 or retries > 10:
            raise ValueError("Timeout must be positive and retries must be between 0 and 10")
        self.serial = serial_port
        self.timeout = timeout
        self.retries = retries
        self.buffer = bytearray()
        self.discard_log = False
        self.last_response = None
        self.last_command = None

    def _write(self, wire):
        deadline = time.monotonic() + self.timeout
        offset = 0
        while offset < len(wire):
            if time.monotonic() >= deadline:
                raise ProtocolError("USB write timed out; incomplete frame was not retried")
            piece = wire[offset:offset + 64]
            written = self.serial.write(piece)
            if written is None or written < 0 or written > len(piece):
                raise ProtocolError("Invalid USB write result")
            if written == 0:
                time.sleep(0.005)
            offset += written
        self.serial.flush()

    def _readline(self, deadline):
        while time.monotonic() < deadline:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                if self.discard_log:
                    self.discard_log = False
                    continue
                return line.removesuffix(b"\r")
            if len(self.buffer) > MAX_LINE_BYTES:
                if self.buffer.startswith(b"CP1 ") or self.buffer == b"CP1":
                    raise ProtocolError("Oversized CP1 response")
                self.buffer.clear()
                self.discard_log = True
            chunk = self.serial.read(64)
            if chunk:
                self.buffer.extend(chunk)
        return None

    def request(self, command, accepts):
        wire = ("CP1 " + command + "\n").encode("ascii")
        for _ in range(self.retries + 1):
            self._write(wire)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                raw = self._readline(deadline)
                if raw is None:
                    break
                if raw != b"CP1" and not raw.startswith(b"CP1 "):
                    continue
                fields = response_fields(raw)
                line = raw.decode("ascii")
                if fields[1] == "ERROR":
                    raise ProtocolError(f"Device rejected request: {line!r}")
                if accepts(fields):
                    self.last_response, self.last_command = line, command
                    return fields
                if line == self.last_response and command != self.last_command:
                    continue
                raise ProtocolError(f"Unexpected CP1 response: {line!r}")
        raise ProtocolError(f"No response after {self.retries + 1} attempts")

    def exact(self, command, response):
        expected = response.split(" ")
        return self.request(command, lambda fields: fields == expected)

    def abort(self):
        self.exact("ABORT", "CP1 ABORTED")


def inspect_source(source):
    size = 0
    crc = 0
    digest = hashlib.sha256()
    source.seek(0)
    while True:
        data = source.read(65536)
        if not data:
            break
        size += len(data)
        if size > MAX_FILE_BYTES:
            raise ProtocolError("File exceeds 32 MiB")
        crc = zlib.crc32(data, crc)
        digest.update(data)
    if not size:
        raise ProtocolError("Empty files are unsupported")
    source.seek(0)
    return size, crc, digest.hexdigest()


def receive(link, remote, sink=None):
    response = link.request(
        "GET " + remote_fields(remote), lambda fields: len(fields) == 3 and fields[1] == "READABLE"
    )
    expected = unsigned(response[2])
    if not expected:
        raise ProtocolError("Device reported an empty file")
    digest = hashlib.sha256()
    received = 0
    sequence = 0
    while received < expected:
        fields = link.request(
            f"READ {sequence}",
            lambda response: len(response) == 4 and response[1:3] == ["CHUNK", str(sequence)],
        )
        data = bytes.fromhex(fields[3])
        if len(data) != min(CHUNK_BYTES, expected - received):
            raise ProtocolError("Readback chunk length differs from advertised file size")
        if sink is not None and sink.write(data) != len(data):
            raise ProtocolError("Short local output write")
        digest.update(data)
        received += len(data)
        sequence += 1
    link.exact(f"READ {sequence}", f"CP1 EOF {sequence}")
    link.exact("DONE", "CP1 CLOSED")
    return received, digest.hexdigest()


def upload(link, local, remote, verify_timeout=120.0):
    encoded_path = remote_fields(remote)
    with Path(local).open("rb") as source:
        size, crc, digest = inspect_source(source)
        link.exact(f"PUT {encoded_path} {size} {crc:08x}", "CP1 READY 512")
        sent = 0
        sequence = 0
        while sent < size:
            data = source.read(min(CHUNK_BYTES, size - sent))
            if not data:
                raise ProtocolError("Source file changed during upload")
            link.exact(f"DATA {sequence} {data.hex()}", f"CP1 ACK {sequence}")
            sent += len(data)
            sequence += 1
        if source.read(1):
            raise ProtocolError("Source file grew during upload")
    deadline = time.monotonic() + verify_timeout
    while True:
        fields = link.request(
            "COMMIT", lambda response: len(response) == 4 and response[1] in {"VERIFY", "COMPLETE"}
        )
        if fields[1] == "COMPLETE":
            if fields[2:] != [str(size), f"{crc:08x}"]:
                raise ProtocolError("Published size or CRC differs from source")
            break
        if unsigned(fields[2]) > size or unsigned(fields[3]) != size:
            raise ProtocolError("Invalid verification progress")
        if time.monotonic() >= deadline:
            raise ProtocolError("Stored-byte verification timed out")
        time.sleep(0.02)
    copied_size, copied_digest = receive(link, remote)
    if (copied_size, copied_digest) != (size, digest):
        raise ProtocolError("Stored readback SHA256 differs from source")
    return copied_size, copied_digest


def download(link, remote, local):
    target = Path(local)
    temporary = target.with_name(target.name + ".cp1.part")
    if target.exists():
        raise ProtocolError("Local destination already exists")
    owned = False
    try:
        with temporary.open("xb") as sink:
            owned = True
            result = receive(link, remote, sink)
            sink.flush()
            os.fsync(sink.fileno())
        os.link(temporary, target)
        temporary.unlink()
        owned = False
        return result
    finally:
        if owned:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="USB serial port, e.g. /dev/cu.usbmodem...")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=3.0, help="Seconds per response attempt")
    parser.add_argument("--retries", type=int, default=3, help="Retries after the first attempt, maximum 10")
    parser.add_argument("--verify-timeout", type=float, default=120.0)
    commands = parser.add_subparsers(dest="command", required=True)
    put = commands.add_parser("put", help="Upload and verify every stored byte by SHA256 readback")
    put.add_argument("local")
    put.add_argument("remote")
    get = commands.add_parser("get", help="Download without overwriting an existing local file")
    get.add_argument("remote")
    get.add_argument("local")
    commands.add_parser("status")
    commands.add_parser("abort")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not 0 <= args.retries <= 10 or args.verify_timeout <= 0 or args.baud <= 0:
        parser.error("Timeouts and baud must be positive; retries must be between 0 and 10")
    try:
        import serial
    except ImportError:
        parser.error("Install pyserial: python3 -m pip install pyserial")
    try:
        port = serial.Serial(baudrate=args.baud, timeout=0.1, write_timeout=args.timeout)
        port.dtr = True
        port.rts = True
        port.port = args.port
        with port:
            link = Link(port, args.timeout, args.retries)
            try:
                if args.command == "put":
                    size, digest = upload(link, args.local, args.remote, args.verify_timeout)
                    print(f"Verified {size} bytes, SHA256 {digest}")
                elif args.command == "get":
                    size, digest = download(link, args.remote, args.local)
                    print(f"Downloaded {size} bytes, SHA256 {digest}")
                elif args.command == "abort":
                    link.abort()
                    print("Transfer aborted")
                else:
                    fields = link.request("STATUS", lambda response: response[1] in {
                        "IDLE", "ABORTED", "CLOSED", "UPLOAD", "VERIFY", "COMPLETE", "READBACK"
                    })
                    print(" ".join(fields))
            except (ProtocolError, OSError, KeyboardInterrupt):
                if args.command in {"put", "get"}:
                    try:
                        link.abort()
                    except (ProtocolError, OSError):
                        pass
                raise
    except (ProtocolError, OSError, KeyboardInterrupt) as error:
        print(f"USB transfer failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
