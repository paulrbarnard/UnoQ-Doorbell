#!/usr/bin/env python3
"""Fit a colour correction matrix (CCM) for the soft ISP from a ColorChecker photo.

    python colorchecker.py image.jpg --grid grid.json [--rot180] [--out overlay.jpg]

grid.json gives patch centres in image pixels as 4 rows x 6 columns, top to
bottom and left to right as the patches appear in the image. --rot180 means the
card was held upside down (grey row at the top).

The image is assumed to be the soft ISP's output: white-balanced, then gamma
2.2 (the "Adjust" algorithm's default), with no CCM. It's linearised, the
grey patches are balanced to neutral, and a 3x3 CCM whose rows sum to 1 (so
white stays white) is fitted by least squares against the reference linear
sRGB values. Prints the CCM (in R,G,B order, as the libcamera tuning file
wants) and the error before and after.
"""

from __future__ import annotations

import argparse
import json

import cv2
import numpy as np

# X-Rite ColorChecker Classic (post-2014), sRGB D65 8-bit, standard order:
# rows top to bottom, patches left to right, card the right way up.
REFERENCE_SRGB = np.array([
    [115, 82, 68], [194, 150, 130], [98, 122, 157], [87, 108, 67], [133, 128, 177], [103, 189, 170],
    [214, 126, 44], [80, 91, 166], [193, 90, 99], [94, 60, 108], [157, 188, 64], [224, 163, 46],
    [56, 61, 150], [70, 148, 73], [175, 54, 60], [231, 199, 31], [187, 86, 149], [8, 133, 161],
    [243, 243, 242], [200, 200, 200], [160, 160, 160], [122, 122, 121], [85, 85, 85], [52, 52, 52],
], dtype=np.float64)
NAMES = [
    "dark skin", "light skin", "blue sky", "foliage", "blue flower", "bluish green",
    "orange", "purplish blue", "moderate red", "purple", "yellow green", "orange yellow",
    "blue", "green", "red", "yellow", "magenta", "cyan",
    "white", "neutral 8", "neutral 6.5", "neutral 5", "neutral 3.5", "black",
]
GREYS = list(range(18, 24))


def srgb_to_linear(v: np.ndarray) -> np.ndarray:
    v = v / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0, 1)
    return 255 * np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def lab(srgb8: np.ndarray) -> np.ndarray:
    px = np.clip(srgb8, 0, 255).astype(np.uint8).reshape(-1, 1, 3)[:, :, ::-1]  # RGB -> BGR
    out = cv2.cvtColor(px.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB).reshape(-1, 3)
    return out


def delta_e(a8: np.ndarray, b8: np.ndarray) -> np.ndarray:
    return np.linalg.norm(lab(a8) - lab(b8), axis=1)


def sample(image: np.ndarray, grid: list[list[list[float]]], half: int) -> np.ndarray:
    """Median RGB of a (2*half+1)^2 box at each centre, in image order (row-major)."""
    out = []
    for row in grid:
        for x, y in row:
            x, y = int(round(x)), int(round(y))
            box = image[y - half : y + half + 1, x - half : x + half + 1].reshape(-1, 3)
            out.append(np.median(box, axis=0)[::-1])  # BGR -> RGB
    return np.array(out, dtype=np.float64)


def to_standard_order(samples: np.ndarray, rot180: bool) -> np.ndarray:
    grid = samples.reshape(4, 6, 3)
    if rot180:
        grid = grid[::-1, ::-1]
    return grid.reshape(24, 3)


