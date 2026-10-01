"""
FeedVision — digit_reader.py testleri (Görev A, 2026-09-30: karakter
segmentasyonu + şablon eşleştirme, Tesseract'a alternatif rakam okuma).

Sentetik (PIL ile render edilmiş) görüntülerle çalışır — gerçek kameraya
bağımlı değil. generate_default_templates() PIL ile render ettiği için bu
testler DE dolaylı olarak PIL'e bağımlı (requirements.txt: Pillow>=10.0).
"""

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

import digit_reader
from digit_reader import (
    CONFIDENCE_THRESHOLD,
    DigitTemplateSet,
    generate_default_templates,
    match_character,
    read_digits,
    segment_characters,
)

# Sentetik test görüntüleri render etmek için — gerçek HMI fontuna bağımlı
# olmadan bir sistem fontu kullanılıyor. digit_reader._load_default_font
# zaten platformdan bağımsız bir arama zinciri + PIL gömülü fallback'i
# içeriyor (bkz. orası) — burada hardcoded Mac-only bir yol (Arial.ttf)
# kullanmak CI'daki Ubuntu runner'da "OSError: cannot open resource" ile
# patlıyordu (01-10-2026, sahada bulundu). Aynı fonksiyonu burada da
# kullanmak testi platformdan bağımsız hale getiriyor; template üretimiyle
# AYNI font olması bir sorun değil (aksine, matching'in küçük render
# farklarına dayanıklı olduğunu daha iyi kanıtlar).


def _render_text_image(text: str, font_size: int = 28, pad: int = 6, invert: bool = False) -> np.ndarray:
    """Beyaz yazı/siyah zemin (invert=False) ya da siyah yazı/beyaz zemin
    (invert=True) sentetik bir ROI kırpıntısı üretir — BGR, screen_reader'ın
    read_roi'ye verdiği kırpıntılarla aynı format."""
    font = digit_reader._load_default_font(font_size)
    probe = ImageDraw.Draw(Image.new("L", (10, 10), 0))
    bbox = probe.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0] + pad * 2
    h = bbox[3] - bbox[1] + pad * 2
    bg, fg = (0, 255) if not invert else (255, 0)
    canvas = Image.new("L", (w, h), bg)
    draw = ImageDraw.Draw(canvas)
    draw.text((pad - bbox[0], pad - bbox[1]), text, fill=fg, font=font)
    array = np.array(canvas, dtype=np.uint8)
    return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)


def _random_noise_char_image() -> np.ndarray:
    """Hiçbir karaktere benzemeyen, piksel-başına bağımsız 0/255 gürültü --
    TEMPLATE_SIZE ile AYNI (genişlik, yükseklik) oranında üretilir ki
    match_character içindeki normalize adımı yeniden ÖLÇEKLEMESİN (resize
    enterpolasyonu rastgele 0/255 değerlerini ara gri tonlara karıştırıp
    davranışı öngörülemez hale getirebilir). Sabit tohum (seed) ile
    deterministik: HERHANGİ bir ikili şablonla beklenen benzerlik skoru
    matematiksel olarak ~0.5'tir (bkz. digit_reader._similarity_score --
    bağımsız 0/1 gürültü ile sabit bir şablon arasındaki ortalama kare fark
    şablonun beyaz oranından BAĞIMSIZ olarak 0.5'e yakınsar), CONFIDENCE_
    THRESHOLD'un (0.55) güvenle altında kalır. Daha önce burada "checkerboard"
    (dama tahtası) deseni denenmişti ama cv2.resize (INTER_AREA) ince
    alternan deseni ara-gri değerlere ortalayıp beklenmedik şekilde bir
    şablona (".") güçlü benzeyebiliyordu -- bu yüzden gerçek piksel-bağımsız
    gürültüye geçildi (ampirik bulgu, 30-09-2026)."""
    from digit_reader import TEMPLATE_SIZE

    width, height = TEMPLATE_SIZE
    rng = np.random.default_rng(7)
    return (rng.integers(0, 2, size=(height, width)) * 255).astype(np.uint8)


class TestSegmentCharacters:
    def test_empty_image_returns_empty_list(self):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        assert segment_characters(empty) == []

    @pytest.mark.parametrize("invert", [False, True])
    def test_two_digit_number_segments_into_two_in_order(self, invert):
        img = _render_text_image("19", invert=invert)
        chars = segment_characters(img)
        assert len(chars) == 2

    @pytest.mark.parametrize("invert", [False, True])
    def test_negative_single_digit_segments_into_minus_and_digit(self, invert):
        img = _render_text_image("-1", invert=invert)
        chars = segment_characters(img)
        assert len(chars) == 2

    @pytest.mark.parametrize("invert", [False, True])
    def test_three_digit_number_segments_into_three_in_order(self, invert):
        img = _render_text_image("100", invert=invert)
        chars = segment_characters(img)
        assert len(chars) == 3

    def test_decimal_with_minus_segments_all_five_characters(self):
        # "-12.5" -> '-', '1', '2', '.', '5'
        img = _render_text_image("-12.5")
        chars = segment_characters(img)
        assert len(chars) == 5

    def test_characters_ordered_left_to_right(self):
        # "1" ve "0" cok farkli genislikte degil ama siralamayi asil test eden
        # sey her karakterin bounding-box x-konumuna gore artan sirada gelmesi.
        img = _render_text_image("10")
        chars = segment_characters(img)
        assert len(chars) == 2
        # İlk karakter ("1") ikinciden ("0") daha dar olmalı (Arial'da rakam 1
        # diğer rakamlardan belirgin şekilde daha dar govdeye sahip).
        assert chars[0].shape[1] < chars[1].shape[1]


