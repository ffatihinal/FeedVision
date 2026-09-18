"""
FeedVision — calibration_store.py testleri.

Gerçek calibration_config.json'a dokunmamak için `isolated_calibration_config`
fixture'ı (conftest.py) kullanılır — CONFIG_PATH'i tmp_path'e yönlendirir.
"""

import json

import calibration_store


class TestGetReference:
    def test_no_file_returns_none(self, isolated_calibration_config):
        assert calibration_store.get_reference("chamber") is None

    def test_corrupt_json_returns_none(self, isolated_calibration_config):
        isolated_calibration_config.write_text("{bozuk", encoding="utf-8")
        assert calibration_store.get_reference("chamber") is None

    def test_non_dict_json_returns_none(self, isolated_calibration_config):
        isolated_calibration_config.write_text("[1, 2, 3]", encoding="utf-8")
        assert calibration_store.get_reference("chamber") is None

    def test_unknown_cam_id_returns_none(self, isolated_calibration_config):
        data = {"chamber": {"corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "calibrated_at": 1.0}}
        isolated_calibration_config.write_text(json.dumps(data), encoding="utf-8")
        assert calibration_store.get_reference("ui_screen") is None

    def test_legacy_cam1_key_migrated_to_chamber(self, isolated_calibration_config):
        legacy = {"cam1": {"corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "calibrated_at": 1.0}}
        isolated_calibration_config.write_text(json.dumps(legacy), encoding="utf-8")
        ref = calibration_store.get_reference("chamber")
        assert ref is not None
        assert ref["corners"] == [[0, 0], [1, 0], [1, 1], [0, 1]]


class TestSaveReference:
    def test_save_then_get_roundtrip(self, isolated_calibration_config):
        corners = [[10.0, 10.0], [100.0, 10.0], [100.0, 100.0], [10.0, 100.0]]
        entry = calibration_store.save_reference("ui_screen", corners)
        assert entry["corners"] == corners
        assert "calibrated_at" in entry

        ref = calibration_store.get_reference("ui_screen")
        assert ref["corners"] == corners

    def test_save_replaces_previous_reference(self, isolated_calibration_config):
        calibration_store.save_reference("chamber", [[0, 0], [1, 0], [1, 1], [0, 1]])
        new_corners = [[5, 5], [6, 5], [6, 6], [5, 6]]
        calibration_store.save_reference("chamber", new_corners)

        ref = calibration_store.get_reference("chamber")
        assert ref["corners"] == new_corners

    def test_save_does_not_affect_other_camera(self, isolated_calibration_config):
        calibration_store.save_reference("chamber", [[0, 0], [1, 0], [1, 1], [0, 1]])
        calibration_store.save_reference("ui_screen", [[2, 2], [3, 2], [3, 3], [2, 3]])

        assert calibration_store.get_reference("chamber")["corners"] == [[0, 0], [1, 0], [1, 1], [0, 1]]
        assert calibration_store.get_reference("ui_screen")["corners"] == [[2, 2], [3, 2], [3, 3], [2, 3]]

    def test_save_writes_atomic_tmp_file_cleaned_up(self, isolated_calibration_config):
        calibration_store.save_reference("chamber", [[0, 0], [1, 0], [1, 1], [0, 1]])
        tmp_path = isolated_calibration_config.with_suffix(".json.tmp")
        assert not tmp_path.exists()
        assert isolated_calibration_config.exists()
