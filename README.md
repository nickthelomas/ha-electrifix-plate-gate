<p align="center"><img src="https://raw.githubusercontent.com/nickthelomas/ha-electrifix-plate-gate/main/custom_components/electrifix_plate_gate/brand/icon.png" width="96" alt="ElectriFix"></p>

# ElectriFix Plate Gate

Your garage or gate opens for your car's number plate, using the plate reader that is
already built into [Frigate](https://frigate.video). No cloud, no subscription, no
trial and error.

You enter the people and plates, pick the door, and Plate Gate does the matching, the
cooldown and the safety checks itself. It also writes out the Frigate settings you need.

Everything in here was learned the hard way on a real driveway, so the defaults are the
ones that turned out to matter.

## Before you start: this is a security decision

A photo of a plate can open your door. Plate Gate is conservative by default, but you
should decide how much you trust it:

- It starts in **dry run**. It logs what it *would* have done and moves nothing until you
  turn dry run off. Give it a few days and watch the *Last action* sensor.
- Add your own conditions in an automation if you want them (only when someone is home,
  only during the day). Plate Gate gives you the sensors to do that.
- **Locks** can be chosen, but only after you tick a disclaimer. A plate read unlocking your
  front door is a bigger step than opening a garage. Think about it.
- Nothing leaves your Home Assistant. Plate Gate talks to your Frigate and to nothing else.

## What you need

- Home Assistant 2025.1 or newer, with the **MQTT** integration set up.
- **Frigate 0.18** with MQTT on, and a camera that already works in Frigate and sees plates as
  cars arrive and leave. Plate Gate was built and tested against 0.18 (its API and message
  format). It is not claimed to work on any other version: it may, and it will tell you when it
  sees one, but check every diff. It has not yet been run against a live Frigate, so treat the
  first week as a trial (dry run does that).
- Plate Gate never changes how a camera is pulled in (stream URLs, codecs). Set the camera up in
  Frigate first; if you swap cameras, fix the stream in Frigate, then Plate Gate carries on.
- A garage door or gate in Home Assistant as a `cover`, a `switch` / `script` that triggers
  it, or a `lock` (with the disclaimer).

## Installing it

### With HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Open the menu (top right) and choose **Custom repositories**.
3. Paste `https://github.com/nickthelomas/ha-electrifix-plate-gate`, choose category
   **Integration**, and select **Add**.
4. Find **ElectriFix Plate Gate** in the list and select **Download**.
5. **Restart Home Assistant** when it asks you to.

### Without HACS

Copy the `custom_components/electrifix_plate_gate` folder from this repository into your
Home Assistant `config` folder, so you end up with
`config/custom_components/electrifix_plate_gate/`, then restart Home Assistant.

## Setting it up

Go to **Settings → Devices & Services → Add Integration** and search for
**ElectriFix Plate Gate**. There are five screens.

| Screen | What you do |
|---|---|
| **1. Connect to Frigate** | The address you open Frigate at. Port 5000 needs no login; port 8971 wants the Frigate username and password. If you already have the Frigate integration, the address is filled in for you. |
| **2. Camera** | Pick the camera that sees the plates. The screen shows each camera's detection size. |
| **3. People and plates** | One line per person: `Bonnie: NO860, 1ABC123`. Spaces, dots and dashes don't matter. Under *Near misses*, list similar plates (a neighbour's car, the regular Uber) and Plate Gate checks they **can't** open the door. It shows you the patterns and the verdicts, and you tick a box to say you've read them. |
| **4. What should open** | The door, and how: open on arrival, close on leaving, or both. Cooldown (default 3 minutes), "only while the car is moving", auto-close, and an optional zone filter. |
| **5. Frigate settings** | The `lpr:` block for Frigate's `config.yml`, plus two tips for your camera. You can paste it yourself, or let Plate Gate write it (next section). |

When you finish, Plate Gate is **enabled** and in **dry run**.

## Writing to Frigate for you

Open the Plate Gate device → **Configure** → **Write the plate-reader settings into Frigate**.
Plate Gate then:

