"""
FeedVision — motion_params.py testleri.

Gerçek motion_params.json'a dokunmamak için `isolated_motion_params_config`
fixture'ı (conftest.py) kullanılır — CONFIG_PATH'i tmp_path'e yönlendirir.
"""

import json

import motion_params


class TestGetParams:
    def test_no_file_returns_defaults(self, isolated_motion_params_config):
        params = motion_params.get_params()
        assert params == motion_params.DEFAULTS

    def test_corrupt_json_returns_defaults(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text("{bozuk", encoding="utf-8")
        assert motion_params.get_params() == motion_params.DEFAULTS

    def test_non_dict_json_returns_defaults(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text("[1, 2, 3]", encoding="utf-8")
        assert motion_params.get_params() == motion_params.DEFAULTS

    def test_missing_field_falls_back_to_default(self, isolated_motion_params_config):
        # Sadece D_drive_mm kayıtlı — diğer 3 alan DEFAULTS'tan gelmeli.
        isolated_motion_params_config.write_text(json.dumps({"D_drive_mm": 60.0}), encoding="utf-8")
        params = motion_params.get_params()
        assert params["D_drive_mm"] == 60.0
        assert params["D_wheel_dc_mm"] == motion_params.DEFAULTS["D_wheel_dc_mm"]
        assert params["D_rod_mm"] == motion_params.DEFAULTS["D_rod_mm"]
        assert params["RPM_MAX_NOLOAD"] == motion_params.DEFAULTS["RPM_MAX_NOLOAD"]

    def test_unknown_field_ignored(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text(json.dumps({"unknown_field": 5.0}), encoding="utf-8")
        params = motion_params.get_params()
        assert "unknown_field" not in params
        assert params == motion_params.DEFAULTS

    def test_zero_or_negative_field_falls_back_to_default(self, isolated_motion_params_config):
        # Fiziksel bir çap/RPM 0 ya da negatif olamaz — bozuk/kasıtsız yazılmış
        # bir değerin komut hesaplamasına sessizce sızmasındansa varsayılana düşer.
        isolated_motion_params_config.write_text(
            json.dumps({"D_drive_mm": 0.0, "D_rod_mm": -5.0}), encoding="utf-8"
        )
        params = motion_params.get_params()
        assert params["D_drive_mm"] == motion_params.DEFAULTS["D_drive_mm"]
        assert params["D_rod_mm"] == motion_params.DEFAULTS["D_rod_mm"]


class TestSaveParams:
    def test_save_then_get_roundtrip(self, isolated_motion_params_config):
        saved = motion_params.save_params({"D_drive_mm": 51.5})
        assert saved["D_drive_mm"] == 51.5
        params = motion_params.get_params()
        assert params["D_drive_mm"] == 51.5

    def test_save_is_partial_update(self, isolated_motion_params_config):
        motion_params.save_params({"D_drive_mm": 51.5})
        motion_params.save_params({"RPM_MAX_NOLOAD": 120.0})
        params = motion_params.get_params()
        # İlk save'de yazılan D_drive_mm, ikinci (kısmi) save'de KORUNMALI.
        assert params["D_drive_mm"] == 51.5
        assert params["RPM_MAX_NOLOAD"] == 120.0

    def test_save_ignores_unknown_field(self, isolated_motion_params_config):
        motion_params.save_params({"unknown_field": 99.0})
        params = motion_params.get_params()
        assert "unknown_field" not in params

    def test_save_ignores_zero_or_negative(self, isolated_motion_params_config):
        motion_params.save_params({"D_drive_mm": 60.0})
        motion_params.save_params({"D_drive_mm": -1.0})
        params = motion_params.get_params()
        assert params["D_drive_mm"] == 60.0

    def test_save_writes_atomic_tmp_file_cleaned_up(self, isolated_motion_params_config):
        motion_params.save_params({"D_drive_mm": 51.5})
        tmp_path = isolated_motion_params_config.with_suffix(".json.tmp")
        assert not tmp_path.exists()
        assert isolated_motion_params_config.exists()
