# CLAUDE.md

Video doorbell App Lab app for the Arduino UNO Q + Media Carrier with a 200°
IMX219 camera. Design: `docs/architecture.md`. Experiment log:
`docs/findings.md`. Keep the status table in `README.md` current.

## Board access
- Test board **Valentina**: `ssh valentina` (alias in ~/.ssh/config: arduino@192.168.0.115,
  key `id_ed25519_valentina`). After a reflash the host key changes and
  `authorized_keys` is wiped. Run `ssh-keygen -R`, then have the user run
  `ssh-copy-id -i ~/.ssh/id_ed25519_valentina.pub arduino@192.168.0.115`.
  Never disable host-key checking permanently.
- `sudo` needs a password on the board. Use `sudo -n` in scripts, and ask the
  user to run anything that really needs root.
- The board has no rsync or gst-launch on the host. GStreamer and libcamera
  0.7 live in the App Lab container (`python-apps-base`), which has a newer
  libcamera than the host.
- Camera-driver and kernel work belongs in the sibling repo `../UnoQ-Camera`, not here.

## Deploy and run
- `scripts/deploy.sh [--homekit]` streams a tar over ssh to
  `~/ArduinoApps/unoq-doorbell` and runs `arduino-app-cli app restart`.
- App logs: `docker logs unoq-doorbell-main-1`. HomeKit:
  `docker logs doorbell-homekit-hap-1`.
- Never overwrite or delete `data/` (events, config overrides) or
  `homekit/state/` (HomeKit pairing keys) on the board. Losing the pairing
  state means re-adding the accessory in the Home app.

## Conventions
- Capture is always 1632x1232 (binned, full FOV). Smaller or 1640-wide
  requests crop (see findings). Downscale with `INTER_AREA`.
- The Python-side `Bridge.call` is synchronous (no `.result()`). The sketch
  uses `Bridge.notify` for button presses.
- Keep `visits.py` and `doorstep.py` pure and covered by `python/tests/`.
  Run `.venv/bin/python -m pytest python/tests`.
- The dev venv pins OpenCV < 5 to match the container (4.13).
- Record every board experiment in `docs/findings.md` with its date.
