import numpy as np

from doorbell.camera import GrayWorld, colour_matrix, display_size


def test_black_floor_and_white_balance():
    img = np.full((1232, 1632, 3), (140, 120, 130), np.uint8)  # BGR, tinted grey scene
    for ys in (slice(0, 60), slice(-60, None)):
        for xs in (slice(0, 60), slice(-60, None)):
            img[ys, xs] = (59, 10, 47)  # soft ISP's magenta "black" outside the lens circle
    gw = GrayWorld(every=1, smoothing=1.0)
    out = gw(img)
    assert out[5, 5].max() <= 1  # unlit corners back to black
    centre = out[616, 816].astype(int)
    assert centre.max() - centre.min() <= 2  # grey comes out neutral


def test_colour_matrix_is_bgr_and_keeps_grey():
    m = colour_matrix([[1.9555, -1.0818, 0.1263], [-0.8341, 2.1957, -0.3615], [-0.0155, -1.7617, 2.7772]])
    assert np.allclose(m[0], [2.7772, -1.7617, -0.0155])  # B row, columns B,G,R
    assert np.allclose(m.sum(axis=1), 1, atol=1e-4)  # rows sum to 1: grey stays grey
    assert colour_matrix(None) is None


def test_display_size_follows_rotation():
    landscape = np.zeros((1232, 1632, 3), np.uint8)
    portrait = np.zeros((1632, 1232, 3), np.uint8)
    assert display_size(landscape, 960) == (960, 725)
    assert display_size(portrait, 960) == (725, 960)
