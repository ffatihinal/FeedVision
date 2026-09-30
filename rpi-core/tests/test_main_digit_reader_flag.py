"""
FeedVision — Görev C (2026-09-30) testleri: RoiDef.reader alanı + main.py'nin
Tesseract/template okuma yolları arasında SADECE ROI'nin kendi "reader"
tercihine göre dallanması.

İKİ KRİTİK REGRESYON burada kilitlenir:
1. reader="tesseract" (varsayılan, eski kayıtlı ROI'lerde alan hiç yok) ->
   davranış 2026-09-30 ÖNCESİ ile birebir aynı kalmalı (screen_reader.read_roi
   çağrılır, digit_reader'a hiç dokunulmaz).
2. reader="template" seçiliyken main._load_digit_template_set + digit_reader.
   read_digits GERÇEKTEN çağrılıyor (mock ile doğrulanıyor) — Tesseract'a
   hiç gidilmiyor.

Diğer main.py testleriyle AYNI desen (bkz. test_calibration_endpoints.py
docstring'i): endpoint fonksiyonları düz Python fonksiyonları, TestClient/
ASGI'ye gerek yok.
"""

import numpy as np
import pytest
from fastapi import HTTPException

import digit_reader
import digit_templates_store
import main
import roi_store
from screen_reader import ScreenReadResult


@pytest.fixture
def _blank_frame():
    return np.zeros((50, 50, 3), dtype=np.uint8)


class TestRoiDefReaderField:
    def test_reader_optional_defaults_to_tesseract_backward_compat(self):
        # Eski kayıtlı ROI (reader alanı hiç yok, 30-09-2026 öncesi) -> Pydantic
        # varsayılanı "tesseract" ile geriye uyumlu, KeyError/ValidationError yok.
        roi = main.RoiDef(name="a", x=0, y=0, w=10, h=10)
        assert roi.reader == "tesseract"

    def test_reader_template_round_trips_through_model_dump(self):
        roi = main.RoiDef(name="a", x=0, y=0, w=10, h=10, reader="template")
        assert roi.model_dump()["reader"] == "template"

    def test_unknown_reader_value_is_rejected(self):
        with pytest.raises(Exception):  # pydantic.ValidationError
            main.RoiDef(name="a", x=0, y=0, w=10, h=10, reader="bogus")


