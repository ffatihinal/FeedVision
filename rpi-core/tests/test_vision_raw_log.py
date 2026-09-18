"""
FeedVision — vision_raw_log.py testleri.

Bu modülün gerçek üretim dosyaları LOG_DIR ("görüntü işleme raw data"
klasörü) ve CONFIG_PATH (vision_raw_log_config.json) — testler ikisini de
tmp_path'e yönlendirir (conftest.py'de bu modüle özel bir ortak fixture
yok, doğrudan burada tanımlanıyor çünkü hem interval config hem log dizini
ayrı ayrı izole edilmesi gerekiyor).
"""

import json

import pytest

import vision_raw_log
from vision_raw_log import VisionRawLogger, format_header, format_row, get_interval_s, set_interval_s


@pytest.fixture
def isolated_interval_config(tmp_path):
    return tmp_path / "vision_raw_log_config.json"


class TestGetSetIntervalS:
    def test_no_file_returns_default(self, isolated_interval_config):
        assert get_interval_s(isolated_interval_config) == vision_raw_log.DEFAULT_INTERVAL_S

    def test_corrupt_json_returns_default(self, isolated_interval_config):
        isolated_interval_config.write_text("{bozuk", encoding="utf-8")
        assert get_interval_s(isolated_interval_config) == vision_raw_log.DEFAULT_INTERVAL_S

    def test_negative_or_zero_value_returns_default(self, isolated_interval_config):
        isolated_interval_config.write_text(json.dumps({"interval_s": 0}), encoding="utf-8")
        assert get_interval_s(isolated_interval_config) == vision_raw_log.DEFAULT_INTERVAL_S

    def test_set_then_get_roundtrip(self, isolated_interval_config):
        set_interval_s(5.0, isolated_interval_config)
        assert get_interval_s(isolated_interval_config) == 5.0

    def test_set_rejects_non_positive_value(self, isolated_interval_config):
        with pytest.raises(ValueError):
            set_interval_s(0, isolated_interval_config)
        with pytest.raises(ValueError):
            set_interval_s(-1.0, isolated_interval_config)


class TestFormatHeaderRow:
    def test_format_header_pads_columns_to_fixed_width(self):
        header = format_header(["basinc", "sicaklik"])
        assert "Zaman" in header
        assert "basinc" in header
        # sütun genişliği COLUMN_WIDTH'e göre hizalanmış olmalı
        parts = header.split(" | ")
        assert len(parts[1]) == vision_raw_log.COLUMN_WIDTH

    def test_format_row_truncates_long_value(self):
        long_value = "x" * 50
        row = format_row("2026-09-18 10:00:00", ["basinc"], {"basinc": long_value})
        parts = row.split(" | ")
        assert len(parts[1]) == vision_raw_log.COLUMN_WIDTH

    def test_format_row_missing_value_shows_dash(self):
        row = format_row("2026-09-18 10:00:00", ["basinc"], {})
        assert "—" in row

    def test_format_row_empty_string_value_shows_dash(self):
        row = format_row("2026-09-18 10:00:00", ["basinc"], {"basinc": ""})
        assert "—" in row


class TestVisionRawLoggerWriteEntry:
    def test_first_write_creates_dir_and_header(self, tmp_path):
        log_dir = tmp_path / "logs"
        logger = VisionRawLogger(log_dir=log_dir)
        logger.write_entry(["basinc"], {"basinc": "4.5 bar"})

        files = list(log_dir.glob("*.txt"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        lines = content.splitlines()
        assert "basinc" in lines[0]
        assert lines[1].startswith("---")  # ayırıcı satır
        assert "4.5 bar" in lines[2]

    def test_same_columns_does_not_repeat_header(self, tmp_path):
        log_dir = tmp_path / "logs"
        logger = VisionRawLogger(log_dir=log_dir)
        logger.write_entry(["basinc"], {"basinc": "4.5"})
        logger.write_entry(["basinc"], {"basinc": "4.6"})

        files = list(log_dir.glob("*.txt"))
        content = files[0].read_text(encoding="utf-8")
        assert content.count("Zaman") == 1

    def test_column_change_writes_new_header(self, tmp_path):
        log_dir = tmp_path / "logs"
        logger = VisionRawLogger(log_dir=log_dir)
        logger.write_entry(["basinc"], {"basinc": "4.5"})
        logger.write_entry(["basinc", "sicaklik"], {"basinc": "4.5", "sicaklik": "60"})

        files = list(log_dir.glob("*.txt"))
        content = files[0].read_text(encoding="utf-8")
        assert content.count("Zaman") == 2

    def test_appends_across_instances_same_day(self, tmp_path):
        log_dir = tmp_path / "logs"
        VisionRawLogger(log_dir=log_dir).write_entry(["basinc"], {"basinc": "1"})
        # yeni bir instance (servis restart senaryosu) header'ı bilmiyor,
        # gün içinde aynı sütun düzeni olsa da tekrar header basmalı
        # (kendi _last_columns'u None ile başlar).
        VisionRawLogger(log_dir=log_dir).write_entry(["basinc"], {"basinc": "2"})

        files = list(log_dir.glob("*.txt"))
        content = files[0].read_text(encoding="utf-8")
        assert content.count("Zaman") == 2
