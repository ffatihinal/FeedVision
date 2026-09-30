"""
FeedVision — Görev A+B (2026-09-30) yeni kalibrasyon HTTP endpoint'lerinin
(main.py: şablon ekle/sil, aç/kapa toggle, GET .../calibration'ın genişlemesi)
testleri.

Diğer main.py testleriyle AYNI desen (bkz. test_motor_feed_start.py'nin baş
docstring'i): endpoint fonksiyonları düz Python fonksiyonları — FastAPI
dekoratörü bunu değiştirmiyor, bu yüzden TestClient/ASGI'ye gerek yok,
doğrudan çağrılabilirler. Kamera donanımına bağımlı olmasın diye
main._capture_frame monkeypatch'leniyor (Mac/CI'da zaten None dönüyor,
ama testi platforma bağımlı kılmamak için açıkça sahteleniyor).
"""

import numpy as np
import pytest
from fastapi import HTTPException

import calibration_store
import main


def _fake_frame(w=100, h=100):
    return np.zeros((h, w, 3), dtype=np.uint8)


class TestCalibrationTemplateAddEndpoint:
    def test_unknown_cam_id_returns_404(self, isolated_calibration_config):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_add_calibration_template("bogus", main.CalibrationTemplateRegion(x=0, y=0, w=10, h=10))
        assert exc_info.value.status_code == 404

    def test_no_camera_frame_returns_503(self, isolated_calibration_config, monkeypatch):
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: None)
        with pytest.raises(HTTPException) as exc_info:
            main.vision_add_calibration_template("ui_screen", main.CalibrationTemplateRegion(x=0, y=0, w=10, h=10))
        assert exc_info.value.status_code == 503

    def test_region_out_of_bounds_returns_400(self, isolated_calibration_config, monkeypatch):
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: _fake_frame(100, 100))
        with pytest.raises(HTTPException) as exc_info:
            main.vision_add_calibration_template("ui_screen", main.CalibrationTemplateRegion(x=90, y=90, w=20, h=20))
        assert exc_info.value.status_code == 400

    def test_valid_region_saves_template_and_returns_it(self, isolated_calibration_config, monkeypatch):
        frame = _fake_frame(100, 100)
        frame[10:30, 20:40] = (10, 20, 30)
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: frame)
        result = main.vision_add_calibration_template(
            "ui_screen", main.CalibrationTemplateRegion(x=20, y=10, w=20, h=20)
        )
        assert result["success"] is True
        assert result["template"]["anchor"] == [20.0, 10.0]
        assert result["template"]["size"] == [20, 20]
        assert len(result["templates"]) == 1
        # Endpoint'in "başarılı" demesi yetmez — gerçekten store'a yazıldığını doğrula.
        assert calibration_store.get_templates("ui_screen") == result["templates"]

    def test_multiple_calls_append_not_replace(self, isolated_calibration_config, monkeypatch):
        frame = _fake_frame(100, 100)
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: frame)
        main.vision_add_calibration_template("ui_screen", main.CalibrationTemplateRegion(x=0, y=0, w=10, h=10))
        main.vision_add_calibration_template("ui_screen", main.CalibrationTemplateRegion(x=50, y=50, w=10, h=10))
        assert len(calibration_store.get_templates("ui_screen")) == 2


class TestCalibrationTemplateDeleteEndpoint:
    def test_unknown_cam_id_returns_404(self, isolated_calibration_config):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_delete_calibration_template("bogus", 0)
        assert exc_info.value.status_code == 404

    def test_deletes_existing_template(self, isolated_calibration_config):
        calibration_store.add_template("ui_screen", anchor=[1.0, 1.0], size=[5, 5], image_bytes=b"x")
        result = main.vision_delete_calibration_template("ui_screen", 0)
        assert result["success"] is True
        assert result["templates"] == []
        assert calibration_store.get_templates("ui_screen") == []


class TestCalibrationToggleEndpoint:
    def test_unknown_cam_id_returns_404(self, isolated_calibration_config):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_toggle_calibration("bogus", main.CalibrationTogglePayload(enabled=True))
        assert exc_info.value.status_code == 404

    def test_toggle_off_then_on_persists(self, isolated_calibration_config):
        result_off = main.vision_toggle_calibration("ui_screen", main.CalibrationTogglePayload(enabled=False))
        assert result_off == {"success": True, "enabled": False}
        assert calibration_store.get_enabled("ui_screen") is False

        result_on = main.vision_toggle_calibration("ui_screen", main.CalibrationTogglePayload(enabled=True))
        assert result_on == {"success": True, "enabled": True}
        assert calibration_store.get_enabled("ui_screen") is True


class TestGetCalibrationIncludesNewFields:
    def test_includes_enabled_and_templates_when_nothing_configured(self, isolated_calibration_config):
        result = main.vision_get_calibration("ui_screen")
        assert result == {"calibration": None, "enabled": True, "templates": []}

    def test_reflects_toggle_and_templates_state(self, isolated_calibration_config):
        calibration_store.set_enabled("ui_screen", False)
        calibration_store.add_template("ui_screen", anchor=[1.0, 1.0], size=[5, 5], image_bytes=b"x")
        result = main.vision_get_calibration("ui_screen")
        assert result["enabled"] is False
        assert len(result["templates"]) == 1

    def test_unknown_cam_id_returns_404(self, isolated_calibration_config):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_get_calibration("bogus")
        assert exc_info.value.status_code == 404
