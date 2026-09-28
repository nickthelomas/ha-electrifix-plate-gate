#!/usr/bin/env bash
# Assemble a BUILDABLE add-on folder. The Supervisor's build context is the add-on folder itself,
# so the Python package and requirements must live inside it. This copies them in.
#
#   companion/addon/publish.sh <target dir>      e.g. ~/src/ha-addons   (a checkout of nickthelomas/ha-addons)
#
# Result: <target>/electrifix_plate_gate_companion/{config.yaml,Dockerfile,build.yaml,run.sh,DOCS.md,README.md,
#         requirements.txt,app/plate_gate_companion/}  — and <target>/repository.yaml if missing.
# It refuses when config.yaml's version and plate_gate_companion.VERSION disagree.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
target="${1:?usage: publish.sh <target dir>}"
slug=electrifix_plate_gate_companion
src="$here/$slug"
cfg_ver=$(grep -E '^version:' "$src/config.yaml" | sed -E 's/version: *"?([^"]*)"?/\1/')
py_ver=$(grep -E '^VERSION' "$root/companion/app/plate_gate_companion/__init__.py" | sed -E 's/.*"([^"]*)".*/\1/')
if [ "$cfg_ver" != "$py_ver" ]; then
  echo "version mismatch: config.yaml says $cfg_ver, plate_gate_companion says $py_ver" >&2; exit 1
fi
mkdir -p "$target/$slug/app"
cp "$src"/config.yaml "$src"/Dockerfile "$src"/build.yaml "$src"/run.sh "$src"/DOCS.md "$src"/README.md "$target/$slug/"
cp "$root/companion/requirements.txt" "$target/$slug/requirements.txt"
rm -rf "$target/$slug/app/plate_gate_companion"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$root/companion/app/plate_gate_companion/" "$target/$slug/app/plate_gate_companion/"
if [ ! -f "$target/repository.yaml" ]; then
  cat > "$target/repository.yaml" <<'YAML'
name: ElectriFix Add-ons
url: https://github.com/nickthelomas/ha-addons
maintainer: ElectriFix Perth <admin@electrifixperth.com>
YAML
fi
# never ship a name, LAN address or token
if grep -rnE "Nick|192\.168\.|100\.[0-9]+\.[0-9]+\.[0-9]+|Bearer|@Password" "$target/$slug" | grep -vE "nickthelomas"; then
  echo "refusing: private details found above" >&2; exit 1
fi
echo "staged $slug $cfg_ver in $target"
