"""
FeedVision — main.py'nin ROI drift + gerçek dörtgen (quad) entegrasyonu
testleri (Görev A, 2026-09-29).

_adjust_rois_for_drift / _read_all_rois saf Python fonksiyonları (FastAPI'ye
bağlı değil) — TestClient/HTTP'ye gerek yok, doğrudan çağrılabilirler (bkz.
test_motor_feed_start.py'deki aynı desen). calibration_store/roi_store gerçek
disk dosyalarına dokunmasın diye isolated_* fixture'ları kullanılıyor.
"""

import numpy as np
import pytest

import calibration_store
import main
import roi_store


@pytest.fixture
def _blank_frame():
    return np.zeros((100, 100, 3), dtype=np.uint8)


class TestAdjustRoisForDriftRegression:
    """Kalibrasyon yokken eski davranış (ham ROI, quad yok) korunmalı."""

    def test_no_calibration_returns_rois_unmodified(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame
    ):
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", _blank_frame, rois)
        assert adjusted == rois  # aynı liste, dokunulmamış
        assert "quad" not in adjusted[0]
        assert uncertain is False


class TestAdjustRoisForDriftWithCalibration:
    """Kalibrasyon VARSA her roi_def'e bounding box'ın YANINDA gerçek quad eklenmeli."""

    def test_calibrated_but_corners_not_found_falls_back_uncertain(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        calibration_store.save_reference(
            "ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]]
        )
        monkeypatch.setattr(main, "detect_screen_corners", lambda frame: None)
        main._last_known_corners.pop("ui_screen", None)
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", _blank_frame, rois)
        # Hiç iyi kare görülmedi -> düzeltmesiz devam, ama uncertain=True.
        assert adjusted == rois
        assert uncertain is True

    def test_calibrated_and_corners_found_adds_quad_alongside_bbox(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        ref_corners = [[0, 0], [100, 0], [100, 100], [0, 100]]
        calibration_store.save_reference("ui_screen", ref_corners)
        # Şimdiki köşeler referansa göre hafif trapez (perspektif kayması).
        current_corners = np.array(
            [[0, 0], [100, 10], [95, 100], [0, 100]], dtype=np.float32
        )
        monkeypatch.setattr(main, "detect_screen_corners", lambda frame: current_corners)
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", _blank_frame, rois)
        assert uncertain is False
        roi_def = adjusted[0]
        assert "quad" in roi_def
        quad = np.array(roi_def["quad"], dtype=np.float32)
        assert quad.shape == (4, 2)
        # bbox (x,y,w,h), quad'ın min/max'ıyla tutarlı olmalı (aynı geometri)
        # — dikdörtgenlik bozulması zaten test_screen_calibration.py'deki
        # TestWarpRoiQuad.test_perspective_skew_... içinde ayrıca doğrulanıyor,
        # burada asıl kontrol main.py'nin quad'ı bbox'ın YANINDA taşıdığı.
        assert roi_def["x"] == pytest.approx(float(np.min(quad[:, 0])), abs=1)
        assert roi_def["y"] == pytest.approx(float(np.min(quad[:, 1])), abs=1)


class TestReadAllRoisUsesQuad:
    def test_result_includes_roi_quad_when_calibrated(
        self, isolated_calibration_config, isolated_roi_config, monkeypatch
    ):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:30, 10:30] = (0, 150, 0)
        roi_store.save_rois(
            "ui_screen", [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        )
        calibration_store.save_reference(
            "ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]]
        )
        # Kimlik dönüşüm (şimdiki = referans) — bbox/quad aynı bölgeyi işaret etsin.
        identity_corners = np.array(
            [[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32
        )
        monkeypatch.setattr(main, "detect_screen_corners", lambda f: identity_corners)
        results, uncertain = main._read_all_rois("ui_screen", frame)
        assert uncertain is False
        assert len(results) == 1
        assert results[0]["roi_quad"] is not None
        assert len(results[0]["roi_quad"]) == 4

    def test_result_roi_quad_is_none_without_calibration(
        self, isolated_calibration_config, isolated_roi_config
    ):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        roi_store.save_rois(
            "ui_screen", [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        )
        results, uncertain = main._read_all_rois("ui_screen", frame)
        assert uncertain is False
        assert results[0]["roi_quad"] is None
