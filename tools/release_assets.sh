#!/usr/bin/env bash
# Collect the model release assets (GitHub release tag `models-v1`): the ONNX files + SHA256SUMS + LICENCE-NOTE.md.
#   tools/release_assets.sh <target dir>
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:?usage: release_assets.sh <target dir>}"
mkdir -p "$target"
cp "$root"/models-dist/yolov9-*.onnx "$root"/models-dist/SHA256SUMS "$root"/models-dist/LICENCE-NOTE.md "$target/"
( cd "$target" && sha256sum -c SHA256SUMS )
echo "release assets in $target"; ls -la "$target"
