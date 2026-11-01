#!/usr/bin/env bash
set -euo pipefail

hub_root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
python_bin="${PYTHON_BIN:-python3}"

"$python_bin" -B "$hub_root/scripts/test-capability-v1.py"
