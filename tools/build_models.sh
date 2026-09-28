#!/usr/bin/env bash
# Convert YOLOv9 weights (WongKinYiu/yolov9 v0.1 release) to ONNX for Frigate, with Frigate's own export recipe.
# Output: models-dist/yolov9-<size>-<imgsz>.onnx + SHA256SUMS + LICENCE-NOTE.md. First run pulls ~11 GB of build cache.
set -euo pipefail
cd "$(dirname "$0")/.."
for spec in t:320 s:320 s:640 m:640; do
  size=${spec%%:*}; img=${spec##*:}
  out="models-dist/yolov9-${size}-${img}.onnx"
  [ -s "$out" ] && { echo "have $out"; continue; }
  echo "== building $out"
  docker build -f tools/Dockerfile.yolov9 tools --build-arg MODEL_SIZE=$size --build-arg IMG_SIZE=$img --output models-dist
done
( cd models-dist && sha256sum yolov9-*.onnx > SHA256SUMS && ls -la )
cat > models-dist/LICENCE-NOTE.md <<'NOTE'
# About these model files

Each `yolov9-<size>-<imgsz>.onnx` is a straight conversion of the YOLOv9 weights published by the
YOLOv9 authors (WongKinYiu) at https://github.com/WongKinYiu/yolov9/releases/tag/v0.1
(`yolov9-<size>-converted.pt`), exported to ONNX with the recipe in `tools/Dockerfile.yolov9`
(the same recipe Frigate's documentation gives). Nothing was retrained or fine-tuned.

YOLOv9 is licensed under the GNU GPL v3.0. These converted files are distributed under the same
licence. The corresponding source is the original weights above plus the conversion recipe in this
repository. Licence text: https://www.gnu.org/licenses/gpl-3.0.txt

ElectriFix Plate Gate itself (MIT) only downloads these files; it does not link against them.
NOTE
echo "== done"
