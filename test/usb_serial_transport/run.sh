#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/../.." && pwd)"
test_dir="$(mktemp -d -t papermono-transport-test.XXXXXX)"
trap 'rm -rf "$test_dir"' EXIT

python3 - "$repo_dir" "$test_dir" <<'PY'
from pathlib import Path
import sys

source = (Path(sys.argv[1]) / "lib/UsbFileTransfer/UsbSerialTransport.h").read_text()
mutants = {
    "accept-malformed": ("return malformed ? Result::Malformed : Result::Complete;",
                         "return malformed ? Result::Complete : Result::Complete;"),
    "unbounded-writes": ("std::min<size_t>(size - sent, 64)", "std::min<size_t>(size - sent, 4096)"),
}
for name, (before, after) in mutants.items():
    if source.count(before) != 1:
        raise RuntimeError(f"mutation target changed: {name}")
    directory = Path(sys.argv[2]) / name
    directory.mkdir()
    (directory / "UsbSerialTransport.h").write_text(source.replace(before, after))
PY

for mutant in accept-malformed unbounded-writes; do
  "${CXX:-c++}" -std=c++17 -DNDEBUG -Wall -Wextra -Werror -pedantic \
    -I "$test_dir/$mutant" -I "$repo_dir/lib/UsbFileTransfer" \
    "$repo_dir/test/usb_serial_transport/test.cpp" -o "$test_dir/$mutant-test"
  if "$test_dir/$mutant-test" > "$test_dir/$mutant.log" 2>&1; then
    cat "$test_dir/$mutant.log"
    echo "FAIL: semantic mutant survived: $mutant"
    exit 1
  fi
  cat "$test_dir/$mutant.log"
  echo "PASS: semantic mutant rejected: $mutant"
done

"${CXX:-c++}" -std=c++17 -DNDEBUG -Wall -Wextra -Werror -pedantic \
  -I "$repo_dir/lib/UsbFileTransfer" \
  "$repo_dir/test/usb_serial_transport/test.cpp" -o "$test_dir/transport-test"
"$test_dir/transport-test"
