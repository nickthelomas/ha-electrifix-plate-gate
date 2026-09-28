# ElectriFix Plate Gate Companion

Runs next to Frigate so ElectriFix Plate Gate can put detection-model files where Frigate reads them,
and can see what your machine really has (a Coral on USB or M.2, Hailo, MemryX, CPU, RAM).

## Install

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add `https://github.com/nickthelomas/ha-addons`.
2. Install **ElectriFix Plate Gate Companion** and start it. It borrows the Mosquitto add-on's login
   automatically; if you run a different broker, fill in the MQTT fields.
3. In Plate Gate (Settings → Devices & Services → ElectriFix Plate Gate → Configure) the *Companion*
   sensor turns **online**, and **Install model** appears in the menu.

## Options

| Option | Meaning |
|---|---|
| `frigate_config_dir` | Where the Frigate add-on keeps its config. The default is right for the official Frigate add-on. |
| `frigate_media_dir` | Where Frigate keeps clips and snapshots (read-only, for the accuracy benchmark). Default is right for the add-on. |
| `mqtt_*` | Leave blank to use the Mosquitto add-on. |
| `allowed_bases` | Extra download sources (comma separated). By default only ElectriFix's release page is allowed. |

## Size

About 300 MB: it carries a small inference library (onnxruntime) so the accuracy benchmark can run candidate models on your own snapshots. The first install builds locally and takes a few minutes.

## What it can and cannot do

It writes only inside `model_cache/plate_gate/` in Frigate's config folder, downloads only from allowed
sources, verifies checksums, and never runs what it downloads. It has no port, no panel, no access to
Home Assistant's or the Supervisor's API, and it never touches Frigate's API or your cameras.
