"""Read packed 10-bit RGGB frames (V4L2 'pRAA' / CSI-2 packed) from the IMX219."""

from __future__ import annotations

import numpy as np

W, H, STRIDE = 1640, 1232, 2056


def unpack(buf: bytes, w: int = W, h: int = H, stride: int = STRIDE) -> np.ndarray:
    """One frame -> uint16 Bayer mosaic (0..1023)."""
    rows = np.frombuffer(buf, dtype=np.uint8, count=stride * h).reshape(h, stride)[:, : w * 5 // 4]
    g = rows.reshape(h, w // 4, 5).astype(np.uint16)
    out = np.empty((h, w // 4, 4), dtype=np.uint16)
    for i in range(4):
        out[:, :, i] = (g[:, :, i] << 2) | ((g[:, :, 4] >> (2 * i)) & 3)
    return out.reshape(h, w)


def frames(path: str, w: int = W, h: int = H, stride: int = STRIDE) -> list[np.ndarray]:
    data = open(path, "rb").read()
    size = stride * h
    return [unpack(data[i * size : (i + 1) * size], w, h, stride) for i in range(len(data) // size)]


def half_rgb(mosaic: np.ndarray) -> np.ndarray:
    """RGGB mosaic -> half-size float RGB (no interpolation): R, mean(G1,G2), B."""
    r = mosaic[0::2, 0::2].astype(np.float64)
    g = (mosaic[0::2, 1::2].astype(np.float64) + mosaic[1::2, 0::2]) / 2
    b = mosaic[1::2, 1::2].astype(np.float64)
    return np.dstack([r, g, b])
