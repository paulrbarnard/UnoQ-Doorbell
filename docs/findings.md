# Findings

Each experiment on the board: date, setup, what was tried, and the result.

## 2026-09-25: camera modes, crop vs scale (Valentina)

**Setup:** IMX219 (8MP) module with a 200° fisheye lens on camera0 of the Media
Carrier. App Lab container `python-apps-base:0.12.0` (its libcamera is 0.7.1
with the `camss` pipeline handler and Software ISP; the host has 0.4).

**Sensor modes** (`v4l2-ctl --list-subdev-framesizes` on the imx219 subdev):
3280x2464, 1920x1080, 1640x1232, 640x480.

**Test:** `python/tools/probe_camera.py` in the container. It uses the same
`libcamerasrc ! video/x-raw,width=W,height=H ! appsink` pipeline shape as
arduino's `CSICamera`.

| Requested | Delivered | Field of view |
|---|---|---|
| 3280x2464 | failed to open | — |
| 1640x1232 | 1640x1232, ~15 fps | **centre crop** of the full-res mode |
| **1632x1232** | 1632x1232 | **full 200° circle** (2x2 binned) |
| 1600x1200 | 1600x1200 | nearly full (small crop of the binned mode) |
| 820x616 | 820x616 | centre crop of the binned mode |
| 640x480 | 640x480 | centre crop |

**Why:** the Software ISP drops an 8 px border when debayering (3280→3272,
1640→1632). A request for 1640 wide no longer fits the binned mode, so
libcamera picks the full-res mode and crops. Anything smaller than the binned
mode is also served by cropping, because the soft ISP has no scaler. The app log
confirms the pick: `SoftwareIsp: Input 1640x1232-RGGB-10-CSI2P` for a
1632x1232 request.

**Decision:** always capture 1632x1232 and downscale in OpenCV (`INTER_AREA`)
for the live view, snapshots and detection. Set in
`python/config/doorbell.yaml` → `camera.capture_size`.

**Colour:** there's no `imx219.yaml` tuning, so the soft ISP uses
`uncalibrated.yaml` and frames come out purple/green. A gray-world white
balance in `camera.py` fixes most of it.

**First open after the probe:** `dma-heap allocation failure` on the first
attempt, then it worked on retry. `FrameSource` retries with backoff.

## 2026-09-25: load and H.264 for HomeKit live view

- At 10 fps capture: the app process uses about 130% CPU (soft ISP debayer
  ~22 ms/frame, WB, JPEG), YOLOX-nano about 90% (~520 ms/inference, ~2 fps).
  Load average is ~8 on 4 cores during live view.
- The MJPEG input to ffmpeg has no timestamps, so ffmpeg assumed 25 fps and
  ran at 0.45x. `-use_wallclock_as_timestamps 1` fixes it: 1280x720 @15 with
  libx264 ultrafast runs at 1.4x realtime (~120% CPU while someone watches).
- Venus hardware encoder `h264_v4l2m2m`: rejects yuv420p (EINVAL), works with
  NV12 at 640x480, **segfaults at 1280x720**. Stay on libx264 for now.

## 2026-09-25: HomeKit companion

- HAP-python 4.9.2 in `homekit/` (host network). Advertised as `Front Door`
  (`_hap._tcp`), seen from the Mac with `dns-sd -B _hap._tcp`.
- ~~`set_value()` only notifies on a change, so `ring()` calls `notify()`
  explicitly~~ **Wrong, corrected 2026-10-02.** HAP-python special-cases
  `ProgrammableSwitchEvent` (`ALWAYS_NULL`): `set_value(0)` sends 0, then
  resets the value to null, so every ring counts as a change. The
  `set_value(..., should_notify=False)` + `notify()` "fix" sent `value: null`
  to the hub, which ignored it: no chime and no notification even though
  the hub was subscribed. `ring()` is back to a plain `set_value(0)`.
- HAP-python makes a new random setup code on every start unless one is
  passed in. It's now kept in `homekit/state/setup_code`.
- Simulated bell and delivery from the web page reach the companion
  (`ring` / `delivery` in its log). Not yet tested: pairing, live view on the
  iPhone, HomePod announce.

