"""
FeedVision — screen_reader.py testleri (SADECE saf goruntu islemesi, OCR
CAGRISI yapilmadan test edilebilen kisimlar).

Sentetik (numpy ile uretilen) goruntulerle calisir, gercek kameraya/Tesseract
binary'sine bagimli degil — read_text_ocr'in kendisi (Tesseract cagrisi)
kasitli olarak test edilmiyor (ortama gore var/yok), sadece "boolean" ROI'lerde
OCR'in hic CAGRILMADIGI dogrulaniyor.
"""

import cv2
import numpy as np
import pytest

from screen_reader import (
    BOOLEAN_BRIGHTNESS_THRESHOLD,
    crop_roi,
    crop_roi_quad,
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

    def test_no_calibration_still_uses_plain_rect_crop_regression(self):
        # Görev A regresyon: quad verilmezse (kalibrasyon yok) read_roi eski
        # crop_roi (düz dikdörtgen) davranışını korumalı.
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:40, 10:30] = (0, 200, 0)  # ROI içi yeşil boyalı
        result = read_roi(frame, roi=(10, 10, 20, 30), kind="numeric", quad=None)
        # avg_color_rgb yaklaşık (0, 200, 0) olmalı — crop_roi ile aynı bölge kırpıldı.
        r, g, b = result.avg_color_rgb
        assert r == pytest.approx(0, abs=2)
        assert g == pytest.approx(200, abs=2)
        assert b == pytest.approx(0, abs=2)


class TestCropRoiQuad:
    def test_axis_aligned_quad_matches_plain_crop_size(self):
        # Eksene hizali (döndürülmemiş) bir "dörtgen" verilirse, crop_roi_quad
        # crop_roi ile aynı boyutta/içerikte bir görüntü üretmeli.
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:40, 10:30] = (0, 200, 0)
        quad = np.array([[10, 10], [30, 10], [30, 40], [10, 40]], dtype=np.float32)
        cropped = crop_roi_quad(frame, quad)
        assert cropped.shape[:2] == (30, 20)
        # atol biraz gevşek: warpPerspective kenar piksellerinde interpolasyon
        # nedeniyle en dış satır/sütunda hafif karışım oluşabilir — asıl
        # doğrulanan şey boyut ve baskın renk, alt-piksel kenar hassasiyeti değil.
        mean_color = cropped.reshape(-1, 3).mean(axis=0)
        np.testing.assert_allclose(mean_color, [0, 200, 0], atol=20)

    def test_skewed_quad_is_flattened_and_contains_painted_region(self):
        # Eğik (paralelkenar) bir dörtgen tanımlanır, içinde belirli bir renk
        # boyanmış olsun — düzleştirilmiş çıktı bu rengi baskın olarak içermeli.
        frame = np.full((200, 200, 3), 255, dtype=np.uint8)
        skewed_region = np.array([[50, 50], [130, 60], [110, 140], [30, 130]], dtype=np.int32)
        cv2.fillConvexPoly(frame, skewed_region, (0, 0, 200))  # kırmızı (BGR)
        quad = np.array([[50, 50], [130, 60], [110, 140], [30, 130]], dtype=np.float32)
        cropped = crop_roi_quad(frame, quad)
        assert cropped.size > 0
        mean_color = cropped.reshape(-1, 3).mean(axis=0)  # BGR
        # Çoğunlukla boyalı bölgeden geldiği için kırmızı (B kanalı) baskın olmalı.
        assert mean_color[2] > mean_color[0]
        assert mean_color[2] > 100

    def test_output_size_override_is_respected(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        quad = np.array([[10, 10], [30, 10], [30, 40], [10, 40]], dtype=np.float32)
        cropped = crop_roi_quad(frame, quad, output_size=(64, 128))
        assert cropped.shape[:2] == (128, 64)


class TestReadRoiQuadPath:
    def test_quad_provided_uses_perspective_crop_not_plain_bbox(self):
        # Aynı boyuttaki bbox'ın DIŞINDA kalan bir alanı kapsayan eğik quad
        # verilirse, read_roi'nin quad yolunu kullandığı (roi bbox'ını değil)
        # renk sonucundan doğrulanabilir.
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[60:90, 60:90] = (0, 0, 250)  # sağ-alt köşede kırmızı bölge
        quad = np.array([[60, 60], [90, 60], [90, 90], [60, 90]], dtype=np.float32)
        # roi (bbox) kasıtlı olarak farklı/boş bir bölgeyi işaret ediyor — quad
        # verildiğinde bunun YOK sayıldığını doğrular.
        result = read_roi(frame, roi=(0, 0, 5, 5), kind="numeric", quad=quad)
        r, g, b = result.avg_color_rgb
        # Kenar interpolasyonu nedeniyle biraz gevşek tolerans (bkz. yukarıdaki
        # TestCropRoiQuad notu) — asıl doğrulanan şey roi bbox'ının DEĞİL,
        # quad'ın kullanıldığı (kırmızı bölgenin baskın çıkması).
        assert r > 200
        assert r > g and r > b
