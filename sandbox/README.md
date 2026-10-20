# Local sandbox

A throwaway Home Assistant, a Mosquitto broker, a 40-line fake Frigate API and a fake
garage door (a template cover over an input_boolean). Everything binds to 127.0.0.1.
Nothing here talks to a real house.

```bash
cd sandbox
docker compose up -d          # first run pulls the Home Assistant image
./run_check.sh                # onboards HA, adds MQTT + Plate Gate via the setup-flow API, then:
                              #  1) dry run on  → publish XO ·520 → last_action = would_open
                              #  2) dry run off → publish       → the fake garage opens
                              #  3) cooldown 180 → publish      → skipped / cooldown
                              #  4) test button                 → garage does not move
                              #  5) write to Frigate            → backup, diff, restart, verify → ok
                              #  6) armed failure               → automatic rollback, config unchanged
                              #  7) hardware screen             → report + recommendation
                              #  8) benchmark s-320 vs m-640    → table, original config restored
                              #  9) Restore button              → config from before step 5
                              # 10) Companion online with a real probe of this laptop
                              # 11) Install model via the Companion → file + checksum in the shared volume
                              # 12) benchmark/hardware screens show it as installed
                              # 13) accuracy check: two models over the sample photos (python3 fetch_photos.py first)
```

Open http://127.0.0.1:8123 (user `sandbox`, password `sandbox-pw-123`) to click through the
screens yourself: Settings → Devices & Services → Add Integration → ElectriFix Plate Gate.
The fake Frigate is `http://frigate:5000` from inside HA (`http://127.0.0.1:5005` from the laptop).

The integration folder is bind-mounted read-only, so after a code change run
`docker compose restart homeassistant` and `./run_check.sh` again. `docker compose down -v`
plus `rm -rf ha-config/.storage .token` gives a clean slate.

## Verified on

| Date | Home Assistant | Result |
|---|---|---|
| 2026-09-28 | 2026.8.2 (container `stable`, Python 3.14) | All four checks passed. Found and fixed one real bug on the way: the cooldown counted the cover's `last_changed`, which after an HA restart is just the start time, so Plate Gate would have ignored the first three minutes after every restart. It now counts only door moves it observed itself. |
| 2026-09-28 (Phase 2) | 2026.8.2 | All nine checks passed: write (6 s round trip incl. Frigate restart), armed failure → automatic rollback with the config byte-identical, hardware report + recommendation, benchmark of two models with the original config restored, one-click Restore. Two real findings fixed on the way: the write screen aborted when Frigate already matched (so plate-crop saving could never be turned on later), and Restore picked a backup identical to the running config after a rolled-back write. |
| 2026-09-28 (Phase 3a) | 2026.8.2 | All twelve checks passed. The Companion container (docker image from `companion/docker/Dockerfile`) came online with a real probe of this laptop (i7-13620H, 6 USB devices), installed `yolov9-s-320.onnx` through the Install-model screen from the sandbox's own model server with the checksum matching `SHA256SUMS`, and the benchmark/hardware screens showed it as installed. |
| 2026-09-28 (Phase 3b) | 2026.8.2 | All thirteen checks passed. The rebuilt Debian companion (onnxruntime) installed two models and ran the accuracy check over six Wikimedia photos: yolov9-t-320 found a vehicle in 4/6 frames (7 vehicles), yolov9-s-320 in 6/6 (8 vehicles); after the review fix pass (letterboxing, streaming) 5/6 and 5/6. |
