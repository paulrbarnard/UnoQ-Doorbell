#!/usr/bin/env python3
"""Calibrate the soft ISP's CCM from raw IMX219 frames of a ColorChecker.

    python calibrate_raw.py chart.raw --grid grid.json [--flare] [--preview out.jpg]

chart.raw: packed 10-bit RGGB frames from
    v4l2-ctl -d /dev/video0 --set-fmt-video=width=1640,height=1232,pixelformat=pRAA \
             --stream-mmap --stream-count=8 --stream-to=chart.raw
(run `cam` once first so libcamera sets up the binned mode on the media graph).
grid.json: patch centres on the half-size (820x616) image, 4 rows x 6, card
the right way up.

The raw data is truly linear, so this models the soft ISP exactly as it runs:
black level -> white balance gains (from the greys) -> CCM -> gamma. The CCM
is fitted with rows summing to 1, so white stays white.
"""

from __future__ import annotations

import argparse
import json

import cv2
import numpy as np

import colorchecker as cc
import rawbayer as rb

BLACK = 64.0  # measured with the lens covered, 2026-10-10 (all four channels)
WHITE = 1023.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw")
    ap.add_argument("--grid", required=True)
    ap.add_argument("--half", type=int, default=4)
    ap.add_argument("--flare", action="store_true", help="remove a per-channel glare offset fitted on the greys")
    ap.add_argument("--gamma", type=float, default=2.2, help="gamma the soft ISP applies after the CCM")
    ap.add_argument("--preview", help="write the simulated ISP output with this CCM here")
    ap.add_argument("--gamma-domain", action="store_true",
                    help="fit a CCM for the gamma-encoded picture (what camera.ccm in doorbell.yaml wants) "
                         "instead of the linear one for the soft ISP's Ccm algorithm")
    args = ap.parse_args()

    frames = rb.frames(args.raw)
    rgb = np.mean([rb.half_rgb(f) for f in frames[2:] or frames], axis=0)  # average frames, less noise
    lin_img = np.clip((rgb - BLACK) / (WHITE - BLACK), 0, None)
    with open(args.grid) as fh:
        grid = json.load(fh)

    meas = []
    for row in grid:
        for x, y in row:
            box = lin_img[y - args.half : y + args.half + 1, x - args.half : x + args.half + 1].reshape(-1, 3)
            meas.append(np.median(box, axis=0))
    meas = np.array(meas)
    raw_max = np.array([rgb[y - args.half : y + args.half + 1, x - args.half : x + args.half + 1].max() for row in grid for x, y in row])
    if (raw_max >= WHITE - 2).any():
        print("WARNING: clipped patches:", [cc.NAMES[i] for i in np.where(raw_max >= WHITE - 2)[0]])

    ref = cc.srgb_to_linear(cc.REFERENCE_SRGB)
    greys = cc.GREYS  # white .. black
    offsets = np.zeros(3)
    print("grey ramp, raw linear (R G B) vs reference:")
    for i in greys:
        print(f"  {cc.NAMES[i]:12s} {np.round(meas[i], 4)}  ref {ref[i, 1]:.4f}")
    for c in range(3):
        slope, off = np.polyfit(ref[greys, 1], meas[greys, c], 1)
        offsets[c] = off
    print(f"per-channel offset on the grey ramp (fraction of full scale): {np.round(offsets, 4)}")
    if args.flare:
        meas = np.clip(meas - offsets, 1e-6, None)
        lin_img = np.clip(lin_img - offsets, 0, None)

    g = greys[1:5]  # neutral 8 .. 3.5 (white may be near clipping, black is noisy)
    wb = meas[g].mean(axis=0)
    gains = wb[1] / wb  # green = 1, as libcamera AWB does
    balanced = meas * gains
    expo = ref[g, 1].mean() / balanced[g, 1].mean()
    balanced *= expo
    def to8(lin):  # soft ISP: power-law gamma
        return 255 * np.clip(lin, 0, 1) ** (1 / args.gamma)

    if args.gamma_domain:
        # Fit on the gamma-encoded ISP output against reference sRGB. Cheap to
        # apply (one uint8 cv2.transform) and as accurate on the chart.
        enc = to8(balanced) / 255
        ccm = cc.fit_ccm(enc, cc.REFERENCE_SRGB / 255.0)
        corrected = np.clip(enc @ ccm.T, 0, 1) ** args.gamma  # back to linear for reporting
    else:
        ccm = cc.fit_ccm(balanced, ref)
        corrected = balanced @ ccm.T

    de_before = cc.delta_e(to8(balanced), cc.REFERENCE_SRGB)
    de_after = cc.delta_e(to8(corrected), cc.REFERENCE_SRGB)
    print("\npatch               WB only        WB + CCM       reference     dE before -> after")
    for i in range(24):
        print(f"{i+1:2d} {cc.NAMES[i]:15s} {np.round(to8(balanced[i])).astype(int)!s:14s} {np.round(to8(corrected[i])).astype(int)!s:14s} "
              f"{cc.REFERENCE_SRGB[i].astype(int)!s:13s} {de_before[i]:5.1f} -> {de_after[i]:5.1f}")
    print(f"\nmean dE: WB only {de_before.mean():.1f}  with CCM {de_after.mean():.1f}  "
          f"(colour patches: {de_before[:18].mean():.1f} -> {de_after[:18].mean():.1f})")
    print(f"WB gains (R,G,B) for this light: {np.round(gains, 3)}   R/G={1/gains[0]:.3f} B/G={1/gains[2]:.3f}")
    print("CCM rows (R,G,B):")
    for row in ccm:
        print("  [" + ", ".join(f"{v: .4f}" for v in row) + "]")

    if args.preview:
        base = lin_img * gains * expo
        out = np.clip(to8(base) / 255 @ ccm.T, 0, 1) * 255 if args.gamma_domain else to8(base @ ccm.T)
        side = np.hstack([to8(lin_img * gains * expo), np.full((out.shape[0], 6, 3), 255), out])
        cv2.imwrite(args.preview, np.round(side[:, :, ::-1]).astype(np.uint8))
    np.save(args.raw + ".ccm.npy", ccm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
