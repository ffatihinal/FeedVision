"""
FeedVision — rules_store.py testleri.

Gerçek rules_config.json'a dokunmamak için `isolated_rules_config` fixture'ı
(conftest.py) kullanılır — CONFIG_PATH'i tmp_path'e yönlendirir.
"""

import json

import rules_store


class TestGetRules:
    def test_no_file_returns_empty_list(self, isolated_rules_config):
        assert rules_store.get_rules() == []

    def test_corrupt_json_returns_empty_list(self, isolated_rules_config):
        isolated_rules_config.write_text("{bozuk", encoding="utf-8")
        assert rules_store.get_rules() == []

    def test_non_list_json_returns_empty_list(self, isolated_rules_config):
        isolated_rules_config.write_text('{"a": 1}', encoding="utf-8")
        assert rules_store.get_rules() == []

    def test_roi_rule_with_legacy_cam_id_migrated(self, isolated_rules_config):
        data = [{"id": "r1", "source": "roi", "cam_id": "cam2", "roi_name": "sicaklik"}]
        isolated_rules_config.write_text(json.dumps(data), encoding="utf-8")
        rules = rules_store.get_rules()
        assert rules[0]["cam_id"] == "ui_screen"

    def test_roi_rule_with_new_cam_id_unchanged(self, isolated_rules_config):
        data = [{"id": "r1", "source": "roi", "cam_id": "chamber", "roi_name": "basinc"}]
        isolated_rules_config.write_text(json.dumps(data), encoding="utf-8")
        rules = rules_store.get_rules()
        assert rules[0]["cam_id"] == "chamber"

    def test_legacy_stm32_rule_migrated_to_stm_with_default_timeout(self, isolated_rules_config):
        data = [{"id": "r2", "source": "stm32", "field": "remaining"}]
        isolated_rules_config.write_text(json.dumps(data), encoding="utf-8")
        rules = rules_store.get_rules()
        rule = rules[0]
        assert rule["source"] == "stm"
        assert "field" not in rule
        assert rule["timeout_s"] == 10.0

    def test_legacy_stm32_rule_keeps_existing_timeout_if_present(self, isolated_rules_config):
        data = [{"id": "r3", "source": "stm32", "field": "remaining", "timeout_s": 30.0}]
        isolated_rules_config.write_text(json.dumps(data), encoding="utf-8")
        rules = rules_store.get_rules()
        assert rules[0]["timeout_s"] == 30.0

    def test_stm_rule_without_source_change_untouched(self, isolated_rules_config):
        data = [{"id": "r4", "source": "stm", "timeout_s": 5.0}]
        isolated_rules_config.write_text(json.dumps(data), encoding="utf-8")
        rules = rules_store.get_rules()
        assert rules[0] == {"id": "r4", "source": "stm", "timeout_s": 5.0}

    def test_migration_does_not_rewrite_file_on_disk(self, isolated_rules_config):
        data = [{"id": "r1", "source": "stm32", "field": "remaining"}]
        isolated_rules_config.write_text(json.dumps(data), encoding="utf-8")
        rules_store.get_rules()
        on_disk = json.loads(isolated_rules_config.read_text(encoding="utf-8"))
        assert on_disk[0]["source"] == "stm32"  # dosya değişmedi, sadece bellek içi görünüm


class TestSaveRules:
    def test_save_then_get_roundtrip(self, isolated_rules_config):
        rules = [{"id": "r1", "source": "stm", "timeout_s": 5.0}]
        rules_store.save_rules(rules)
        assert rules_store.get_rules() == rules

    def test_save_replaces_entire_list(self, isolated_rules_config):
        rules_store.save_rules([{"id": "old"}])
        rules_store.save_rules([{"id": "new"}])
        assert rules_store.get_rules() == [{"id": "new"}]

    def test_save_empty_list_clears_all_rules(self, isolated_rules_config):
        rules_store.save_rules([{"id": "r1"}])
        rules_store.save_rules([])
        assert rules_store.get_rules() == []

    def test_save_writes_atomic_tmp_file_cleaned_up(self, isolated_rules_config):
        rules_store.save_rules([{"id": "r1"}])
        tmp_path = isolated_rules_config.with_suffix(".json.tmp")
        assert not tmp_path.exists()
        assert isolated_rules_config.exists()
