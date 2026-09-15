import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "patch_freeink.py"
SPEC = importlib.util.spec_from_file_location("patch_freeink", SCRIPT_PATH)
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)

SOURCE_PATH = "libs/display/FreeInkDisplay/src/driver/Ssd1683Driver.cpp"
FIXTURE_PATCH = """diff --git a/libs/display/FreeInkDisplay/src/driver/Ssd1683Driver.cpp b/libs/display/FreeInkDisplay/src/driver/Ssd1683Driver.cpp
--- a/libs/display/FreeInkDisplay/src/driver/Ssd1683Driver.cpp
+++ b/libs/display/FreeInkDisplay/src/driver/Ssd1683Driver.cpp
@@ -1 +1 @@
-before
+after
"""


class PatchHookTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_dir = Path(self.temp_dir.name)
        self.sdk_dir = self.project_dir / "freeink-sdk"
        self.source = self.sdk_dir / SOURCE_PATH
        self.source.parent.mkdir(parents=True)
        self.source.write_text("before\n")
        self.patch_path = (
            self.project_dir
            / "scripts/freeink_patches/0001-papermono-diagnostics.patch"
        )
        self.patch_path.parent.mkdir(parents=True)
        self.patch_path.write_text(FIXTURE_PATCH)
        self.git("init", "--quiet")
        self.git("add", SOURCE_PATH)
        self.git(
            "-c", "user.name=Patch Hook Test", "-c", "user.email=patch@example.test",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Fixture source"
        )
        self.revision = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.sdk_dir, capture_output=True, text=True, check=True
        )

    def apply_patch(self):
        HOOK.patch_freeink(self.project_dir, expected_revision=self.revision)

    def test_fresh_apply(self):
        self.apply_patch()
        self.assertEqual(self.source.read_bytes(), b"after\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.revision)
        self.assertEqual(self.git("diff", "--cached").stdout, "")

    def test_repeat_apply(self):
        self.apply_patch()
        first_diff = self.git("diff").stdout
        self.apply_patch()
        self.assertEqual(self.source.read_bytes(), b"after\n")
        self.assertEqual(self.git("diff").stdout, first_diff)

    def test_missing_patch(self):
        self.patch_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "patch missing"):
            self.apply_patch()
        self.assertEqual(self.source.read_bytes(), b"before\n")

    def test_wrong_revision(self):
        with self.assertRaisesRegex(RuntimeError, "revision mismatch"):
            HOOK.patch_freeink(self.project_dir, expected_revision="0" * 40)
        self.assertEqual(self.source.read_bytes(), b"before\n")

    def test_diverged_input(self):
        self.source.write_text("diverged\n")
        with self.assertRaisesRegex(RuntimeError, "does not apply cleanly"):
            self.apply_patch()
        self.assertEqual(self.source.read_bytes(), b"diverged\n")

    def test_missing_sdk(self):
        shutil.rmtree(self.sdk_dir)
        with self.assertRaisesRegex(RuntimeError, "checkout missing"):
            self.apply_patch()

    def test_missing_source(self):
        self.source.unlink()
        with self.assertRaisesRegex(RuntimeError, "source missing"):
            self.apply_patch()

    def test_git_file_checkout(self):
        git_dir = self.project_dir / "sdk-git-dir"
        (self.sdk_dir / ".git").rename(git_dir)
        (self.sdk_dir / ".git").write_text("gitdir: %s\n" % git_dir)
        self.apply_patch()
        self.assertEqual(self.source.read_bytes(), b"after\n")

    def test_cli_rejects_unpinned_revision(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--project-dir", str(self.project_dir)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "expected f5518c7a39d3956dbc9d43be508bf9edae8f222d", result.stderr
        )
        self.assertEqual(self.source.read_bytes(), b"before\n")


if __name__ == "__main__":
    unittest.main()
