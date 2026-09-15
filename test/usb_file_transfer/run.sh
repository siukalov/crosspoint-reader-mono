#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/../.." && pwd)"
test_bin="$(mktemp -t papermono-usb-test.XXXXXX)"
trap 'rm -f "$test_bin"' EXIT
"${CXX:-c++}" -std=c++17 -Wall -Wextra -Werror -pedantic \
  -I "$repo_dir/lib/UsbFileTransfer" \
  "$repo_dir/lib/UsbFileTransfer/UsbFileTransfer.cpp" \
  "$repo_dir/test/usb_file_transfer/test.cpp" -o "$test_bin"
"$test_bin"
PYTHONDONTWRITEBYTECODE=1 python3 - "$repo_dir" "$test_bin" <<'PY'
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("papermono_usb", Path(sys.argv[1]) / "tools/papermono_usb.py")
usb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usb)


class ProcessSerial:
    def __init__(self):
        self.process = subprocess.Popen([sys.argv[2], "--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.pending = bytearray(b"normal boot log\n")
        self.incoming = bytearray()
        self.drop = {b"CP1 READY 512", b"CP1 ACK 0", b"CP1 CHUNK 0"}
        self.flushes = 0
        self.write_calls = 0

    def write(self, data):
        assert len(data) <= 64, "USB writes must be bounded to 64 bytes"
        self.write_calls += 1
        return os.write(self.process.stdin.fileno(), data[:7])

    def flush(self):
        self.flushes += 1

    def read(self, length):
        if not self.pending and select.select([self.process.stdout], [], [], 0.005)[0]:
            self.incoming.extend(os.read(self.process.stdout.fileno(), 4096))
            while b"\n" in self.incoming:
                line, _, tail = self.incoming.partition(b"\n")
                self.incoming = bytearray(tail)
                dropped = next((prefix for prefix in self.drop if line.startswith(prefix)), None)
                if dropped is not None:
                    self.drop.remove(dropped)
                    continue
                self.pending.extend(line + b"\n")
        result = bytes(self.pending[:length])
        del self.pending[:length]
        return result

    def close(self):
        self.process.stdin.close()
        assert self.process.wait(timeout=3) == 0
        self.process.stdout.close()


transport = ProcessSerial()
try:
    link = usb.Link(transport, timeout=0.1, retries=2)
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.epub"
        target = Path(directory) / "copy.epub"
        payload = bytes(range(256)) * 4 + b"final chunk"
        source.write_bytes(payload)
        expected = (len(payload), hashlib.sha256(payload).hexdigest())
        assert usb.upload(link, source, "/Classics/USB Test.epub") == expected
        assert usb.download(link, "/Classics/USB Test.epub", target) == expected
        assert target.read_bytes() == payload
        assert not transport.drop, "lost READY, ACK and readback CHUNK responses must be retried"
        assert transport.write_calls > transport.flushes * 2, "short writes must require multiple calls"
        try:
            usb.download(link, "/Classics/USB Test.epub", target)
            raise AssertionError("existing local file was not rejected")
        except usb.ProtocolError:
            assert target.read_bytes() == payload
        link.abort()
finally:
    transport.close()


class ScriptSerial:
    def __init__(self, incoming, zero_writes=False):
        self.incoming = bytearray(incoming)
        self.commands = 0
        self.zero_writes = zero_writes

    def write(self, data):
        self.commands += data.count(b"\n")
        return 0 if self.zero_writes else len(data)

    def flush(self):
        pass

    def read(self, length):
        data = bytes(self.incoming[:length])
        del self.incoming[:length]
        return data


class OpenSerial(ScriptSerial):
    def __init__(self, port=None, **kwargs):
        assert port is None, "serial port must remain closed until control lines are configured"
        super().__init__(b"CP1 IDLE\n")
        self.port = None
        self.dtr = self.rts = None
        self.closed = False

    def __enter__(self):
        assert self.port == "test-port"
        assert self.dtr is True and self.rts is True, "DTR and RTS must be high before opening"
        return self

    def __exit__(self, *args):
        self.closed = True


opened_ports = []


def open_serial(**kwargs):
    port = OpenSerial(**kwargs)
    opened_ports.append(port)
    return port


sys.modules["serial"] = SimpleNamespace(Serial=open_serial)
with redirect_stdout(io.StringIO()) as output:
    assert usb.main(["--port", "test-port", "status"]) == 0
assert output.getvalue() == "CP1 IDLE\n"
assert len(opened_ports) == 1 and opened_ports[0].closed


for response in [b"CP1\n", b"CP1 IDLE extra\n", b"CP1 \xff\n", b"CP1 " + b"x" * 1200 + b"\n"]:
    try:
        usb.Link(ScriptSerial(response), timeout=0.02).exact("STATUS", "CP1 IDLE")
        raise AssertionError("malformed framed response was ignored")
    except usb.ProtocolError:
        pass

try:
    usb.Link(ScriptSerial(b"CP1 IDLE extra\n"), timeout=0.02).request("STATUS", lambda response: response[1] == "IDLE")
    raise AssertionError("invalid status shape was accepted")
except usb.ProtocolError:
    pass

logs = b"CP10 ordinary log\n" + b"x" * 2000 + b"\nCP1 IDLE\n"
usb.Link(ScriptSerial(logs), timeout=0.02).exact("STATUS", "CP1 IDLE")
duplicates = ScriptSerial(b"CP1 READY 512\nCP1 READY 512\nCP1 ACK 0\n")
duplicate_link = usb.Link(duplicates, timeout=0.02)
duplicate_link.exact("PUT ignored", "CP1 READY 512")
duplicate_link.exact("DATA 0 00", "CP1 ACK 0")

silent = ScriptSerial(b"")
try:
    usb.Link(silent, timeout=0.01, retries=2).exact("STATUS", "CP1 IDLE")
    raise AssertionError("silent device should exhaust finite retries")
except usb.ProtocolError:
    assert silent.commands == 3

try:
    usb.Link(ScriptSerial(b"", zero_writes=True), timeout=0.01).exact("STATUS", "CP1 IDLE")
    raise AssertionError("zero USB writes should time out")
except usb.ProtocolError:
    pass

for remote in ["/Classics/../book.epub", "/fonts/a/b/font.cpfont", "/fonts/notes.md", "/Classics/bad\\.epub"]:
    try:
        usb.remote_fields(remote)
        raise AssertionError("invalid host remote path was accepted")
    except usb.ProtocolError:
        pass
assert usb.remote_fields("/fonts/SourceSerif4/LICENSE.md")
print("PASS: host CLI round trip against production core, lost acknowledgments, short writes, strict framing and finite retries")
PY