class TestReadAllRoisDefaultTesseractPathUnchanged:
    """Kritik regresyon: reader="tesseract" (varsayılan/eski ROI) davranışı
    HİÇ değişmemeli — read_roi'nin (screen_reader) çağrıldığı, digit_reader'a
    hiç dokunulmadığı doğrulanır."""

    def test_missing_reader_field_calls_read_roi_not_digit_reader(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        roi_store.save_rois(
            "ui_screen",
            [{"name": "eski_roi", "x": 5, "y": 5, "w": 10, "h": 10, "kind": "numeric"}],
        )
        read_roi_called = {"count": 0}
        digit_reader_called = {"count": 0}

        def _fake_read_roi(frame, roi, kind="numeric", quad=None, ocr_whitelist=None):
            read_roi_called["count"] += 1
            return ScreenReadResult(
                roi=roi,
                avg_color_hsv=(0.0, 0.0, 0.0),
                avg_color_rgb=(0.0, 0.0, 0.0),
                text="19",
                ocr_error=None,
                kind=kind,
                bool_state=None,
            )

        def _fake_read_digits(image, templates):
            digit_reader_called["count"] += 1
            return "SHOULD_NOT_BE_CALLED", 1.0

        monkeypatch.setattr(main, "read_roi", _fake_read_roi)
        monkeypatch.setattr(digit_reader, "read_digits", _fake_read_digits)

        results, _uncertain = main._read_all_rois("ui_screen", _blank_frame)

        assert read_roi_called["count"] == 1
        assert digit_reader_called["count"] == 0
        assert results[0]["text"] == "19"
        assert results[0]["reader"] == "tesseract"
        assert results[0]["confidence"] is None

    def test_explicit_reader_tesseract_same_as_missing(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        roi_store.save_rois(
            "ui_screen",
            [{"name": "roi1", "x": 5, "y": 5, "w": 10, "h": 10, "kind": "numeric", "reader": "tesseract"}],
        )
        digit_reader_called = {"count": 0}
        monkeypatch.setattr(
            digit_reader, "read_digits", lambda *a, **k: digit_reader_called.update(count=1) or ("x", 1.0)
        )
        results, _uncertain = main._read_all_rois("ui_screen", _blank_frame)
        assert digit_reader_called["count"] == 0
        assert results[0]["reader"] == "tesseract"

    def test_boolean_roi_ignores_reader_field_uses_read_roi(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        # kind="boolean" ROI'lerde reader anlamsız (zaten hiç OCR çağrılmıyor)
        # -- reader="template" bile olsa read_roi (boolean dalı) çağrılmalı.
        roi_store.save_rois(
            "ui_screen",
            [{"name": "durum", "x": 5, "y": 5, "w": 10, "h": 10, "kind": "boolean", "reader": "template"}],
        )
        digit_reader_called = {"count": 0}
        monkeypatch.setattr(
            digit_reader, "read_digits", lambda *a, **k: digit_reader_called.update(count=1) or ("x", 1.0)
        )
        results, _uncertain = main._read_all_rois("ui_screen", _blank_frame)
        assert digit_reader_called["count"] == 0
        assert results[0]["kind"] == "boolean"


class TestReadAllRoisTemplateReaderPath:
    def test_reader_template_calls_digit_reader_not_tesseract(
        self, isolated_calibration_config, isolated_roi_config, isolated_digit_templates_store, _blank_frame, monkeypatch
    ):
        roi_store.save_rois(
            "ui_screen",
            [{"name": "sicaklik", "x": 5, "y": 5, "w": 10, "h": 10, "kind": "numeric", "reader": "template"}],
        )
        read_roi_called = {"count": 0}
        digit_reader_called: dict = {"count": 0}

        def _fake_read_roi(*a, **k):
            read_roi_called["count"] += 1
            raise AssertionError("reader='template' iken Tesseract yoluna (read_roi) HİÇ girilmemeli")

        def _fake_read_digits(image, templates):
            digit_reader_called["count"] += 1
            digit_reader_called["templates"] = templates
            return "42", 0.9

        monkeypatch.setattr(main, "read_roi", _fake_read_roi)
        monkeypatch.setattr(digit_reader, "read_digits", _fake_read_digits)

        results, _uncertain = main._read_all_rois("ui_screen", _blank_frame)

        assert read_roi_called["count"] == 0
        assert digit_reader_called["count"] == 1
        assert isinstance(digit_reader_called["templates"], digit_reader.DigitTemplateSet)
        assert results[0]["text"] == "42"
        assert results[0]["confidence"] == 0.9
        assert results[0]["reader"] == "template"
        assert results[0]["ocr_error"] is None
        assert results[0]["bool_state"] is None

    def test_real_captured_template_overrides_default_for_that_label(
        self, isolated_calibration_config, isolated_roi_config, isolated_digit_templates_store, _blank_frame
    ):
        # Gerçek uçtan uca (mock yok): bir "5" şablonu gerçekten kaydedilirse
        # _load_digit_template_set o etiket için varsayılanı EZMELİ.
        custom_five = np.full((32, 20), 255, dtype=np.uint8)
        import cv2

        ok, jpg = cv2.imencode(".jpg", custom_five)
        assert ok
        digit_templates_store.add_template("ui_screen", "5", jpg.tobytes())

        template_set = main._load_digit_template_set("ui_screen")
        assert set(template_set.labels()) == set(digit_reader.CHARACTER_LABELS)
        # Varsayılan (PIL render) "5" şablonu, gerçek (tamamen beyaz) görüntüyle
        # AYNI olamaz -- gerçekten üzerine yazıldığını (referans eşitliğinden
        # farklı, gerçek piksel içeriğinden) doğrula.
        assert np.array_equal(template_set.templates["5"], digit_reader._normalize_char_image(custom_five))


class TestDigitTemplateEndpoints:
    def _fake_frame(self, w=100, h=100):
        return np.zeros((h, w, 3), dtype=np.uint8)

    def test_add_unknown_cam_id_returns_404(self, isolated_digit_templates_store):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_add_digit_template("bogus", main.DigitTemplateCapture(x=0, y=0, w=10, h=10, label="5"))
        assert exc_info.value.status_code == 404

    def test_add_no_camera_frame_returns_503(self, isolated_digit_templates_store, monkeypatch):
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: None)
        with pytest.raises(HTTPException) as exc_info:
            main.vision_add_digit_template("ui_screen", main.DigitTemplateCapture(x=0, y=0, w=10, h=10, label="5"))
        assert exc_info.value.status_code == 503

    def test_add_region_out_of_bounds_returns_400(self, isolated_digit_templates_store, monkeypatch):
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: self._fake_frame(100, 100))
        with pytest.raises(HTTPException) as exc_info:
            main.vision_add_digit_template(
                "ui_screen", main.DigitTemplateCapture(x=90, y=90, w=20, h=20, label="5")
            )
        assert exc_info.value.status_code == 400

    def test_add_valid_region_saves_and_returns_template(self, isolated_digit_templates_store, monkeypatch):
        frame = self._fake_frame(100, 100)
        frame[10:30, 20:40] = (10, 20, 30)
        monkeypatch.setattr(main, "_capture_frame", lambda cam_id: frame)
        result = main.vision_add_digit_template(
            "ui_screen", main.DigitTemplateCapture(x=20, y=10, w=20, h=20, label="7")
        )
        assert result["success"] is True
        assert result["label"] == "7"
        assert "7" in result["templates"]
        assert digit_templates_store.get_templates("ui_screen") == result["templates"]

    def test_get_endpoint_unknown_cam_id_returns_404(self, isolated_digit_templates_store):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_get_digit_templates("bogus")
        assert exc_info.value.status_code == 404

    def test_get_endpoint_returns_stored_templates(self, isolated_digit_templates_store):
        digit_templates_store.add_template("ui_screen", "3", b"x")
        result = main.vision_get_digit_templates("ui_screen")
        assert "3" in result["templates"]

    def test_delete_endpoint_removes_template(self, isolated_digit_templates_store):
        digit_templates_store.add_template("ui_screen", "3", b"x")
        result = main.vision_delete_digit_template("ui_screen", "3")
        assert result["success"] is True
        assert result["templates"] == {}
        assert digit_templates_store.get_templates("ui_screen") == {}

    def test_delete_endpoint_unknown_cam_id_returns_404(self, isolated_digit_templates_store):
        with pytest.raises(HTTPException) as exc_info:
            main.vision_delete_digit_template("bogus", "3")
        assert exc_info.value.status_code == 404
