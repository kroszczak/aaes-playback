# AAES — Automated and Autonomous Experimental Setup

**Playback experiments without an observer.**

A self-contained field unit that wakes itself at a scheduled time, plays a territorial
song toward a decoy for the whole trial window, watches the scene with a camera, and
records video only when a bird enters the frame — **without ever interrupting the
stimulus**. Between windows the unit is fully powered down.

Built on a Luckfox Pico Pro Max (Rockchip RV1106). All detection runs on the board's
NPU: no network, no cloud, no connectivity in the field.

> Companion repository to a conference poster. The unit is real and bench-verified;
> **no field deployment has been run yet.** See [Status](#status) for exactly what has
> and has not been demonstrated.

---

## Why this exists

A conventional playback trial needs a human inside the territory. That costs three
things at once:

- **the observer alters the behaviour being measured** — presence changes approach
  distance and boldness;
- **the number of trials is bounded by observer availability** — dawn peaks, one
  person, one site per morning;
- **a response scored by eye leaves no record** to re-analyse, re-score, or check for
  inter-observer agreement.

Motion-triggered playback modules already exist, but they start the sound *after* an
animal arrives. That models a cue delivered on encounter — not an intrusion broadcast
to draw the territory holder in.

## The design decision that matters

**Detection and recording never interrupt playback.**

This is the point of the experiment: the "intruder" must keep singing while the
resident arrives to investigate. A device that falls silent at the moment of detection
changes the stimulus in exactly the second being measured.

Here the camera runs as a single continuous MJPEG stream for the whole window. The
detector reads the newest frame from that stream; a clip is written from the same
stream. The audio path is entirely separate and simply never stops.

## What it opens up

- **Repeated exposure over weeks** at one territory with nobody entering it, so
  habituation becomes measurable rather than confounded.
- **Synchronous multi-site trials** — several units run the same dawn, which a
  sequential single-observer design cannot do without confounding site with date.
- **Stimulus-set comparisons** — dialects, heterospecific song, predator calls, masking
  noise: any audio file, identical hardware, identical schedule.
- **A re-usable archive** — stored clips are labelled video for blind re-scoring or for
  training detection models.

---

## How a cycle runs

1. **RTC alarm** (DS3231) switches the power rail on at the start of a window.
2. Alarms for the next window are **programmed and read back immediately**, before
   anything else starts — so even a window that fails leaves the next wake armed.
3. A power watchdog deadline is handed to a resident daemon.
4. **The window**: the player loops the track in hardware; the camera starts **once**
   and feeds a named pipe; a reader thread splits the MJPEG stream on JPEG markers and
   keeps the newest frame in memory; the NPU scores it about once a second.
5. **Detection** → still frame + a 45 s clip cut from the same stream. Analysis
   continues while recording, so if the bird is still in frame when a clip ends, the
   next clip starts on the very next frame — chained, with no gap.
6. **Close-out**: stop playback, close the clip, write the RTC alarms, journal and
   `sync`, then cut power through the latching switch.

Each subsystem is isolated: no audio module → the window runs silent; stream failure →
up to three restarts; an exception inside the window never skips close-out.

### Recovery daemon

When I/O jams on this SoC you cannot launch *any* process — not even `reboot`. A
resident daemon runs from boot with its pages locked in RAM (`mlockall`) and reboots
through the raw syscall via `ctypes`, without `exec`. It reacts to a stale heartbeat, an
explicit request, and a missed window deadline (which triggers a hard power cut — the
only protection for the cell if the main program hangs unrecoverably in the kernel).

Heartbeats and deadlines use `CLOCK_MONOTONIC`, not wall clock: the system clock jumps
by years on cold boot, which an earlier wall-clock version read as a five-year-old
heartbeat and rebooted on every wake.

---

## Measured on hardware

| | |
|---|---|
| Capture | 2304×1296, 30 fps MJPEG, one pipeline start per window |
| NPU inference (YOLOv5, int8) | **0.29 s** |
| Analysis rate | ~1/s, CPU 72 % idle |
| Clip | 45 s at 15 fps; 38–80 MB depending on scene detail |
| Power, running | **~1.6 W** |
| Power, between windows | **~10 mW** |
| Longest verified continuous window | 2 h, 7,175 analyses, no pipeline restart |
| Detection confidence, real bird image | 0.40–0.70, stable across consecutive frames |

### One platform lesson worth repeating

An earlier version started the camera pipeline for **every** photo. After ~20
start/stop cycles of VI/ISP/VENC the whole system lost I/O — the SD card path first,
then the NAND rootfs going read-only and processes dying with `SIGBUS`. Bisection on
freshly booted hardware:

| Test | Result |
|---|---|
| Photo loop, SD card **unmounted** | fails after ~100 s |
| One continuous stream, 250 s | clean |
| **21× start/stop pipeline**, no storage, no NPU | **hang on the 22nd** |
| Continuous stream + 203 MB written to card in 4 min | clean |

The pipeline restarts were the cause — not the card, not the writes, not the NPU. Hence
one stream per window, which also happens to be what makes uninterrupted playback
possible.

Separately: the stock RKNN demo encoded a full-size annotated PNG on every inference,
which the trap never reads. Removing that write cut inference from 2.41 s to 0.29 s —
**88 % of inference time** was spent on an image thrown away. See
[`docs/rknn-demo-no-image-write.patch`](docs/rknn-demo-no-image-write.patch).

---

## Status

**Built and bench-tested.** Verified on hardware:

- full unattended daily cycle: self-wake → window → self-cutoff → next self-wake
- real detection → still → clip → chained clips → chain break when the bird leaves
- windows of 15, 30, 60 and 120 minutes, all closing cleanly
- recovery daemon: power cut on missed deadline, no false reboot across a 4-year clock
  jump, reboot on a genuinely stale heartbeat

**Not yet done:** any field deployment; false-positive rate in moving vegetation;
solar-charging balance; detection performance on wild birds in motion.

## Known limitations

- **No audio recording of the response.** The unit plays but does not listen. Song rate
  — a standard playback metric — is not captured. This is the largest functional gap;
  the platform has audio input, so it is an extension rather than a redesign.
- **No species identification.** The detector uses the generic COCO `bird` class. For
  most playback designs this is enough — the stimulus is species-specific and the site
  is known.
- **Two windows per day**, because the DS3231 has two hardware alarms.
- **No approach distance** from a single fixed camera; presence, timing and in-frame
  behaviour only.
- The confidence threshold is deliberately set for sensitivity over precision: a false
  positive costs one clip on a card with room for hundreds, a miss costs a whole event.

---

## Repository layout

| Path | Contents |
|---|---|
| `src/` | The complete trap: cycle driver, RTC, audio, recovery daemon |
| `hardware/BOM.md` | Parts list, wiring, and the power path |
| `docs/build-notes.md` | Cross-compilation environment |
| `docs/rknn-demo-no-image-write.patch` | The inference speed-up |
| `README.pl.md` | Full engineering documentation (Polish), including measurements and false leads |

Vendor SDKs (Rockchip media stack, RKNN toolkit, toolchains) are **not** included —
only our patch against them. See `docs/build-notes.md` for how to obtain and build.

## Running it

```bash
python3 birdbox_main.py                      # production: window per schedule, then power off
python3 birdbox_main.py --test               # keeps power on, keeps every frame
python3 birdbox_main.py --test --window 900  # 15-minute window starting now
python3 birdbox_main.py --force-window       # window outside the schedule
```

Windows are configured in a plain text file on the card (`HH:MM-HH:MM`, one per line),
so the schedule can be changed in the field without a console. Creating a file named
`STOP` on the card halts the automation the same way.

## Funding

This work was funded by the **Study@research** programme (Initiative of Excellence – Research University, ID UB), Adam Mickiewicz University, Poznań. Project number: **201/34/UAM/0068**.
Please quote the grant identifier when citing or reporting on this project.

## License

MIT — see [LICENSE](LICENSE). The RKNN demo patch applies to Rockchip's sample code,
which carries its own license.
