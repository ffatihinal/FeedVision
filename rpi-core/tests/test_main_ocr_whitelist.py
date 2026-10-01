"""
FeedVision — main.py'nin RoiDef.ocr_whitelist alanini screen_reader.read_roi'ye
DOGRU aktardigini dogrular (30-09-2026, OCR PSM/whitelist gorevi).

screen_reader.read_roi GERCEKTEN cagrilmiyor (Tesseract binary'sine bagimli
olmasin diye) -- main.read_roi bir spy ile degistirilip hangi kwargs ile
cagrildigi kontrol ediliyor. Kalibrasyon YOK varsayimiyla (en basit yol)
_adjust_rois_for_drift ham ROI'yi degistirmeden dondurur (bkz.
test_main_roi_quad_drift.py'deki ayni varsayimli regresyon testi).
"""

import numpy as np
import pytest

import main
import roi_store
from screen_reader import ScreenReadResult


@pytest.fixture
def _blank_frame():
    return np.zeros((50, 50, 3), dtype=np.uint8)


def _fake_read_roi_factory(captured: dict):
    def _fake_read_roi(frame, roi, kind="numeric", quad=None, ocr_whitelist=None, bool_threshold=None):
        captured["ocr_whitelist"] = ocr_whitelist
        captured["kind"] = kind
        return ScreenReadResult(
            roi=roi,
            avg_color_hsv=(0.0, 0.0, 0.0),
            avg_color_rgb=(0.0, 0.0, 0.0),
            text="19",
            ocr_error=None,
            kind=kind,
            bool_state=None,
        )

    return _fake_read_roi


class TestReadAllRoisForwardsOcrWhitelist:
    def test_ocr_whitelist_is_forwarded_to_read_roi(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        roi_store.save_rois(
            "ui_screen",
            [
                {
                    "name": "oksijen",
                    "x": 5,
                    "y": 5,
                    "w": 10,
                    "h": 10,
                    "kind": "numeric",
                    "ocr_whitelist": "0123456789.-",
                }
            ],
        )
        captured: dict = {}
        monkeypatch.setattr(main, "read_roi", _fake_read_roi_factory(captured))

        results, _uncertain = main._read_all_rois("ui_screen", _blank_frame)

        assert captured["ocr_whitelist"] == "0123456789.-"
        assert results[0]["text"] == "19"

    def test_missing_ocr_whitelist_defaults_to_none_backward_compat(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        # Eski kayitli ROI (ocr_whitelist alani hic yok, 30-09-2026 oncesi
        # kaydedilmis) -> None ile cagrilmali, KeyError firlatmamali.
        roi_store.save_rois(
            "ui_screen",
            [{"name": "eski_roi", "x": 5, "y": 5, "w": 10, "h": 10, "kind": "numeric"}],
        )
        captured: dict = {}
        monkeypatch.setattr(main, "read_roi", _fake_read_roi_factory(captured))

        main._read_all_rois("ui_screen", _blank_frame)

        assert captured["ocr_whitelist"] is None


class TestRoiDefModelAcceptsOcrWhitelist:
    def test_ocr_whitelist_optional_and_defaults_to_none(self):
        roi = main.RoiDef(name="a", x=0, y=0, w=10, h=10)
        assert roi.ocr_whitelist is None

    def test_ocr_whitelist_round_trips_through_model_dump(self):
        roi = main.RoiDef(name="a", x=0, y=0, w=10, h=10, ocr_whitelist="0123456789.-")
        assert roi.model_dump()["ocr_whitelist"] == "0123456789.-"
