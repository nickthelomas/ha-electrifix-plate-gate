# About these model files

Each `yolov9-<size>-<imgsz>.onnx` is a straight conversion of the YOLOv9 weights published by the
YOLOv9 authors (WongKinYiu) at https://github.com/WongKinYiu/yolov9/releases/tag/v0.1
(`yolov9-<size>-converted.pt`), exported to ONNX with the recipe in `tools/Dockerfile.yolov9`
(the same recipe Frigate's documentation gives). Nothing was retrained or fine-tuned.

YOLOv9 is licensed under the GNU GPL v3.0. These converted files are distributed under the same
licence. The corresponding source is the original weights above plus the conversion recipe in this
repository. Licence text: https://www.gnu.org/licenses/gpl-3.0.txt

ElectriFix Plate Gate itself (MIT) only downloads these files; it does not link against them.