## 2026-10-02: HomeKit companion dead after a power cut

- The board rebooted on 2026-09-27 (no clean shutdown). Docker restarted the
  companion, but it wasn't listening on 51826 and wasn't advertised. Most
  likely it started before 192.168.0.115 was assigned, so the HAP bind failed.
  HAP-python logs the failure and keeps its event loop running, so the
  container stayed "Up" and was never restarted.
- The original error was lost: "app not reachable" every 5 s (the App Lab app
  wasn't set to start at boot) filled the 2×5 MB log.
- Fixes: `wait_for_address()` before starting the driver; a watchdog that
  exits if the HAP port stops answering (Docker restarts it); reconnect
  warnings now rate-limited; the app set as the App Lab default
  (`arduino-app-cli properties set default ...`) so it starts at boot.

## 2026-10-04: board down ~44 h after two crashes on 2026-10-02

- Unclean ends at about 15:47 UTC and 16:30 UTC on 2026-10-02. The board then
  stayed down until it was powered up again on 2026-10-04 at about 13:53 UTC.
  The journal just stops: no OOM, panic, watchdog or thermal messages.
  Temperatures after restart are 45–56 °C. Docker's json log was corrupted
  (`\x00`), which fits a sudden power loss.
- Clue: at 16:18 UTC on 2026-10-02 the kernel logged `anx7625 ... cannot access
  interrupt change reg` (the USB-C bridge) plus pm4125 SoundWire parity errors.
  Suspect: the USB-C power supply under full load (soft ISP + YOLO + ffmpeg live
  view use all 4 cores). Not confirmed.
- With no RTC, the board boots with the clock at its last saved time until NTP
  syncs. Boot records and container uptimes look wrong after a power cut;
  trust `/proc/uptime`.
- Separate bug found in the logs: once a visit reached `max_visit_s` (900 s,
  someone sitting at the desk), the tracker flipped left/returned on every
  detection. Fixed: a capped visit now closes, and a new one starts.

## 2026-10-10: colour, first pass from a ColorChecker snapshot

- Source: the bell event `20261010-142928-bell`, a ColorChecker Classic held upside
  down, as a 960x724 JPEG (soft ISP output with no CCM, then the app's
  gray-world WB). Tool: `python/tools/colorchecker.py`.
- Very low saturation. A plain CCM fit (rows sum to 1) gives
  `[1.99 -0.69 -0.30; -0.56 1.88 -0.32; -0.38 -0.34 1.72]`, close to the
  Raspberry Pi imx219 CCMs. Mean ΔE goes from 24.7 to 14.9 (colour patches
  29.8 → 14.7).
- The grey ramp is compressed and offset: black reads 74 (ref 52), white 182
  (ref 243), with a large per-channel offset (~6–8% linear, most in blue).
  This is most likely veiling glare from the window behind the card, through
  the fisheye. Applying the CCM turns the shadows purple. Fitting with the
  offset removed is unstable (shadows clip, CCM gets positive cross-terms).
- The soft ISP supports a CCM (`Ccm: ccms: - ct: … ccm: […]`) and a
  per-sensor tuning file `simple/imx219.yaml`, found through
  `LIBCAMERA_IPA_CONFIG_PATH`. Nothing is installed yet. The next step is
  calibrating from raw Bayer frames.

## 2026-10-10: colour calibration from raw frames

- **Raw capture.** The host's libcamera 0.4 `cam` quietly turns `role=raw` into a
  processed ABGR8888 stream. To get real raw: run `cam` once (it sets up the
  binned 1640x1232 mode on the media graph), then
  `v4l2-ctl -d /dev/video0 --set-fmt-video=width=1640,height=1232,pixelformat=pRAA --stream-mmap ...`
  (10-bit packed RGGB, stride 2056). If STREAMON fails with "Broken pipe",
  run `cam` again. Tools: `python/tools/rawbayer.py`, `calibrate_raw.py`.
- **Black level.** 64 (10-bit) in all four channels, measured with the lens
  covered at max gain (exposure 1703, analogue gain 232). Set explicitly in
  `python/config/libcamera/simple/imx219.yaml` (`blackLevel: 4096`). libcamera
  loads it through `LIBCAMERA_IPA_CONFIG_PATH` (main.py): "Using tuning file
  /app/python/config/libcamera/simple/imx219.yaml".
- **Fit.** ColorChecker right way up, indoor daylight. The greys come out
  neutral after WB (gains R 1.28, B 1.98). Raw glare is small (2–4%).
  Linear CCM: mean dE 23.3 → 10.8. Blue, purplish blue, blue flower, foliage,
  dark skin and purple stay too light: those patches reflect strongly in
  near-IR, so the lens probably has **no IR-cut filter**.
- **CCM in the soft ISP is too slow.** Debayer goes from ~22–35 to 51 ms/frame
  and the app from ~260% to ~275+% CPU. Left disabled in the tuning file.
  Instead a gamma-domain CCM (fitted with `--gamma-domain`, mean dE 9.5) is
  applied with one uint8 `cv2.transform` (6 ms per 960x724 frame) to viewed
  pictures only: live view, snapshots, HomeKit. It's `camera.ccm` in
  doorbell.yaml. Detection and the doorstep diff use uncorrected frames.
- **Two problems the CCM made visible, both fixed in `GrayWorld`.**
  1. The soft ISP leaves a **magenta black floor**: unlit pixels outside the
     fisheye circle come out at about R 47, G 10, B 59 (8-bit), while the raw
     there is neutral (~9 counts of glare per channel after WB). The cause in
     the ISP is unknown. Fix: those corners serve as a black reference, and
     their level is subtracted per channel in linear light.
  2. The lens's outer ring is purple (colour shading). Gray-world over the
     whole frame pushed the centre green. Fix: measure WB on the centre
     (r < 0.6) and on mid-tones only.
  Result: neutral walls and door, black blacks, natural skin and wood.
- `GET /api/live.jpg?ccm=0` returns the uncorrected picture, for calibration.
- **CPU (not caused by the colour work).** The app process runs at ~260% with
  nobody watching: soft ISP debayer threads ~105%, GStreamer videoconvert ~20%,
  Python ~150%. Worth profiling separately, given the crashes on 2026-10-02.

## 2026-10-12: unreachable after moving to the porch (Orbi mesh)

- After Valentina was fitted in the porch, it joined a different Orbi satellite
  (5 GHz, signal 45–49%). Its mDNS announcements
  still reached the LAN, but unicast to it failed from the wired Mac and from
  Steffi (Wi-Fi). Only the iPhone could load the page. Most likely the mesh
  kept a stale path to the old satellite. A power cycle fixed it.
- Wi-Fi power saving was at the driver default. Now disabled on the connection:
  `nmcli con modify "<home Wi-Fi>" 802-11-wireless.powersave 2` (persists;
  undo with `... powersave 0`). No root needed: NetworkManager has
  `auth-polkit=false`.
- Self-healing: `scripts/wifi-watchdog.sh`, run every minute from the arduino
  user's crontab. Pings the gateway; after 3 failed minutes it runs `nmcli con up`,
  after 6 it cycles the radio, then reconnects every 10. Log:
  `journalctl -t wifi-watchdog`. Tested with a fake gateway (counting,
  logging, recovery). The reconnect itself hasn't been seen to fix a real
  outage yet. Also unproven: whether this failure mode breaks the board's own
  pings to the gateway, which is the watchdog's trigger.
- Still to do (user): a DHCP reservation for 192.168.0.115 on the FRITZ!Box.

## 2026-10-15: false visits from backlight in the porch

- After the porch install (18° down-tilt case): 169 "caller" visits from
  2026-10-12 to 2026-10-15 (~50 a day), many running to the 900 s cap. Their
  snapshots show tall, narrow "person" boxes on the stone pillars beside the
  bright doorway, at 0.47–0.50 confidence. Strong backlight turns the
  pillars into silhouettes. Even after the tilt, a 0.38–0.39 ghost remained
  in the empty porch.
- **CLAHE on the detector input** (lightness channel, clip 2.0, 4x4 tiles,
  after the letterbox downscale, so cheap) removed the ghosts in the empty
  porch and on a stored real-person snapshot (its 0.37 ghost went). Real
  people: 0.88→0.87 and 0.84→0.82 (well lit); backlit people 0.40→0.78 and
  0.44→0.56. Detector input only; the picture people see is unchanged.
- **Threshold 0.45 → 0.50.** Note: saving the web page's settings form
  stores every field in data/config.yaml, so the board had 0.45 pinned there.
  Updated through the API.
- **Static filter** (`doorbell/static.py`): a detection whose box centre stays
  within 2% for 60 s is ignored until it hasn't been seen for 10 min. A safety
  net for whatever else the scene throws up. A visitor standing perfectly
  still for over a minute would also be dropped, but fidgeting resets it.
- **Detection log:** `data/detections.jsonl`, one line per frame with any
  detection ≥ 0.3, each marked kept/static/low, for tuning.
- Result right after deploy: empty porch → no detections at all (not even
  ≥ 0.3). Still to check: a real walk-up with these settings.

## 2026-10-15: false delivery when leaving the house

- 11:32 local: the occupants walked out through the front door, giving a
  "caller" visit and then a "delivery" (doorstep change 13.5%). The before and
  after doorstep crops show the same empty mat. The far end of the zone (the
  sunlit path beyond the porch doorway) went almost white in "after":
  auto-exposure had opened up with dark-clothed people close to the lens, or
  the sun came out. The real test parcel at 11:22 scored 12.6%, so no
  threshold could separate the two.
- Tried and rejected: block-wise structure comparison (normalised correlation
  plus a texture-loss test). Overexposure flattens texture too, so the false
  case got *worse* (21–23%).
- Fix in `doorstep.compare`: ignore pixels that were already near-white
  *before* the visit (Lab L >= 215, `doorstep.highlight`), and match lightness
  with the median over the remaining pixels. Real parcel 10.6% → parcel;
  leaving 2.5% → no parcel (threshold 6%). A white parcel on the darker mat
  still counts, because only the before brightness is checked. A parcel placed
  on a spot that was in full sun beforehand could be missed.
- The detection log also showed a second narrow ghost (aspect ~0.2) from
  11:33, flickering at 0.30–0.56. Because only detections ≥ 0.5 reached the
  static filter, its occasional crossings never formed a track. The static
  filter now tracks all detections (≥ 0.3) and the threshold is applied after.

## 2026-10-15: post-box deliveries ("post")

- 12:27 and 13:15 local: couriers put parcels in the post box on the right
  wall. Nothing on the mat, so both were logged as "caller". The detection log
  shows their person boxes covering the post-box area (0.43–0.54 x,
  0.62–0.85 y) in 9 and 14 detections (max overlap 0.87 / 0.95). Other visits
  that day: occupants leaving 1 detection (0.69), everyone else 0.
- New: an optional `zones.postbox`. A visit where a person box covers ≥ 15%
  of it in ≥ 4 detections (`post.min_overlap`, `post.min_frames`, ~2 s)
  becomes kind **post**. A parcel on the mat still makes it a delivery. A
  bell press doesn't change it. Alerts: Pushover "Post" (`notify.on_post`,
  `post_sound`), the HomeKit "Delivery" sensor pulses, and the event keeps a
  "postbox" snapshot.
- Not yet seen live. Risk: someone standing in front of the post box for
  more than ~2 s without posting (e.g. waiting after ringing) would read as
  post. If that happens, the next step is to also require the post box itself
  to change (lid opened) against the pre-visit reference.
- **Correction (same day):** 13:15 was the residents arriving home and
  *collecting* the post, not a delivery. Reaching into the post box looks the
  same either way. The difference is direction: the courier started and ended
  out at the porch opening (person box area ~0.02–0.03 of the frame), while
  the residents ended right by the camera at the house door (0.25). Now: post
  requires that the visit does **not** end near the camera (`post.near_area`
  0.10). Each event records `from_house` / `into_house`. Replayed on the day's
  visits: 12:27 → post, 13:15 → caller (into house), the rest unchanged.
