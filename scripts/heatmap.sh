#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${HEATMAP_PYTHON:-python3}" "$SCRIPT_DIR/heatmap.py" "$@"
