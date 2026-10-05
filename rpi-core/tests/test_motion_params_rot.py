"""
FeedVision — motion_params NEMA17 alanları (D_wheel_rot_mm, ROT_PPR) testleri.
Gerçek motion_params.json'a dokunmaz (isolated_motion_params_config fixture'ı).
"""

import json

import pytest

import motion_params


class TestDefaults:
    def test_defaults(self, isolated_motion_params_config):
        params = motion_params.get_params()
        assert params["D_wheel_rot_mm"] == 52.0
        assert params["ROT_PPR"] == 3200
        assert isinstance(params["ROT_PPR"], int)

    def test_allowed_ppr_matches_tb6600_label_table(self):
        assert motion_params.ALLOWED_ROT_PPR == (200, 400, 800, 1600, 3200, 6400)


class TestOldFileWithoutNewFields:
    def test_old_json_missing_new_fields_falls_back_to_defaults(self, isolated_motion_params_config):
        # NEMA17 öncesi yazılmış dosya: sadece eski 4 alan var
        isolated_motion_params_config.write_text(
            json.dumps({"D_drive_mm": 50.0, "D_wheel_dc_mm": 50.0, "D_rod_mm": 12.0, "RPM_MAX_NOLOAD": 80.0}),
            encoding="utf-8",
        )
        params = motion_params.get_params()
        assert params["D_drive_mm"] == 50.0
        assert params["D_rod_mm"] == 12.0
        assert params["D_wheel_rot_mm"] == 52.0
        assert params["ROT_PPR"] == 3200

    def test_old_file_then_save_new_field_keeps_old_fields(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text(json.dumps({"D_rod_mm": 12.0}), encoding="utf-8")
        motion_params.save_params({"ROT_PPR": 6400})
        params = motion_params.get_params()
        assert params["D_rod_mm"] == 12.0
        assert params["ROT_PPR"] == 6400


class TestValidation:
    @pytest.mark.parametrize("ppr", [200, 400, 800, 1600, 3200, 6400])
    def test_allowed_ppr_saved(self, isolated_motion_params_config, ppr):
        assert motion_params.save_params({"ROT_PPR": ppr})["ROT_PPR"] == ppr
        assert motion_params.get_params()["ROT_PPR"] == ppr

    @pytest.mark.parametrize("ppr", [0, -3200, 100, 1000, 12800, 3200.5, "3200", None, True])
    def test_invalid_ppr_ignored_on_save(self, isolated_motion_params_config, ppr):
        motion_params.save_params({"ROT_PPR": 6400})
        motion_params.save_params({"ROT_PPR": ppr})
        assert motion_params.get_params()["ROT_PPR"] == 6400

    def test_invalid_ppr_in_file_falls_back_to_default(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text(json.dumps({"ROT_PPR": 1234}), encoding="utf-8")
        assert motion_params.get_params()["ROT_PPR"] == 3200

    def test_float_valued_allowed_ppr_in_file_normalized_to_int(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text(json.dumps({"ROT_PPR": 6400.0}), encoding="utf-8")
        params = motion_params.get_params()
        assert params["ROT_PPR"] == 6400 and isinstance(params["ROT_PPR"], int)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_non_positive_wheel_diameter_ignored(self, isolated_motion_params_config, value):
        motion_params.save_params({"D_wheel_rot_mm": 60.0})
        motion_params.save_params({"D_wheel_rot_mm": value})
        assert motion_params.get_params()["D_wheel_rot_mm"] == 60.0


class TestPersistence:
    def test_roundtrip_persists_to_disk(self, isolated_motion_params_config):
        motion_params.save_params({"D_wheel_rot_mm": 48.5, "ROT_PPR": 6400})
        on_disk = json.loads(isolated_motion_params_config.read_text(encoding="utf-8"))
        assert on_disk["D_wheel_rot_mm"] == 48.5
        assert on_disk["ROT_PPR"] == 6400
        assert motion_params.get_params()["D_wheel_rot_mm"] == 48.5

    def test_partial_save_keeps_other_new_field(self, isolated_motion_params_config):
        motion_params.save_params({"ROT_PPR": 1600})
        motion_params.save_params({"D_wheel_rot_mm": 40.0})
        params = motion_params.get_params()
        assert params["ROT_PPR"] == 1600
        assert params["D_wheel_rot_mm"] == 40.0

    def test_atomic_tmp_cleaned_up(self, isolated_motion_params_config):
        motion_params.save_params({"ROT_PPR": 800})
        assert not isolated_motion_params_config.with_suffix(".json.tmp").exists()