1. Takes a copy of Frigate's `config.yml` (kept under `config/electrifix_plate_gate/backups/`, the last 20).
2. Shows you the exact change as a diff, with a plain-English summary. It only ever touches the
   plate-reader block, optionally the camera's detect size, and (from the benchmark) the model and
   detector lines. Zones, masks, recordings and everything else are left alone, comments included.
   Spacing may be tidied.
3. Waits for you to tick the box. Nothing happens before that.
4. Saves it and restarts Frigate once (detection pauses for 30 to 60 seconds).
5. Watches Frigate come back: the camera streaming again, the plate reader on. If that has not
   happened within two minutes, it **puts the previous config back automatically** and tells you.

Two options on that screen:

- **Native resolution**: removes the camera's detect width and height so Frigate detects at the
  camera's real stream size (Frigate 0.18: leave them empty and it uses the native size). Plates are
  small; this is the single cheapest improvement. On by default when the camera has an explicit size.
- **Save plate crops**: Frigate's `debug_save_plates`, useful for a week of diagnosis, then turn it off.

**Restore**: the *Restore Frigate* button puts back the config from before Plate Gate's last write, with
one press and one restart. The options menu also lets you pick any of the last 20 backups.

The *Frigate status* sensor shows where a write is up to (`applying`, `verifying`, `ok`, `rolled_back`,
`failed`) with the last backup path in its attributes.

## Hardware check and model recommendation

**Configure → Hardware check** reads what Frigate reports about the machine: detectors and their speed,
detection rate, skipped frames, camera detect sizes, GPU, whether a Coral is in use, and how fast the
plate reader itself runs. It then recommends a detection model with the cost stated plainly, in the
form "best for the cost; X finds more plates for Y more CPU". The reasoning:

| Situation | Recommendation |
|---|---|
| A Coral is doing the detecting | Keep it (basic). A Coral cannot run YOLOv9. |
| CPU / OpenVINO, plenty of headroom | YOLOv9-**m** at 640: found 96% of driveway cars in our test, about 42 ms per check on a Ryzen 7 mini PC. |
| CPU, moderate headroom | YOLOv9-**s** at 640: 89% of cars, about 17 ms. |
| CPU, tight, or frames already being skipped | YOLOv9-**s** at 320, and a second detector. |

The *Hardware* sensor keeps the last report in its attributes.

### Getting a model file onto the Frigate machine: the Companion