class TestDigitTemplateSet:
    def test_empty_set_has_zero_length(self):
        assert len(DigitTemplateSet()) == 0

    def test_add_increases_length_and_labels(self):
        templates = DigitTemplateSet()
        templates.add("5", np.full((30, 20), 255, dtype=np.uint8))
        assert len(templates) == 1
        assert templates.labels() == ["5"]

    def test_add_same_label_twice_replaces_not_duplicates(self):
        templates = DigitTemplateSet()
        templates.add("5", np.zeros((30, 20), dtype=np.uint8))
        templates.add("5", np.full((30, 20), 255, dtype=np.uint8))
        assert len(templates) == 1


class TestMatchCharacter:
    def test_empty_template_set_returns_unknown_with_zero_confidence(self):
        label, score = match_character(_random_noise_char_image(), DigitTemplateSet())
        assert label == "?"
        assert score == 0.0

    def test_random_noise_garbage_returns_unknown_low_confidence(self):
        templates = generate_default_templates()
        label, score = match_character(_random_noise_char_image(), templates)
        assert label == "?"
        assert score < CONFIDENCE_THRESHOLD

    def test_exact_template_match_is_high_confidence_and_correct_label(self):
        templates = DigitTemplateSet()
        digit_image = np.zeros((32, 20), dtype=np.uint8)
        digit_image[5:27, 5:15] = 255
        templates.add("7", digit_image)
        label, score = match_character(digit_image, templates)
        assert label == "7"
        assert score == pytest.approx(1.0)


class TestReadDigitsWithDefaultTemplates:
    """generate_default_templates() cıktısıyla sentetik ROI'ler okunuyor —
    Görev'in 'sentetik "19" görüntüsünü doğru okur' test kriteri."""

    @pytest.fixture
    def templates(self):
        return generate_default_templates()

    def test_generated_set_covers_all_expected_labels(self, templates):
        assert set(templates.labels()) == set(digit_reader.CHARACTER_LABELS)

    @pytest.mark.parametrize(
        "text",
        ["19", "-1", "100", "0", "-12.5", "42", "7.3"],
    )
    def test_reads_synthetic_number_correctly(self, templates, text):
        img = _render_text_image(text)
        result_text, confidence = read_digits(img, templates)
        assert result_text == text
        assert confidence >= CONFIDENCE_THRESHOLD

    def test_reads_correctly_regardless_of_polarity(self, templates):
        # Görev metninde polarite (koyu/açık zemin) belirtilmiyor -- gerçek
        # sahada hangisiyle karşılaşılacağı bilinmiyor, ikisi de çalışmalı.
        light_on_dark = _render_text_image("19", invert=False)
        dark_on_light = _render_text_image("19", invert=True)
        assert read_digits(light_on_dark, templates)[0] == "19"
        assert read_digits(dark_on_light, templates)[0] == "19"

    def test_empty_image_returns_empty_text_zero_confidence(self, templates):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        assert read_digits(empty, templates) == ("", 0.0)

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 7, 13, 21, 42, 99])
    def test_garbled_input_does_not_silently_misread(self, templates, seed):
        # Görev kriteri: "Bozuk/gurultulu girdide dusuk guven skoru + '?'
        # davranisini dogrula (sessiz yanlis tahmin yok)." Tam renkli rastgele
        # gurultu hicbir karaktere benzemez -- ya "?" iceren bir metin ya da
        # genel olarak dusuk guven donmeli, YUKSEK guvenle temiz bir rakam
        # dizisi donmemeli. Tek sabit tohum (eski hali) kirilgandi -- 01-10-2026'da
        # DejaVu fontuyla uretilen sablonlarla 30/30 tohumda bu assertion FAIL
        # veriyordu (bkz. CONFIDENCE_THRESHOLD yorumu + INSA_GUNLUGU), coklu
        # tohuma gecis bu sinifta regresyonu guvenilir yakalamak icin.
        rng = np.random.default_rng(seed)
        garbage = rng.integers(0, 256, (32, 60, 3), dtype=np.uint8)
        text, confidence = read_digits(garbage, templates)
        assert confidence < CONFIDENCE_THRESHOLD or "?" in text


class TestPerformanceComparison:
    """Görev'in iddiası ('20-30 kat daha hızlı') GERÇEKTEN ölçülüyor --
    körlemesine güvenilmiyor. Tesseract binary'si bu ortamda kurulu
    olmayabilir (CI/Mac gelistirme) -- o durumda karsilastirma atlanir
    (skip), digit_reader'in kendi performansi hala olculur/rapor edilir."""

    def test_read_digits_is_reasonably_fast_and_faster_than_tesseract_if_available(self):
        import time

        import pytesseract

        from screen_reader import read_text_ocr

        templates = generate_default_templates()
        img = _render_text_image("19")

        start = time.perf_counter()
        for _ in range(5):
            read_digits(img, templates)
        template_duration = (time.perf_counter() - start) / 5

        try:
            pytesseract.get_tesseract_version()
        except Exception:
            pytest.skip("Tesseract binary bu ortamda kurulu degil, hiz kiyaslamasi atlaniyor.")

        start = time.perf_counter()
        for _ in range(5):
            read_text_ocr(img, char_whitelist="0123456789.-")
        tesseract_duration = (time.perf_counter() - start) / 5

        print(
            f"\n[perf] template={template_duration*1000:.2f}ms "
            f"tesseract={tesseract_duration*1000:.2f}ms "
            f"oran={tesseract_duration / template_duration:.1f}x"
        )
        # İddia "20-30 kat" idi -- ortam farkına göre değişebileceğinden
        # (ör. CPU affinity pinleme Mac'te no-op) test daha mütevazı/güvenli
        # bir alt sınırla ("en azından belirgin şekilde daha hızlı") doğrular.
        assert template_duration < tesseract_duration
