"""
FeedVision — journal.py testleri.

Gerçek `rpi-core/journal/` klasörüne dokunmamak için `write_entry`'ye her
zaman tmp_path tabanlı bir `journal_dir` parametresi geçiriyoruz (modülün
kendi imzası buna izin veriyor, conftest.py'de ayrı bir fixture'a gerek yok).
"""

import json

from journal import write_entry


class TestWriteEntry:
    def test_creates_dir_and_file(self, tmp_path):
        journal_dir = tmp_path / "journal"
        write_entry({"stm32": {"remaining": 100}}, journal_dir=journal_dir)

        files = list(journal_dir.glob("*.jsonl"))
        assert len(files) == 1

    def test_entry_gets_timestamp_field(self, tmp_path):
        journal_dir = tmp_path / "journal"
        write_entry({"foo": "bar"}, journal_dir=journal_dir)

        line = list(journal_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert "timestamp" in record
        assert record["foo"] == "bar"

    def test_multiple_writes_append_as_separate_lines(self, tmp_path):
        journal_dir = tmp_path / "journal"
        write_entry({"n": 1}, journal_dir=journal_dir)
        write_entry({"n": 2}, journal_dir=journal_dir)

        lines = list(journal_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["n"] == 1
        assert json.loads(lines[1])["n"] == 2

    def test_entry_with_turkish_chars_preserved(self, tmp_path):
        journal_dir = tmp_path / "journal"
        write_entry({"kural_ihlali": "basınç sınırı aşıldı"}, journal_dir=journal_dir)

        line = list(journal_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["kural_ihlali"] == "basınç sınırı aşıldı"

    def test_empty_entry_still_writes_timestamp_only_record(self, tmp_path):
        journal_dir = tmp_path / "journal"
        write_entry({}, journal_dir=journal_dir)

        line = list(journal_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert set(record.keys()) == {"timestamp"}

    def test_filename_uses_todays_date(self, tmp_path):
        from datetime import datetime

        journal_dir = tmp_path / "journal"
        write_entry({"n": 1}, journal_dir=journal_dir)

        today = datetime.now().strftime("%Y-%m-%d")
        assert (journal_dir / f"{today}.jsonl").exists()
