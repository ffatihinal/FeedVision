"""
FeedVision — roi_store.py testleri.

Gerçek roi_config.json'a dokunmamak için `isolated_roi_config` fixture'ı
(conftest.py) kullanılır — CONFIG_PATH'i tmp_path'e yönlendirir.
"""

import json

import roi_store


class TestGetRois:
    def test_no_file_returns_empty_list(self, isolated_roi_config):
        assert roi_store.get_rois("chamber") == []

    def test_corrupt_json_returns_empty_list(self, isolated_roi_config):
        isolated_roi_config.write_text("{bozuk", encoding="utf-8")
        assert roi_store.get_rois("chamber") == []

    def test_non_dict_json_returns_empty_list(self, isolated_roi_config):
        isolated_roi_config.write_text("[1, 2, 3]", encoding="utf-8")
        assert roi_store.get_rois("chamber") == []

    def test_unknown_cam_id_returns_empty_list(self, isolated_roi_config):
        data = {"chamber": [{"name": "basinc", "x": 0, "y": 0, "w": 10, "h": 10}]}
        isolated_roi_config.write_text(json.dumps(data), encoding="utf-8")
        assert roi_store.get_rois("ui_screen") == []

    def test_legacy_cam2_key_migrated_to_ui_screen(self, isolated_roi_config):
        legacy = {"cam2": [{"name": "sicaklik", "x": 1, "y": 1, "w": 5, "h": 5}]}
        isolated_roi_config.write_text(json.dumps(legacy), encoding="utf-8")
        rois = roi_store.get_rois("ui_screen")
        assert rois == [{"name": "sicaklik", "x": 1, "y": 1, "w": 5, "h": 5}]

    def test_legacy_migration_does_not_rewrite_file(self, isolated_roi_config):
        # migrate_legacy_camera_keys sadece bellek icinde yapilir — dosya
        # bir sonraki save_rois cagrisina kadar eski haliyle kalmali.
        legacy = {"cam1": [{"name": "x"}]}
        isolated_roi_config.write_text(json.dumps(legacy), encoding="utf-8")
        roi_store.get_rois("chamber")
        on_disk = json.loads(isolated_roi_config.read_text(encoding="utf-8"))
        assert "cam1" in on_disk


class TestSaveRois:
    def test_save_then_get_roundtrip(self, isolated_roi_config):
        rois = [{"name": "basinc", "x": 0, "y": 0, "w": 10, "h": 10}]
        roi_store.save_rois("chamber", rois)
        assert roi_store.get_rois("chamber") == rois

    def test_save_replaces_entire_list(self, isolated_roi_config):
        roi_store.save_rois("chamber", [{"name": "eski"}])
        roi_store.save_rois("chamber", [{"name": "yeni"}])
        assert roi_store.get_rois("chamber") == [{"name": "yeni"}]

    def test_save_does_not_affect_other_camera(self, isolated_roi_config):
        roi_store.save_rois("chamber", [{"name": "a"}])
        roi_store.save_rois("ui_screen", [{"name": "b"}])
        assert roi_store.get_rois("chamber") == [{"name": "a"}]
        assert roi_store.get_rois("ui_screen") == [{"name": "b"}]

    def test_save_empty_list_clears_camera_rois(self, isolated_roi_config):
        roi_store.save_rois("chamber", [{"name": "a"}])
        roi_store.save_rois("chamber", [])
        assert roi_store.get_rois("chamber") == []

    def test_save_writes_atomic_tmp_file_cleaned_up(self, isolated_roi_config):
        roi_store.save_rois("chamber", [{"name": "a"}])
        tmp_path = isolated_roi_config.with_suffix(".json.tmp")
        assert not tmp_path.exists()
        assert isolated_roi_config.exists()
