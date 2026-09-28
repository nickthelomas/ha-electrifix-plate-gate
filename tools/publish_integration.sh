#!/usr/bin/env bash
# Stage the PUBLIC copy of this repository (what goes to github.com/nickthelomas/ha-electrifix-plate-gate).
#   tools/publish_integration.sh <target dir>
# Includes: README, LICENSE, hacs.json, custom_components/, companion/ (no .venv), tools/, sandbox/ (no
# tokens, no HA storage, no fetched photos), .github/, models-dist/SHA256SUMS + LICENCE-NOTE.md, PUBLISHING.md.
# Excludes: HANDOFF.md, docs/ (internal specs/plans), models-dist/*.onnx (they go on the release), venvs, caches.
# Then scans the result for names, LAN addresses, tokens and passwords, and REFUSES on a hit.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:?usage: publish_integration.sh <target dir>}"
rm -rf "$target"   # a reused target must not keep stale files
mkdir -p "$target"
rsync -a --delete --delete-excluded --include='/sandbox/ha-config/configuration.yaml' --filter=':- .gitignore' \
  --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.superpowers' \
  --exclude='/sandbox/.token' --exclude='/sandbox/ha-config/.storage' --exclude='/sandbox/ha-config/*.db*' --exclude='/sandbox/ha-config/*.log*' \
  --exclude='/sandbox/ha-config/custom_components' --exclude='/sandbox/ha-config/deps' --exclude='/sandbox/ha-config/tts' --exclude='/sandbox/ha-config/blueprints' --exclude='/sandbox/ha-config/*.yaml.bak' \
  --exclude='/sandbox/media/frigate/clips/*' --exclude='/models-dist/build.log' --exclude='/models-dist/*.onnx' \
  --include='/README.md' --include='/LICENSE' --include='/hacs.json' --include='/PUBLISHING.md' --include='/.gitignore' \
  --include='/pyproject.toml' --include='/requirements_test.txt' --include='/tests/***' \
  --include='/custom_components/***' --include='/companion/***' --include='/tools/***' --include='/sandbox/***' --include='/.github/***' \
  --include='/models-dist/' --include='/models-dist/SHA256SUMS' --include='/models-dist/LICENCE-NOTE.md' \
  --exclude='*' \
  "$root/" "$target/"
bad=$(grep -rniE "\bnick\b|192\.168\.|100\.[0-9]+\.[0-9]+\.[0-9]+|Bearer [A-Za-z0-9]|github_pat_|ghp_|@Password|/home/nick" "$target" \
      --exclude-dir=.git --exclude=publish_integration.sh --exclude=publish.sh \
      | grep -vE "nickthelomas|192\.168\.1\.10 +# your broker|192\.168\.50\.40:554/stream1|192\.168\.50\.40/x|192\.168\.50\.40\"|192\.168\.50\.112 +# the HA box|sandbox-pw-123" || true)
if [ -n "$bad" ]; then echo "REFUSING to publish, private details found:"; echo "$bad"; exit 1; fi
echo "staged public copy in $target ($(find "$target" -type f | wc -l) files)"
