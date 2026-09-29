"""
FeedVision — vision_settings_store.py testleri.

Gerçek vision_settings.json'a dokunmamak için `isolated_vision_settings_config`
fixture'ı (conftest.py) kullanılır — CONFIG_PATH'i tmp_path'e yönlendirir.
"""

import json

import vision_settings_store


class TestGetSettings:
    def test_no_file_returns_defaults(self, isolated_vision_settings_config):
        settings = vision_settings_store.get_settings("ui_screen")
        assert settings == {
            "exposure": {"auto": True, "exposure_time": None, "gain": None},
            "preprocess": {"clahe_enabled": False},
        }

    def test_corrupt_json_returns_defaults(self, isolated_vision_settings_config):
        isolated_vision_settings_config.write_text("{bozuk", encoding="utf-8")
        assert vision_settings_store.get_settings("ui_screen")["exposure"]["auto"] is True

    def test_partial_stored_data_merged_with_defaults(self, isolated_vision_settings_config):
        # Sadece preprocess kaydedilmis olsun — exposure hala varsayilan donmeli.
        data = {"ui_screen": {"preprocess": {"clahe_enabled": True}}}
        isolated_vision_settings_config.write_text(json.dumps(data), encoding="utf-8")
        settings = vision_settings_store.get_settings("ui_screen")
        assert settings["preprocess"]["clahe_enabled"] is True
        assert settings["exposure"] == {"auto": True, "exposure_time": None, "gain": None}

    def test_legacy_cam2_key_migrated_to_ui_screen(self, isolated_vision_settings_config):
        legacy = {"cam2": {"preprocess": {"clahe_enabled": True}}}
        isolated_vision_settings_config.write_text(json.dumps(legacy), encoding="utf-8")
        assert vision_settings_store.get_settings("ui_screen")["preprocess"]["clahe_enabled"] is True


class TestSaveExposure:
    def test_save_then_get_roundtrip_manual(self, isolated_vision_settings_config):
        vision_settings_store.save_exposure("ui_screen", auto=False, exposure_time=20000, gain=2.5)
        settings = vision_settings_store.get_settings("ui_screen")
        assert settings["exposure"] == {"auto": False, "exposure_time": 20000, "gain": 2.5}

    def test_auto_forces_manual_fields_to_none(self, isolated_vision_settings_config):
        # Onceden manuel kaydedilmis olsa bile auto=True'ya gecince eski
        # exposure_time/gain diskte tutarsiz kalmasin diye None'a zorlanir.
        vision_settings_store.save_exposure("ui_screen", auto=False, exposure_time=15000, gain=1.0)
        vision_settings_store.save_exposure("ui_screen", auto=True, exposure_time=15000, gain=1.0)
        settings = vision_settings_store.get_settings("ui_screen")
        assert settings["exposure"] == {"auto": True, "exposure_time": None, "gain": None}

    def test_save_does_not_affect_other_camera(self, isolated_vision_settings_config):
        vision_settings_store.save_exposure("ui_screen", auto=False, exposure_time=1000, gain=1.0)
        vision_settings_store.save_exposure("chamber", auto=False, exposure_time=2000, gain=2.0)
        assert vision_settings_store.get_settings("ui_screen")["exposure"]["exposure_time"] == 1000
        assert vision_settings_store.get_settings("chamber")["exposure"]["exposure_time"] == 2000

    def test_save_exposure_does_not_clobber_preprocess(self, isolated_vision_settings_config):
        vision_settings_store.save_preprocess("ui_screen", True)
        vision_settings_store.save_exposure("ui_screen", auto=False, exposure_time=5000, gain=1.0)
        settings = vision_settings_store.get_settings("ui_screen")
        assert settings["preprocess"]["clahe_enabled"] is True
        assert settings["exposure"]["exposure_time"] == 5000


class TestSavePreprocess:
    def test_save_then_get_roundtrip(self, isolated_vision_settings_config):
        vision_settings_store.save_preprocess("ui_screen", True)
        assert vision_settings_store.get_settings("ui_screen")["preprocess"] == {"clahe_enabled": True}

    def test_save_preprocess_does_not_clobber_exposure(self, isolated_vision_settings_config):
        vision_settings_store.save_exposure("ui_screen", auto=False, exposure_time=3000, gain=1.5)
        vision_settings_store.save_preprocess("ui_screen", True)
        settings = vision_settings_store.get_settings("ui_screen")
        assert settings["exposure"]["exposure_time"] == 3000
        assert settings["preprocess"]["clahe_enabled"] is True

    def test_save_writes_atomic_tmp_file_cleaned_up(self, isolated_vision_settings_config):
        vision_settings_store.save_preprocess("ui_screen", True)
        tmp_path = isolated_vision_settings_config.with_suffix(".json.tmp")
        assert not tmp_path.exists()
        assert isolated_vision_settings_config.exists()
