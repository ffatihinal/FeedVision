"""
FeedVision — digit_templates_store.py testleri (Görev B, 2026-09-30).

test_calibration_store.py::TestTemplates ile AYNI desen — iki modül aynı
atomik-yazım/dosya G/Ç mimarisini paylaşıyor. Farklar burada ayrıca test
edilir: bu depo INDEX değil LABEL bazında anahtarlanır ve "ekle" ESKİSİNİN
ÜZERİNE YAZAR (calibration_store'un "biriktir" mantığının aksine).
"""

import digit_templates_store


class TestGetTemplates:
    def test_no_file_returns_empty_dict(self, isolated_digit_templates_store):
        assert digit_templates_store.get_templates("ui_screen") == {}

    def test_corrupt_json_returns_empty_dict(self, isolated_digit_templates_store):
        isolated_digit_templates_store.write_text("{bozuk", encoding="utf-8")
        assert digit_templates_store.get_templates("ui_screen") == {}

    def test_non_dict_json_returns_empty_dict(self, isolated_digit_templates_store):
        isolated_digit_templates_store.write_text("[1, 2, 3]", encoding="utf-8")
        assert digit_templates_store.get_templates("ui_screen") == {}


class TestAddTemplate:
    def test_add_persists_metadata_and_image_file(self, isolated_digit_templates_store):
        entry = digit_templates_store.add_template("ui_screen", "5", b"\xff\xd8\xff\xe0fake-jpeg")
        assert "filename" in entry
        assert "captured_at" in entry

        templates = digit_templates_store.get_templates("ui_screen")
        assert templates == {"5": entry}

        img_path = digit_templates_store.TEMPLATES_DIR / entry["filename"]
        assert img_path.exists()
        assert img_path.read_bytes() == b"\xff\xd8\xff\xe0fake-jpeg"

    def test_add_different_labels_accumulate(self, isolated_digit_templates_store):
        digit_templates_store.add_template("ui_screen", "0", b"zero")
        digit_templates_store.add_template("ui_screen", "1", b"one")
        templates = digit_templates_store.get_templates("ui_screen")
        assert set(templates.keys()) == {"0", "1"}

    def test_add_same_label_twice_replaces_not_accumulates(self, isolated_digit_templates_store):
        first = digit_templates_store.add_template("ui_screen", "5", b"old-image")
        old_path = digit_templates_store.TEMPLATES_DIR / first["filename"]
        assert old_path.exists()

        second = digit_templates_store.add_template("ui_screen", "5", b"new-image")

        templates = digit_templates_store.get_templates("ui_screen")
        assert len(templates) == 1
        assert templates["5"] == second
        # Eski dosya silinmiş olmalı (biriktirme değil, üzerine yazma).
        assert not old_path.exists()
        new_path = digit_templates_store.TEMPLATES_DIR / second["filename"]
        assert new_path.exists()
        assert new_path.read_bytes() == b"new-image"

    def test_dot_and_minus_labels_use_safe_filenames(self, isolated_digit_templates_store):
        dot_entry = digit_templates_store.add_template("ui_screen", ".", b"dot-image")
        minus_entry = digit_templates_store.add_template("ui_screen", "-", b"minus-image")
        assert "dot" in dot_entry["filename"]
        assert "minus" in minus_entry["filename"]
        # JSON'daki label anahtarı orijinal karakteri korumalı (dosya adı değil).
        templates = digit_templates_store.get_templates("ui_screen")
        assert set(templates.keys()) == {".", "-"}

    def test_templates_are_per_camera(self, isolated_digit_templates_store):
        digit_templates_store.add_template("chamber", "3", b"c")
        assert set(digit_templates_store.get_templates("chamber").keys()) == {"3"}
        assert digit_templates_store.get_templates("ui_screen") == {}


class TestReadTemplateImageBytes:
    def test_roundtrip(self, isolated_digit_templates_store):
        entry = digit_templates_store.add_template("ui_screen", "7", b"hello-bytes")
        data = digit_templates_store.read_template_image_bytes("ui_screen", entry["filename"])
        assert data == b"hello-bytes"

    def test_missing_file_returns_none(self, isolated_digit_templates_store):
        assert digit_templates_store.read_template_image_bytes("ui_screen", "nope.jpg") is None


class TestRemoveTemplate:
    def test_removes_metadata_and_file(self, isolated_digit_templates_store):
        entry = digit_templates_store.add_template("ui_screen", "9", b"x")
        img_path = digit_templates_store.TEMPLATES_DIR / entry["filename"]
        assert img_path.exists()

        remaining = digit_templates_store.remove_template("ui_screen", "9")
        assert remaining == {}
        assert digit_templates_store.get_templates("ui_screen") == {}
        assert not img_path.exists()

    def test_unknown_label_is_noop(self, isolated_digit_templates_store):
        digit_templates_store.add_template("ui_screen", "9", b"x")
        remaining = digit_templates_store.remove_template("ui_screen", "8")
        assert len(remaining) == 1
        assert "9" in remaining
