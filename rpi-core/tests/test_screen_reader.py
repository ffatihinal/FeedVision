"""
FeedVision — screen_reader.py testleri (SADECE saf goruntu islemesi, OCR
CAGRISI yapilmadan test edilebilen kisimlar).

Sentetik (numpy ile uretilen) goruntulerle calisir, gercek kameraya/Tesseract
binary'sine bagimli degil — read_text_ocr'in kendisi (Tesseract cagrisi)
kasitli olarak test edilmiyor (ortama gore var/yok), sadece "boolean" ROI'lerde
OCR'in hic CAGRILMADIGI dogrulaniyor.
"""

import numpy as np

from screen_reader import (
    BOOLEAN_BRIGHTNESS_THRESHOLD,
    crop_roi,
    read_boolean_state,
    read_roi,
)


def _solid_bgr_image(size: int, value: int) -> np.ndarray:
    """Tum pikselleri ayni gri tonda (value, value, value) olan kare bir goruntu —
    HSV'ye cevrilince V kanali da value'ya esit olur, esik testlerini basitlestirir."""
    return np.full((size, size, 3), value, dtype=np.uint8)


class TestReadBooleanState:
    def test_empty_image_returns_zero(self):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        state, brightness = read_boolean_state(empty)
        assert state == 0
        assert brightness == 0.0

    def test_dark_image_below_threshold_is_zero(self):
        dark = _solid_bgr_image(20, value=30)
        state, brightness = read_boolean_state(dark)
        assert state == 0
        assert brightness < BOOLEAN_BRIGHTNESS_THRESHOLD

    def test_bright_image_above_threshold_is_one(self):
        bright = _solid_bgr_image(20, value=220)
        state, brightness = read_boolean_state(bright)
        assert state == 1
        assert brightness > BOOLEAN_BRIGHTNESS_THRESHOLD

    def test_custom_threshold_is_respected(self):
        image = _solid_bgr_image(20, value=100)
        # Varsayilan esikte (130) 0 doner, dusuk ozel esikte 1 donmeli.
        assert read_boolean_state(image)[0] == 0
        assert read_boolean_state(image, threshold=50.0)[0] == 1


class TestReadRoiKindDispatch:
    def test_boolean_kind_skips_ocr_and_fills_bool_state(self):
        frame = _solid_bgr_image(100, value=200)
        result = read_roi(frame, roi=(0, 0, 50, 50), kind="boolean")
        assert result.kind == "boolean"
        assert result.bool_state == 1
        assert result.text == ""
        assert result.ocr_error is None

    def test_numeric_kind_leaves_bool_state_none(self):
        frame = _solid_bgr_image(100, value=200)
        result = read_roi(frame, roi=(0, 0, 50, 50), kind="numeric")
        assert result.kind == "numeric"
        assert result.bool_state is None

    def test_default_kind_is_numeric(self):
        frame = _solid_bgr_image(100, value=200)
        result = read_roi(frame, roi=(0, 0, 50, 50))
        assert result.kind == "numeric"


class TestCropRoi:
    def test_crop_within_bounds(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        cropped = crop_roi(frame, (10, 10, 20, 30))
        assert cropped.shape == (30, 20, 3)

    def test_crop_clips_to_frame_bounds(self):
        frame = np.zeros((50, 50, 3), dtype=np.uint8)
        cropped = crop_roi(frame, (40, 40, 30, 30))
        assert cropped.shape == (10, 10, 3)
