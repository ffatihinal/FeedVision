"""
FeedVision — screen_calibration.py testleri (SADECE saf matematik kısmı).

Bu modül dosya/kamera G/Ç'si yapmıyor — testler sentetik (numpy ile
üretilen) görüntülerle çalışır, gerçek kameraya/donanıma dokunmaz.
"""

import cv2
import numpy as np
import pytest

from screen_calibration import (
    compute_warp_matrix,
    detect_screen_corners,
    order_points,
    warp_roi_rect,
)


def _make_bezel_frame(size=300, outer=(60, 60, 240, 240), inner=(85, 85, 215, 215)):
    """Beyaz zemin üzerine, dış (outer) ile iç (inner) dikdörtgen arasında
    SİYAH bir çerçeve (bezel) çizer — gerçek HMI'ın kalın siyah çerçevesini
    taklit eder. inner içi tekrar beyaz (ekran içeriği rengi önemsiz)."""
    frame = np.full((size, size, 3), 255, dtype=np.uint8)
    x0, y0, x1, y1 = outer
    cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 0, 0), thickness=-1)
    ix0, iy0, ix1, iy1 = inner
    cv2.rectangle(frame, (ix0, iy0), (ix1, iy1), (255, 255, 255), thickness=-1)
    return frame


class TestOrderPoints:
    def test_orders_shuffled_points_to_tl_tr_br_bl(self):
        # sol-üst, sağ-üst, sağ-alt, sol-alt — karıştırılmış sırayla verildi
        pts = np.array([[100, 100], [0, 0], [100, 0], [0, 100]], dtype=np.float32)
        ordered = order_points(pts)
        np.testing.assert_array_equal(ordered[0], [0, 0])       # sol-üst
        np.testing.assert_array_equal(ordered[1], [100, 0])     # sağ-üst
        np.testing.assert_array_equal(ordered[2], [100, 100])   # sağ-alt
        np.testing.assert_array_equal(ordered[3], [0, 100])     # sol-alt

    def test_order_is_stable_regardless_of_input_order(self):
        pts_a = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        pts_b = np.array([[100, 100], [100, 0], [0, 0], [0, 100]], dtype=np.float32)
        np.testing.assert_array_equal(order_points(pts_a), order_points(pts_b))


class TestDetectScreenCorners:
    def test_none_frame_returns_none(self):
        assert detect_screen_corners(None) is None

    def test_empty_frame_returns_none(self):
        assert detect_screen_corners(np.array([])) is None

    def test_blank_white_frame_returns_none(self):
        # hiçbir koyu bölge/kenar yok — ne primer ne yedek yöntem aday bulmalı
        frame = np.full((200, 200, 3), 255, dtype=np.uint8)
        assert detect_screen_corners(frame) is None

    def test_detects_bezel_corners_approximately(self):
        frame = _make_bezel_frame()
        corners = detect_screen_corners(frame)
        assert corners is not None
        assert corners.shape == (4, 2)
        # sol-üst köşe outer=(60,60) civarında olmalı (birkaç piksel tolerans)
        assert abs(corners[0][0] - 60) <= 5
        assert abs(corners[0][1] - 60) <= 5
        # sağ-alt köşe outer=(240,240) civarında olmalı
        assert abs(corners[2][0] - 240) <= 5
        assert abs(corners[2][1] - 240) <= 5

    def test_too_small_bezel_rejected_by_min_area_ratio(self):
        # kare 300x300=90000, MIN_SCREEN_AREA_RATIO=0.05 -> min_area=4500.
        # 20x20'lik bir kutu (~400px^2 dış alan) bu eşiğin altında kalmalı.
        frame = _make_bezel_frame(outer=(140, 140, 160, 160), inner=(146, 146, 154, 154))
        assert detect_screen_corners(frame) is None


class TestComputeWarpAndRoi:
    def test_identity_transform_preserves_roi(self):
        corners = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        matrix = compute_warp_matrix(corners, corners)
        roi = (10, 10, 20, 20)
        warped = warp_roi_rect(roi, matrix)
        assert warped == pytest.approx(roi, abs=1)

    def test_translation_shifts_roi_accordingly(self):
        ref = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
        cur = ref + np.array([10, 5], dtype=np.float32)
        matrix = compute_warp_matrix(ref, cur)
        roi = (10, 10, 20, 20)
        x, y, w, h = warp_roi_rect(roi, matrix)
        assert x == pytest.approx(20, abs=1)
        assert y == pytest.approx(15, abs=1)
        assert w == pytest.approx(20, abs=1)
        assert h == pytest.approx(20, abs=1)