Frigate can only load a model from a local file, and Plate Gate (inside Home Assistant) cannot put a
file on the Frigate machine. The **Plate Gate Companion** is a small service that runs next to Frigate
with write access to Frigate's config folder. It does two things on Plate Gate's behalf, over the MQTT
broker Frigate already uses: it puts model files into `model_cache/plate_gate/` (downloaded only from
ElectriFix's release page, checksum-verified) and it reports what the machine really has (CPU, RAM,
a Coral on USB or M.2 that Frigate isn't using, Hailo, MemryX). It has no port, no panel, no access to
Home Assistant's API, and never touches Frigate's API or your cameras.

- **Home Assistant OS / Supervised**: add the repository `https://github.com/nickthelomas/ha-addons`,
  install **ElectriFix Plate Gate Companion**, start it. It borrows the Mosquitto add-on's login.
- **Frigate in Docker**: add the service from `companion/docker/docker-compose.example.yml` next to
  Frigate, mounting the same folder Frigate has at `/config`.

When the Companion is online the *Companion* sensor says so, **Configure → Install model** appears, and
the hardware and benchmark screens show which files are already on the box. Without the Companion the
hardware screen gives you the one-line `curl` to run yourself:

```
mkdir -p /path/to/frigate/config/model_cache/plate_gate && curl -L -o /path/to/frigate/config/model_cache/plate_gate/yolov9-m-640.onnx <url>
```

The model files are straight conversions of the YOLOv9 authors' released weights, made with
`tools/Dockerfile.yolov9` (Frigate's own recipe). They are GPL-3.0; see `models-dist/LICENCE-NOTE.md`.
Until the first release is published the download links do not resolve yet. Converting on your own
machine stays a host-side recipe (`tools/build_models.sh`, needs Docker and ~11 GB of build cache); the
Companion does not do it, on purpose: it would be hours on a small box.

## Benchmark

**Configure → Benchmark** switches Frigate to each candidate you tick (a model, or "current model with
2 detectors"), lets it run on your live cameras for the minutes you choose, records per-check speed,
detections per second, skipped frames and detector CPU, then **puts your original config back**, even
if a candidate fails or you close the window. Each candidate costs one Frigate restart, and the model
files must already be on the Frigate machine. Results land in the *Benchmark* sensor and on screen as
a table. This is a speed benchmark; accuracy on your own footage is Phase 3.

## Accuracy check on your own footage

**Configure → Accuracy check** (needs the Companion). The Companion runs the models you tick over the
most recent event snapshots of your camera and counts, per model, the frames where it found a vehicle,
how many vehicles, and the mean confidence. It runs on the Frigate machine's CPU for a few minutes;
Frigate keeps working. Results land in the *Accuracy* sensor and on screen.

Read it honestly: those snapshots are frames Frigate **already** flagged, so the test favours the
model that raised them and cannot count what nobody saw. Each whole frame is letterboxed to the
model's input size, which is a harder test than Frigate's motion-region crops, so counts run below
Frigate's and the 640 models gain a little on the 320 ones. It compares candidates on *your* scene; it
is not ground truth. The per-frame time shown is the Companion's inference library on the CPU, not
Frigate's detector: use the speed benchmark for that.

## What you get

One device per camera, named *Plate Gate: &lt;camera&gt;*, with:

| Entity | What it is |
|---|---|
| **Enabled** (switch) | Master on/off. |
| **Dry run** (switch) | On by default. While on, nothing moves; the sensors still update. |
| **Last plate** (sensor) | The last plate read on this camera, normalised (`NO860`). Attributes: the raw read (`NO ·860`), who it matched, the pattern, Frigate's confidence, the event id. |
| **Last action** (sensor) | `open`, `close`, `trigger`, `would_open`/`would_close` (dry run), `skipped`, or `error`. The `reason` attribute explains a skip: `cooldown`, `device_busy`, `no_match`, `no_transition`, `stationary`, `outside_zone`. |
| **Timing** (sensor) | Seconds from the car first being seen to the action. Attributes break it down: car seen → plate read → action → door moving. |
| **Cooldown** (number) | Seconds since the door last moved before Plate Gate will move it again. |
| **Frigate status** (sensor) | Where the last write/restore/benchmark is up to, and the last backup path. |
| **Hardware** (sensor) | The last hardware check and recommendation. |
| **Benchmark** (sensor) | The last benchmark's rows. |
| **Companion** (sensor) | none / online / offline, with what the Companion last reported. |
| **Accuracy** (sensor) | The last accuracy check's rows. |
| **Restore Frigate** (button) | One press: put back the config from before the last write (restarts Frigate). |
| **Test a plate** (button) | Runs the whole chain with a pretend read of your first plate, always in dry run. It never moves the door. Use it to check the setup without driving anywhere. It obeys the same rules as a real read, so if the door moved in the last few minutes it will honestly report `skipped` / `cooldown`. |

## How the matching works

Frigate's plate reader often returns plates with spaces and dots in them, like `NO ·860`,
and sometimes swaps look-alike characters (O and 0, I and 1, B and 8, S and 5, G and 6,
Z and 2). A plain text comparison misses perfectly good reads.

Plate Gate normalises every read (upper case, separators removed) and matches it against a
tolerant pattern built from your plate. `NO860` becomes:

```
N[ .·•\-]*[O0][ .·•\-]*[8B][ .·•\-]*[6G][ .·•\-]*[0O]
```

That is also what goes into Frigate's `known_plates`, because Frigate matches the raw
read with a regular expression and does no normalising of its own.

**Match distance** (default 0) lets you accept reads that are one or two characters wrong
as well. Be careful with it: with distance 1, `NO840` opens the door for `NO860`. That is
exactly what the near-miss check on screen 3 is for. The Frigate block always uses
`match_distance: 0`, because Plate Gate does the tolerant matching itself and Frigate's
own distance check does not work with patterns.

## How the door logic works

Every Frigate update for a car on your camera goes through the same checks, in order.
The first one that fails is the `reason` on the *Last action* sensor.

1. Enabled, right camera, it's a vehicle, the event hasn't ended.
2. The plate matches one of your people (Plate Gate checks the raw read itself, even if
   Frigate already labelled the car).
3. "Only while moving" and the zone filter, if you turned them on.
4. The door isn't busy (`opening`, `closing`, `unavailable`).
5. Door **closed** and *open on arrival* → open. Door **open** and *close on leaving* →
   close. For a lock: locked → unlock, unlocked → lock. Anything else → `no_transition`.
6. **Repeat protection**: the cooldown (nothing within N seconds of the door last moving,
   whoever moved it; survives a reload or restart), and optionally once per Frigate event. This is the one that stops the door re-opening 90 seconds after you drove off,
   because Frigate kept tracking your car in the street.
7. Dry run → log "would open", stop. Otherwise → do it.

### Repeat protection: you choose the risk

The same car can be seen by Frigate for a long time. Something has to stop the door flipping
again on every update, and each way of doing that has a failure mode. Plate Gate lets you pick:

| Choice | How it works | What can go wrong |
|---|---|---|
| **Cooldown only** (default) | Nothing moves within the cooldown of the door last moving, whoever moved it. | A car parked in view for longer than the cooldown, still tracked by Frigate, can trigger the door again. |
| **Cooldown + once per Frigate event** | As above, and one Frigate tracking event can only act once. | Frigate can start a *new* event for the same car: a bad frame, someone walking in front of the camera, the car leaving view and coming back, or pulling forward to grab something and reversing again. A new event may trigger the door again once the cooldown allows. |
| **Only while moving** (extra, off by default) | Ignore cars Frigate marks as stationary. | A car that stops before its plate is read is missed, and a slow arrival can be marked stationary. |

Whatever you pick, the *Last action* sensor tells you why something was skipped
(`cooldown`, `same_event`, `stationary`), so you can tune it from evidence.

**Auto-close** (default 5 minutes, 0 = off): if Plate Gate opened (or unlocked) the door and it is
still open after that long, it closes (re-locks) it. If you close it yourself first, the timer is cancelled.
Turning Plate Gate off, or dry run on, also cancels a pending auto-close: when you switch it
off, nothing moves.

## Frigate tips

- **Detection size**: set your camera's `detect` width and height to the camera's real
  stream size. Plates are small, and feeding Frigate a downscaled picture throws away the
  pixels the plate reader needs. Screen 5 shows the current size.
- **Don't mask the street.** Tracking a car as it turns in gives the plate reader a
  head start and keeps the door quick.
- **Camera aim beats models.** When plates are missed it is usually because the plate is
  cut off at the bottom of the frame as the car pulls close, or the car is side-on. Fix
  the aim before you change models. Frigate's `debug_save_plates: true` saves the crops
  it tried, under `/media/frigate/clips/lpr/<camera>/`.
- **Zones don't limit detection**, only masks do, and a car is "in" a zone by the bottom
  centre of its box. If you use the zone filter, pick the zone the car is in when you want
  the door to move, not where it parks.
- Restarting Frigate pauses detection for about a minute. Do it when nobody is on the way
  home.

## Typical timings

On a Ryzen 7 mini PC running Frigate's YOLOv9 model on the CPU, arrivals take about
5 seconds from first sight to the door moving, and exits 8 to 12 seconds (the plate is on
the back of the car, which the camera sees later). The *Timing* sensor tells you yours.

## Roadmap

- **Phase 2 (this release)**: Frigate writer with backup, diff, verify and rollback; hardware check;
  model recommendation; speed benchmark; hosted model files.
- **Phase 3 (this release)**: the Companion add-on/container: model files put in place for you, real
  hardware probe (Coral/Hailo/MemryX, CPU, RAM), camera stream host shown so a swapped camera is obvious,
  and the accuracy check on your own footage.

## Stuck?

ElectriFix can set this up for you: https://fix.electrifixperth.com.au

## Licence

MIT. ElectriFix Perth, 2026.
