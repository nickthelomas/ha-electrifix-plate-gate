# Publishing ElectriFix Plate Gate

Three things go public, in this order. Scripts do the staging and refuse to ship private details.

## 1. The integration repository (`github.com/nickthelomas/ha-electrifix-plate-gate`)

```bash
tools/publish_integration.sh /tmp/publish/ha-electrifix-plate-gate    # filtered copy, scanned
cd /tmp/publish/ha-electrifix-plate-gate
git init -b main && git add -A && git commit -m "ElectriFix Plate Gate 0.4.2"
git remote add origin https://github.com/nickthelomas/ha-electrifix-plate-gate.git
git push -u origin main && git tag v0.4.2 && git push origin v0.4.2
```
Create the empty repository on GitHub first (public, no README). **Never force-push `main` later**: HACS
downloads the default-branch HEAD zip and a replaced commit 404s in-flight installs. Bump
`manifest.json` + tag for every change.

## 2. The model files (GitHub release `models-v1` on that repository)

```bash
tools/release_assets.sh /tmp/publish/release
```
Create a release with tag `models-v1`, title "Detection models v1", and upload every file in that
folder (the four `.onnx`, `SHA256SUMS`, `LICENCE-NOTE.md`). The catalogue URLs in `models.py` already
point at `…/releases/download/models-v1/<file>`; the Companion verifies the sha256 on install.
Release notes: paste `LICENCE-NOTE.md` (GPL-3.0 conversions of the YOLOv9 authors' weights; sources linked).

## 3. The Companion add-on (`github.com/nickthelomas/ha-addons`)

```bash
git clone https://github.com/nickthelomas/ha-addons /tmp/publish/ha-addons
companion/addon/publish.sh /tmp/publish/ha-addons      # stages a BUILDABLE add-on folder, checks versions, scans
cd /tmp/publish/ha-addons && git add -A && git commit -m "Plate Gate Companion 0.2.0" && git push origin main
```
Users add the repository URL in Settings → Add-ons → Add-on Store → ⋮ → Repositories.

## Before any of it
- `./.venv/bin/pytest -q` and `companion/.venv/bin/pytest -q -c companion/pyproject.toml companion/tests` green.
- `sandbox/run_check.sh` green (13 checks).
- HACS default-store submission (later): hassfest + HACS action green on the public repo, brand images present, topics/description set.
