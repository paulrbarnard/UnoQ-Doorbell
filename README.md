# UnoQ-Doorbell

A video doorbell on an Arduino UNO Q ("Valentina", 192.168.0.115) with the
Media Carrier and an 8MP IMX219 camera behind a 200° fisheye lens.

- **Bell button on IO2**: HomePods chime and announce, the iPhone gets a
  HomeKit notification with a snapshot, plus a Pushover push that plays a
  chosen (or your own) chime on the iPhone.
- **Delivery detection**: someone walks up, leaves a parcel and goes. You get
  a "Delivery" push with the doorstep photo, and the HomeKit "Delivery" sensor
  fires.
- **Test page** at http://192.168.0.115:7000: live view, zones, status,
  events, settings.

How it fits together: [docs/architecture.md](docs/architecture.md).
Camera and board experiments: [docs/findings.md](docs/findings.md).

## Status

| Part | State |
|---|---|
| Full-FOV capture (1632x1232 binned, OpenCV downscale) | working on the board |
| Web page, live view, events | working on the board |
| Person detection (YOLOX-nano, ~2 fps) | running; needs testing at the real door |
| Visit and parcel logic | unit-tested; thresholds need tuning at the door |
| IO2 button sketch + Bridge | wired and working |
| HomeKit pairing, bell chime/notification | working (paired 2026-10-02) |
| Pushover push | working |
| Colour (black level, CCM, centre-weighted WB) | calibrated 2026-10-10 from raw ColorChecker frames |

## Wiring

Momentary push button between **IO2 (D2)** and **GND**. The sketch uses the
internal pull-up. The built-in LED lights while the button is held.

## Deploy

```sh
scripts/deploy.sh              # copy to the board, restart the App Lab app (also flashes the sketch)
scripts/deploy.sh --homekit    # ...and rebuild/restart the HomeKit companion
```

Wi-Fi watchdog (once, on the board): `scripts/wifi-watchdog.sh` reconnects Wi-Fi
if the gateway stops answering. Install it in the arduino user's crontab:
`(crontab -l; echo "* * * * * $HOME/ArduinoApps/unoq-doorbell/scripts/wifi-watchdog.sh") | crontab -`.
Wi-Fi power saving is off on the connection (see docs/findings.md, 2026-10-12).

The HomeKit companion is a separate `docker compose` project in `homekit/`.
It has `restart: unless-stopped`, so it comes back after a reboot. To start
the App Lab app at boot:
`arduino-app-cli properties set default ~/ArduinoApps/unoq-doorbell`.

## First-time setup

1. **Zones**: open the web page, choose *Edit zone*, and drag:
   - **Approach**: the path right up to the door. People count only when their
     feet are inside it, so passers-by on the street are ignored.
   - **Doorstep**: where parcels get left.
2. **Rotation**: if the camera is mounted upside down, set `camera.rotate: 180`
   in `python/config/doorbell.yaml` and redeploy.
3. **iPhone push (Pushover)**: install **Pushover** on the iPhone and sign in.
   At pushover.net, create an application ("Doorbell") to get an API token.
   Enter the token and your user key under *Settings* on the web page, then
   press *Test push*, which plays the bell sound. For a real chime, upload a
   sound at pushover.net (*Your Custom Sounds*) and enter its name as *Bell sound*.
4. **HomeKit**: in the Home app, choose *Add Accessory → More options → Front
   Door*, and enter the setup code shown on the web page (status → HomeKit).
   It's also in `docker logs doorbell-homekit-hap-1`. Then turn on doorbell and
   "Delivery" sensor notifications for it in the Home app.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r python/requirements-dev.txt
.venv/bin/python -m pytest python/tests
.venv/bin/python python/main.py --mock-camera <folder of jpgs | video> --mock-bridge --port 7071
```

Mock mode uses OpenCV's HOG people detector in place of the Edge Impulse model
and has no socket.io, so the page falls back to polling.
