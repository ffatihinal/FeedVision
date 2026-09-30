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

    def test_save_reference_preserves_existing_enabled_and_templates(self, isolated_calibration_config):
        # 2026-09-30 regresyon: save_reference eskiden tum girdiyi replace
        # ediyordu — bu da onceden secilmis sablonlari/toggle tercihini
        # sessizce silerdi. Artik SADECE corners/calibrated_at degismeli.
        calibration_store.set_enabled("chamber", False)
        calibration_store.add_template("chamber", anchor=[1.0, 2.0], size=[10, 10], image_bytes=b"\xff\xd8\xff")
        calibration_store.save_reference("chamber", [[0, 0], [1, 0], [1, 1], [0, 1]])

        assert calibration_store.get_enabled("chamber") is False
        assert len(calibration_store.get_templates("chamber")) == 1
        assert calibration_store.get_reference("chamber")["corners"] == [[0, 0], [1, 0], [1, 1], [0, 1]]


class TestEnabledToggle:
    def test_default_enabled_is_true_when_never_set(self, isolated_calibration_config):
        # Görev B öncesi tek davranış "referans varsa uygula" idi — yeni
        # toggle'ın varsayılanı bunu bozmamalı (sessiz regresyon olmasın).
        assert calibration_store.get_enabled("ui_screen") is True

    def test_set_enabled_false_then_true_roundtrip(self, isolated_calibration_config):
        calibration_store.set_enabled("ui_screen", False)
        assert calibration_store.get_enabled("ui_screen") is False
        calibration_store.set_enabled("ui_screen", True)
        assert calibration_store.get_enabled("ui_screen") is True

    def test_enabled_is_per_camera(self, isolated_calibration_config):
        calibration_store.set_enabled("chamber", False)
        assert calibration_store.get_enabled("chamber") is False
        assert calibration_store.get_enabled("ui_screen") is True


class TestTemplates:
    def test_no_templates_returns_empty_list(self, isolated_calibration_config):
        assert calibration_store.get_templates("ui_screen") == []

    def test_add_template_persists_metadata_and_image_file(self, isolated_calibration_config):
        entry = calibration_store.add_template(
            "ui_screen", anchor=[12.0, 34.0], size=[20, 15], image_bytes=b"\xff\xd8\xff\xe0fake-jpeg"
        )
        assert entry["anchor"] == [12.0, 34.0]
        assert entry["size"] == [20, 15]
        assert "captured_at" in entry

        templates = calibration_store.get_templates("ui_screen")
        assert len(templates) == 1
        assert templates[0] == entry

        img_path = calibration_store.TEMPLATES_DIR / entry["filename"]
        assert img_path.exists()
        assert img_path.read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg"

    def test_add_template_appends_does_not_replace(self, isolated_calibration_config):
        calibration_store.add_template("ui_screen", anchor=[0, 0], size=[5, 5], image_bytes=b"a")
        calibration_store.add_template("ui_screen", anchor=[100, 100], size=[5, 5], image_bytes=b"b")
        templates = calibration_store.get_templates("ui_screen")
        assert len(templates) == 2
        assert templates[0]["anchor"] == [0.0, 0.0]
        assert templates[1]["anchor"] == [100.0, 100.0]

    def test_read_template_image_bytes_roundtrip(self, isolated_calibration_config):
        entry = calibration_store.add_template("ui_screen", anchor=[1, 1], size=[5, 5], image_bytes=b"hello-bytes")
        data = calibration_store.read_template_image_bytes("ui_screen", entry["filename"])
        assert data == b"hello-bytes"

    def test_read_template_image_bytes_missing_file_returns_none(self, isolated_calibration_config):
        assert calibration_store.read_template_image_bytes("ui_screen", "nope.jpg") is None

    def test_remove_template_deletes_metadata_and_file(self, isolated_calibration_config):
        entry = calibration_store.add_template("ui_screen", anchor=[1, 1], size=[5, 5], image_bytes=b"x")
        img_path = calibration_store.TEMPLATES_DIR / entry["filename"]
        assert img_path.exists()

        remaining = calibration_store.remove_template("ui_screen", 0)
        assert remaining == []
        assert calibration_store.get_templates("ui_screen") == []
        assert not img_path.exists()

    def test_remove_template_invalid_index_is_noop(self, isolated_calibration_config):
        calibration_store.add_template("ui_screen", anchor=[1, 1], size=[5, 5], image_bytes=b"x")
        remaining = calibration_store.remove_template("ui_screen", 5)
        assert len(remaining) == 1

    def test_templates_are_per_camera(self, isolated_calibration_config):
        calibration_store.add_template("chamber", anchor=[1, 1], size=[5, 5], image_bytes=b"c")
        assert len(calibration_store.get_templates("chamber")) == 1
        assert calibration_store.get_templates("ui_screen") == []
