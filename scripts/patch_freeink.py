import argparse
import os
import subprocess
import sys


PINNED_REVISION = "f5518c7a39d3956dbc9d43be508bf9edae8f222d"
SOURCE_PATH = "libs/display/FreeInkDisplay/src/driver/Ssd1683Driver.cpp"
PATCH_PATH = "scripts/freeink_patches/0001-papermono-diagnostics.patch"


def patch_freeink(project_dir, *, expected_revision=PINNED_REVISION):
    project_dir = os.path.abspath(project_dir)
    sdk_dir = os.path.join(project_dir, "freeink-sdk")
    patch_path = os.path.join(project_dir, PATCH_PATH)
    if not os.path.exists(os.path.join(sdk_dir, ".git")):
        raise RuntimeError("FreeInk SDK checkout missing: %s" % sdk_dir)
    if not os.path.isfile(os.path.join(sdk_dir, SOURCE_PATH)):
        raise RuntimeError("FreeInk SDK source missing: %s" % SOURCE_PATH)
    if not os.path.isfile(patch_path):
        raise RuntimeError("FreeInk SDK patch missing: %s" % patch_path)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sdk_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    revision = result.stdout.strip()
    if revision != expected_revision:
        raise RuntimeError(
            "FreeInk SDK revision mismatch: expected %s, found %s"
            % (expected_revision, revision)
        )
    _apply_one(sdk_dir, patch_path)


def _apply_one(sdk_dir, patch_path):
    result = _check_patch(sdk_dir, patch_path, reverse=True)
    if result.returncode == 0:
        return
    result = _check_patch(sdk_dir, patch_path, reverse=False)
    if result.returncode != 0:
        raise RuntimeError(
            "FreeInk SDK patch %s does not apply cleanly:\n%s%s"
            % (os.path.basename(patch_path), result.stdout, result.stderr)
        )
    subprocess.run(["git", "apply", patch_path], cwd=sdk_dir, check=True)
    print("Applied FreeInk SDK patch: %s" % os.path.basename(patch_path))


def _check_patch(sdk_dir, patch_path, *, reverse):
    cmd = ["git", "apply", "--check"]
    if reverse:
        cmd.append("--reverse")
    cmd.append(patch_path)
    return subprocess.run(cmd, cwd=sdk_dir, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-dir", default=os.path.dirname(os.path.dirname(__file__))
    )
    args = parser.parse_args()
    try:
        patch_freeink(args.project_dir)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    return 0


if "Import" in globals():
    Import("env")  # noqa: F821
    patch_freeink(env["PROJECT_DIR"])  # noqa: F821
elif __name__ == "__main__":
    raise SystemExit(main())