def fit_ccm(measured_lin: np.ndarray, reference_lin: np.ndarray) -> np.ndarray:
    """3x3 M with rows summing to 1 minimising |measured @ M.T - reference|.

    Row sum 1 is enforced by fitting the two off-diagonal-style parameters per
    row: out_c = in_c + a*(in_x - in_c) + b*(in_y - in_c).
    """
    ccm = np.zeros((3, 3))
    for c in range(3):
        others = [o for o in range(3) if o != c]
        a = np.stack([measured_lin[:, o] - measured_lin[:, c] for o in others], axis=1)
        y = reference_lin[:, c] - measured_lin[:, c]
        coef, *_ = np.linalg.lstsq(a, y, rcond=None)
        ccm[c, c] = 1 - coef.sum()
        ccm[c, others] = coef
    return ccm


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--grid", required=True)
    ap.add_argument("--rot180", action="store_true")
    ap.add_argument("--half", type=int, default=7, help="sample box half-size in pixels")
    ap.add_argument("--gamma", type=float, default=2.2, help="soft ISP gamma to undo")
    ap.add_argument("--out", help="write an overlay of the sample boxes here")
    ap.add_argument("--flare", action="store_true",
                    help="remove a per-channel offset (veiling glare) fitted on the grey ramp before fitting")
    ap.add_argument("--preview", help="write the image with the fitted correction applied here")
    args = ap.parse_args()

    image = cv2.imread(args.image)
    with open(args.grid) as fh:
        grid = json.load(fh)
    measured8 = to_standard_order(sample(image, grid, args.half), args.rot180)

    if args.out:
        ov = image.copy()
        flat = [p for row in grid for p in row]
        order = np.arange(24).reshape(4, 6)
        if args.rot180:
            order = order[::-1, ::-1]
        std_index = {int(img_i): std_i for std_i, img_i in enumerate(order.reshape(-1))}
        for i, (x, y) in enumerate(flat):
            x, y = int(round(x)), int(round(y))
            cv2.rectangle(ov, (x - args.half, y - args.half), (x + args.half, y + args.half), (0, 0, 255), 1)
            cv2.putText(ov, str(std_index[i] + 1), (x - 8, y - args.half - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        cv2.imwrite(args.out, ov)

    # Linearise the camera output, then balance on the greys (excluding black,
    # which is noisy and pulled by flare).
    meas_lin = (measured8 / 255.0) ** args.gamma
    ref_lin = srgb_to_linear(REFERENCE_SRGB)
    offsets = np.zeros(3)
    if args.flare:
        # Glare adds a roughly constant amount of light to every patch. Fit
        # measured = gain * reference + offset per channel on the greys.
        for c in range(3):
            _, offsets[c] = np.polyfit(ref_lin[GREYS, 1], meas_lin[GREYS, c], 1)
        meas_lin = np.clip(meas_lin - offsets, 1e-6, None)
    g = GREYS[1:5]
    gains = ref_lin[g].mean(axis=0) / meas_lin[g].mean(axis=0)
    gains /= gains[1]
    balanced = meas_lin * gains
    # Match overall exposure to the reference on the greys.
    balanced *= ref_lin[g, 1].mean() / balanced[g, 1].mean()

    ccm = fit_ccm(balanced, ref_lin)
    corrected = balanced @ ccm.T

    before8 = linear_to_srgb(balanced)
    after8 = linear_to_srgb(corrected)
    de_before = delta_e(before8, REFERENCE_SRGB)
    de_after = delta_e(after8, REFERENCE_SRGB)

    print("patch                 measured(sRGB)      balanced         corrected        reference     dE before -> after")
    for i in range(24):
        print(f"{i+1:2d} {NAMES[i]:15s} {np.round(measured8[i]).astype(int)!s:18s} {np.round(before8[i]).astype(int)!s:16s} "
              f"{np.round(after8[i]).astype(int)!s:16s} {REFERENCE_SRGB[i].astype(int)!s:14s} {de_before[i]:5.1f} -> {de_after[i]:5.1f}")
    print(f"\nmean dE (Lab, 0-100 L scale): before {de_before.mean():.1f}  after {de_after.mean():.1f}")
    print(f"colour patches only (1-18): before {de_before[:18].mean():.1f}  after {de_after[:18].mean():.1f}")
    print(f"grey balance gains applied (R,G,B): {np.round(gains, 3)}")
    if args.flare:
        print(f"glare offsets removed (linear R,G,B): {np.round(offsets, 4)}")
    print("\nCCM (R,G,B rows, rows sum to 1):")
    for row in ccm:
        print("  [" + ", ".join(f"{v: .4f}" for v in row) + "]")
    if args.preview:
        lin = (image[:, :, ::-1].astype(np.float64) / 255.0) ** args.gamma
        lin = np.clip(lin - offsets, 0, None) * gains
        lin *= ref_lin[g, 1].mean() / (meas_lin[g, 1] * gains[1]).mean()
        out = linear_to_srgb(lin @ ccm.T)
        cv2.imwrite(args.preview, np.round(out[:, :, ::-1]).astype(np.uint8))
    print("\nccm: [ " + ",\n       ".join(", ".join(f"{v:.4f}" for v in row) for row in ccm) + " ]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
