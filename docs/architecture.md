# Architecture

```
 IO2 button ──► MCU sketch ──Bridge.notify("bell_pressed")──┐
                                                            ▼
 IMX219 200° ─► FrameSource (1632x1232, 10 fps) ──► DoorbellService ──► EventStore (data/events)
                 │   gray-world WB                  ▲   │  VisitTracker      │
                 │                                  │   │  doorstep.compare  ├─► Pushover push (iPhone)
                 ├─► Detector (approach-zone crop,  │   │                    │
                 │    letterbox → YOLOX-nano) ──────┘   └─ socket.io "bell"/"delivery"/"event"
                 │                                                  │
                 └─► /stream.mjpg, /api/live.jpg (960x724) ─────────┤
                                                                    ▼
                        web page (assets/)        HomeKit companion (homekit/, host network)
                                                  Video Doorbell + "Delivery" motion sensor
```

## Processes

| What | Where | Why there |
|---|---|---|
| `sketch/sketch.ino` | STM32U5 MCU | debounces the button on IO2, notifies Linux |
| `python/main.py` | App Lab container `unoq-doorbell-main-1` (bridge network, port 7000) | camera, detection, logic, web page |
| EI models runner | `unoq-doorbell-ei-obj-detection-runner-1`, started by the `object_detection` brick | YOLOX-nano inference |
| `homekit/hap_doorbell.py` | `doorbell-homekit-hap-1`, `docker compose`, **host network**, `restart: unless-stopped` | HomeKit needs Bonjour/mDNS on the LAN, which the App Lab bridge network can't provide |

The companion connects to the app on `127.0.0.1:7000`. It takes snapshots from
`/api/live.jpg`, live video from `/stream.mjpg` (ffmpeg → H.264 → SRTP to the
iPhone), and events from socket.io. It reports its pairing state to
`POST /api/homekit`.

## Camera: why not the Arduino `Camera` class

`Camera(resolution=(640, 480))` asks libcamera for 640x480. The soft ISP gets
there by cropping, which throws away most of the fisheye image. `FrameSource`
builds the same `libcamerasrc` pipeline (using the brick's own
`csi_camss_discovery.gstreamer_source`) but always asks for the binned
1632x1232 mode, then resizes with `INTER_AREA`. See docs/findings.md.

## Visits and deliveries

`visits.VisitTracker` is a pure state machine (tested in `python/tests/`):

- **arrived**: a person's feet (bottom-centre of the box) are in the
  *approach zone* in 3 of the last 5 detections, or the bell was pressed.
- **left**: no one in the zone for `leave_after_s` (8 s).
- **closed**: `settle_s` (3 s) later. The *doorstep zone* is photographed
  again and compared with the last empty-doorstep reference from before the
  visit.

`doorstep.compare` works in Lab colour, normalises lightness, then thresholds
and opens the mask. If more than `changed_fraction` of the zone changed, a
parcel was left.

| Result | Meaning | Push | HomeKit |
|---|---|---|---|
| bell | button pressed (sent at once) | yes | Doorbell ring → HomePod chime/announce, iPhone rich notification |
| delivery | parcel left, bell or not | yes | "Delivery" motion sensor pulses for 30 s |
| visitor | rang, left nothing | summary only if `on_visit` | — |
| caller | came, no bell, no parcel | only if `on_visit` | — |
| ignored | shorter than `min_dwell_s` | — | — |

Zones are fractions of the full frame, set by dragging on the web page.
Detection runs on a crop of the approach zone, so a tight zone gives the model
more pixels per person.

## Data on the board

`~/ArduinoApps/unoq-doorbell/data/`: `config.yaml` (web page overrides of
`python/config/doorbell.yaml`) and `events/<id>/{event.json,*.jpg}`, pruned
after 14 days or 300 events. `homekit/state/` holds the pairing keys and
setup code. `scripts/deploy.sh` never overwrites either.
