#!/usr/bin/env python3
"""Check UI font assets and scoped generation without firmware dependencies."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "lib/EpdFont/builtinFonts"
SCRIPT = ROOT / "lib/EpdFont/scripts/convert-builtin-fonts.sh"
SELECTED_SIZE = None

# Baselines from the 1-bit headers at 5b444eb22c3b7b747cf1f282253461f3cee0c481.
BASELINES = {
    "ubuntu_10_regular": {
        "glyph_count": 1032, "bitmap_bytes": 23522,
        "metrics": "1f51f7ee2adc577407053202a19be0769767d106595099d5021166b51d9542fd",
        "tables": "de05f820420c26e565706bef7be1552a40b36ffcd501df913a8e9ab27a9f35fb",
        "metadata": "db94191fa49e0ab76abee1cbddb628bd3a23aa6cf4b304f30fad3dbe97240683",
        "command": "949ddc8b1d63636d62f45802aca91e84753dcfecf2bc1afa2b265c891aa71863",
        "edges": [(8, 1, 1), (5, 1, 2)],
    },
    "ubuntu_10_bold": {
        "glyph_count": 1032, "bitmap_bytes": 26693,
        "metrics": "f715014f4c4e4f1296c75ffc91788017e65c03079d7f4ed5aa11f5530cae0ed4",
        "tables": "b884cad5b86fa04742eec0622f6ad3fe7414027cf020d91f86f9d84006a8dc46",
        "metadata": "20c6ca5f552b7e33b6100d215ed3a2f35a34eb4b2dea5995381064f6bf30e07f",
        "command": "b24d91880af4ea50f0dd7cb4f98cabad7dadcd2e8a9c2396390e37cf554c8c47",
        "edges": [(9, 0, 1), (5, 1, 2)],
    },
    "ubuntu_12_regular": {
        "glyph_count": 1032, "bitmap_bytes": 32540,
        "metrics": "f21f231c189258e2f93f5bdb862d18a94fee89e2c66d05e026c8ab0e8e6a5cb2",
        "tables": "880894b370d48f7b9c9211ce071da165bbd59333a07c3b2c977e460450b13608",
        "metadata": "00cca154dde3450a4ecbf4eb7e601f233e0636a0bebd319b47210d9f9ff15b32",
        "command": "e8ccdcd05af901ec00a1b12ab9b2dccf7b13ef8d93f1de25775dfe47cefc4693",
        "edges": [(7, 4, 1), (9, 0, 2)],
    },
    "ubuntu_12_bold": {
        "glyph_count": 1032, "bitmap_bytes": 36561,
        "metrics": "fbb2600e3804212f9837e5530d659045713ec8205635d55eda36e67d4855952b",
        "tables": "c168c013c50bcf7ed2a738c95899b40e786544e1bd38cd83e4f5a45c32e74960",
        "metadata": "c18197a96c641e3bf64886e9fc8522bb1a01fee5fc461f6660d19606312480a8",
        "command": "f0fde74737800d9ea41d4efcd942d9708c2213364e08fec90a43d0d855f2ba50",
        "edges": [(5, 3, 1), (6, 1, 2)],
    },
    "notosans_8_regular": {
        "glyph_count": 1294, "bitmap_bytes": 18647,
        "metrics": "35e783e9035e3ea27569c92995db6d373760864a1c0a4c3636d59365927cea1d",
        "tables": "c4ed41087c93d907be6bc11720f9dec73609bcc48550ac872086df32a6500744",
        "metadata": "3455cf2f2fae68dc9ab9b55f9e2e9babfc82b971616e4ea1f0124588f6bd0ff1",
        "command": "cad2b2c1674a3ade194c2aa0c2a41ec4e831f0ea46ec2406b818d19357635188",
        "edges": [(3, 2, 1), (4, 0, 2)],
    },
}


def tokens(value):
    return re.findall(r"-?0x[0-9A-Fa-f]+|-?\d+|[A-Za-z_]\w*", value)


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def parse_header(name):
    text = (ASSETS / f"{name}.h").read_text()
    blocks = {
        suffix: re.sub(r"//[^\n]*", "", body)
        for suffix, body in re.findall(
            r"static const \w+ " + name + r"(\w*)\s*(?:\[[^]]*\])?\s*=\s*\{(.*?)\};",
            text, re.S,
        )
    }
    return text, blocks


def command_digest(command):
    return hashlib.sha256("\0".join(arg for arg in command if arg != "--2bit").encode()).hexdigest()


class UiFontAssetsTest(unittest.TestCase):
    def test_assets(self):
        for name, baseline in BASELINES.items():
            if SELECTED_SIZE and name.split("_")[1] != SELECTED_SIZE:
                continue
            with self.subTest(font=name):
                self.check_asset(name, baseline)

    def check_asset(self, name, baseline):
        text, blocks = parse_header(name)
        fields = tokens(blocks[""])
        self.assertEqual(fields[7], "true", f"{name}: expected 2bpp asset")
        fields[7] = "BPP"
        self.assertEqual(digest(fields), baseline["metadata"], "font metadata changed")
        rows = [tokens(row) for row in re.findall(r"\{([^}]+)\}", blocks["Glyphs"])]
        self.assertEqual(len(rows), baseline["glyph_count"])
        self.assertEqual(digest([row[:5] for row in rows]), baseline["metrics"], "glyph metrics changed")
        tables = {key: tokens(value) for key, value in blocks.items() if key not in ("", "Bitmaps", "Glyphs")}
        self.assertEqual(digest(tables), baseline["tables"], "coverage, kerning or ligatures changed")
        command = shlex.split(re.search(r"Command used: (.*)", text)[1])
        self.assertEqual(command.count("--2bit"), 1)
        self.assertEqual(command_digest(command), baseline["command"], "font stack or options changed")
        bitmap = bytes(int(value, 0) for value in tokens(blocks["Bitmaps"]))
        declared_size = int(re.search(name + r"Bitmaps\[(\d+)\]", text)[1])
        self.assertEqual(declared_size, len(bitmap))
        offset = 0
        glyphs = [[int(value, 0) for value in row] for row in rows]
        for width, height, _, _, _, length, start in glyphs:
            self.assertEqual(start, offset)
            self.assertEqual(length, (width * height + 3) // 4)
            offset += length
        self.assertEqual(offset, len(bitmap))
        intervals = [[int(value, 0) for value in tokens(row)] for row in re.findall(r"\{([^}]+)\}", blocks["Intervals"])]
        codepoints = [cp for first, last, _ in intervals for cp in range(first, last + 1)]
        self.assertEqual(len(codepoints), len(glyphs))
        self.assertEqual(codepoints, sorted(set(codepoints)))
        for first, _, start in intervals:
            self.assertEqual(codepoints[start], first)
        width, height, _, _, _, _, start = glyphs[codepoints.index(ord("A"))]
        # A's edge coordinates come from the source FreeType raster, before conversion.
        for x, y, expected in baseline["edges"]:
            self.assertLess(x, width)
            self.assertLess(y, height)
            pixel = y * width + x
            actual = (bitmap[start + pixel // 4] >> (6 - 2 * (pixel % 4))) & 3
            self.assertEqual(actual, expected, f"{name}: A({x}, {y}) coverage")
        print(f"{name}: bitmap {baseline['bitmap_bytes']} -> {len(bitmap)} bytes (+{len(bitmap) - baseline['bitmap_bytes']})")

    def run_script(self, arguments):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            assets = root / "builtinFonts"
            assets.mkdir()
            for name in BASELINES:
                (assets / f"{name}.h").write_text("unchanged\n")
            shutil.copy2(SCRIPT, scripts / SCRIPT.name)
            interpreter = root / "python"
            interpreter.write_text('#!/bin/bash\nprintf "%s\\0" "$@" >> "$FONT_CALLS"\nprintf "\\n" >> "$FONT_CALLS"\n')
            interpreter.chmod(0o755)
            calls = root / "calls"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", FONT_CALLS=str(calls))
            result = subprocess.run(["bash", str(scripts / SCRIPT.name), *arguments], env=env, capture_output=True, text=True)
            invoked = [line.split("\0")[:-1] for line in calls.read_text().splitlines()] if calls.exists() else []
            headers = {p.stem: p.read_text() for p in assets.glob("*.h")}
            return result, invoked, headers

    def test_invalid_arguments_do_not_write(self):
        for arguments in (["--bad"], ["10"], ["--ui-only", "9"], ["--ui-only", ""], ["--ui-only", "10", "12"]):
            with self.subTest(arguments=arguments):
                result, calls, headers = self.run_script(arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)
                self.assertEqual(calls, [])
                self.assertTrue(all(value == "unchanged\n" for value in headers.values()))

    def test_generation_scope_and_options(self):
        for arguments in ([], ["--ui-only"], ["--ui-only", "8"], ["--ui-only", "10"], ["--ui-only", "12"]):
            with self.subTest(arguments=arguments):
                result, calls, headers = self.run_script(arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected_ui = {name for name in BASELINES if len(arguments) < 2 or name.split("_")[1] == arguments[1]}
                generated = {call[1] for call in calls if call[0] == "fontconvert.py"}
                expected = set(expected_ui)
                if not arguments:
                    expected.update(f"{family}_{size}_{style.lower()}" for family in ("notoserif", "notosans") for size in (12, 14, 16, 18) for style in ("Regular", "Italic", "Bold", "BoldItalic"))
                self.assertEqual(generated, expected)
                self.assertEqual(len(calls), len(expected) + (not arguments))
                for call in calls:
                    if call[0] == "verify_compression.py":
                        self.assertEqual(call, ["verify_compression.py", "../builtinFonts/"])
                    elif call[1] in BASELINES:
                        self.assertEqual(call.count("--2bit"), 1)
                        self.assertEqual(command_digest(call), BASELINES[call[1]]["command"])
                    else:
                        family, size, style = call[1].split("_")
                        source = {"notoserif": "NotoSerif", "notosans": "NotoSans"}[family]
                        face = {"regular": "Regular", "italic": "Italic", "bold": "Bold", "bolditalic": "BoldItalic"}[style]
                        self.assertEqual(call, ["fontconvert.py", call[1], size, f"../builtinFonts/source/{source}/{source}-{face}.ttf", "--2bit", "--compress", "--pnum"])
                for name in set(BASELINES) - expected_ui:
                    self.assertEqual(headers[name], "unchanged\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", choices=("8", "10", "12"), help="Check only this UI font size")
    arguments, remaining = parser.parse_known_args()
    SELECTED_SIZE = arguments.size
    unittest.main(argv=[__file__, *remaining])
