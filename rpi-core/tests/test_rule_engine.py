"""
FeedVision — rule_engine.py testleri.

Bu modül dosya/donanım G/Ç'si yapmıyor (saf mantık), bu yüzden burada
`tmp_path` tabanlı izolasyon fixture'ları GEREKMİYOR — ama conftest.py'deki
fixture'lar ileride main.py/store modülleri test edildiğinde kullanılacak.
"""

from rule_engine import (
    RuleEvalSkipped,
    RuleViolation,
    _evaluate_roi_rule,
    _try_parse_number,
    evaluate_rules,
)

# ---------------------------------------------------------------------------
# _try_parse_number
# ---------------------------------------------------------------------------

class TestTryParseNumber:
    def test_plain_number(self):
        assert _try_parse_number("23.4") == 23.4

    def test_comma_decimal(self):
        assert _try_parse_number("5,2") == 5.2

    def test_whitespace_trimmed(self):
        assert _try_parse_number("  12.0  ") == 12.0

    def test_empty_string_returns_none(self):
        assert _try_parse_number("") is None

    def test_non_numeric_text_returns_none(self):
        # OCR gürültüsü — birim/etiket karışmış metin, kısmi sayı üretilmemeli
        assert _try_parse_number("23.4 C") is None

    def test_multi_number_text_returns_none(self):
        # "12:30" gibi metinlerden yanlış sayı çıkarılmamalı
        assert _try_parse_number("12:30") is None


# ---------------------------------------------------------------------------
# _evaluate_roi_rule
# ---------------------------------------------------------------------------

class TestEvaluateRoiRule:
    def test_roi_read_and_parsed(self):
        rule = {"cam_id": "ui_screen", "roi_name": "basinc"}
        readings = {("ui_screen", "basinc"): {"text": "4.5"}}
        value, error = _evaluate_roi_rule(rule, readings)
        assert value == 4.5
        assert error is None

    def test_roi_not_read_this_frame(self):
        # ROI bu karede hiç okunmamış (kamera/ROI eşleşmedi)
        rule = {"cam_id": "ui_screen", "roi_name": "basinc"}
        value, error = _evaluate_roi_rule(rule, {})
        assert value is None
        assert "okunmadı" in error

    def test_roi_text_not_a_number(self):
        rule = {"cam_id": "ui_screen", "roi_name": "basinc"}
        readings = {("ui_screen", "basinc"): {"text": "ERR"}}
        value, error = _evaluate_roi_rule(rule, readings)
        assert value is None
        assert "sayı değil" in error


# ---------------------------------------------------------------------------
# evaluate_rules
# ---------------------------------------------------------------------------

class TestEvaluateRules:
    def test_happy_path_no_violation(self):
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "stop_motor": False,
            }
        ]
        readings = {("ui_screen", "basinc"): {"text": "4.0"}}
        violations, skipped = evaluate_rules(rules, readings, {})
        assert violations == []
        assert skipped == []

    def test_out_of_range_roi_produces_violation(self):
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "stop_motor": True,
            }
        ]
        readings = {("ui_screen", "basinc"): {"text": "7.5"}}
        violations, _skipped = evaluate_rules(rules, readings, {})
        assert len(violations) == 1
        v = violations[0]
        assert isinstance(v, RuleViolation)
        assert v.value == 7.5
        assert v.stop_motor is True
        assert v.source_label == "ui_screen / basinc"

    def test_ocr_text_not_a_number_is_skipped_not_violated(self):
        # Uç durum: OCR metni sayı değil — ihlal DEĞİL, atlanan olarak dönmeli
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
            }
        ]
        readings = {("ui_screen", "basinc"): {"text": "ERR"}}
        violations, skipped = evaluate_rules(rules, readings, {})
        assert violations == []
        assert len(skipped) == 1
        assert isinstance(skipped[0], RuleEvalSkipped)
        assert skipped[0].rule_id == "r1"

    def test_roi_never_read_is_skipped(self):
        # Uç durum: ROI hiç okunmadı (main.py bu karede o kamerayı/ROI'yi işlemedi)
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
            }
        ]
        violations, skipped = evaluate_rules(rules, {}, {})
        assert violations == []
        assert len(skipped) == 1
        assert "eşleşmedi" in skipped[0].reason

    def test_boundary_values_are_not_violations(self):
        # Uç durum: min/max sınırındaki değer (dahil) ihlal SAYILMAMALI (< / > kıyası)
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
            }
        ]
        for boundary in ("2.0", "6.0"):
            readings = {("ui_screen", "basinc"): {"text": boundary}}
            violations, _skipped = evaluate_rules(rules, readings, {})
            assert violations == [], f"{boundary} sınırda, ihlal olmamalı"

    def test_disabled_rule_is_ignored(self):
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "enabled": False,
            }
        ]
        readings = {("ui_screen", "basinc"): {"text": "99"}}
        violations, skipped = evaluate_rules(rules, readings, {})
        assert violations == []
        assert skipped == []

    def test_stm_timeout_scenario_connection_lost(self):
        # STM32 bağlantısı kopmuş — "sonsuz" beklemiş sayılır, eşik aşılmış kabul edilir
        rules = [
            {
                "id": "r2",
                "name": "STM Bağlantı",
                "source": "stm",
                "timeout_s": 5,
                "stop_motor": True,
            }
        ]
        stm32_meta = {"is_connected": False}
        violations, _skipped = evaluate_rules(rules, {}, {}, stm32_meta)
        assert len(violations) == 1
        assert violations[0].value == 6.0  # timeout_s + 1
        assert violations[0].source_label == "stm / bağlantı"

    def test_stm_timeout_scenario_within_limit(self):
        # Bağlı ve son durum satırı eşik altında — ihlal yok
        rules = [
            {
                "id": "r2",
                "name": "STM Bağlantı",
                "source": "stm",
                "timeout_s": 5,
                "stop_motor": True,
            }
        ]
        stm32_meta = {"is_connected": True, "status_age_s": 1.2}
        violations, skipped = evaluate_rules(rules, {}, {}, stm32_meta)
        assert violations == []
        assert skipped == []

    def test_stm_no_status_yet_is_skipped(self):
        # Uç durum: bağlı ama henüz hiç durum satırı gelmemiş
        rules = [
            {
                "id": "r2",
                "name": "STM Bağlantı",
                "source": "stm",
                "timeout_s": 5,
            }
        ]
        stm32_meta = {"is_connected": True, "status_age_s": None}
        violations, skipped = evaluate_rules(rules, {}, {}, stm32_meta)
        assert violations == []
        assert len(skipped) == 1
        assert "durum satırı gelmedi" in skipped[0].reason

    def test_unknown_source_is_skipped(self):
        rules = [{"id": "r3", "name": "Bilinmeyen", "source": "mystery"}]
        violations, skipped = evaluate_rules(rules, {}, {})
        assert violations == []
        assert len(skipped) == 1
        assert "Bilinmeyen kriter kaynağı" in skipped[0].reason


# ---------------------------------------------------------------------------
# Fail-safe: stop_motor=True kriter okunamazsa ihlal sayılmalı (29-09-2026,
# sahada teyit edilen güvenlik bug'ı — kamera önü elle kapatıldığında motor
# durmuyordu)
# ---------------------------------------------------------------------------

class TestFailSafeUnreadableStopMotorRule:
    def test_roi_unreadable_with_stop_motor_true_is_a_violation(self):
        # Senaryo 1: ROI okunamıyor (kamera önü kapalı) + stop_motor=True
        # -> artık skipped değil, violations'da — motor durmalı.
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "stop_motor": True,
            }
        ]
        violations, skipped = evaluate_rules(rules, {}, {})
        assert skipped == []
        assert len(violations) == 1
        v = violations[0]
        assert v.rule_id == "r1"
        assert v.value is None
        assert v.stop_motor is True
        assert v.error is not None and "okunmadı" in v.error

    def test_roi_unreadable_with_stop_motor_false_is_skipped_not_violated(self):
        # Senaryo 2: aynı okunamama durumu ama stop_motor=False -> davranış
        # AYNI kalmalı, hâlâ sadece skipped (bilgilendirme amaçlı).
        rules = [
            {
                "id": "r1",
                "name": "Basınç (alarm-only)",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "stop_motor": False,
            }
        ]
        violations, skipped = evaluate_rules(rules, {}, {})
        assert violations == []
        assert len(skipped) == 1
        assert skipped[0].rule_id == "r1"

    def test_roi_readable_in_range_with_stop_motor_true_no_violation(self):
        # Senaryo 3: normal okunuyor, aralık içinde -> regresyon olmamalı,
        # ne violation ne skip.
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "stop_motor": True,
            }
        ]
        readings = {("ui_screen", "basinc"): {"text": "4.0"}}
        violations, skipped = evaluate_rules(rules, readings, {})
        assert violations == []
        assert skipped == []

    def test_roi_readable_out_of_range_with_stop_motor_true_violation_has_no_error(self):
        # Senaryo 4: "aralık dışı" ihlali "okunamadı" mesajıyla karışmamalı —
        # value dolu, error None kalmalı (regresyon olmasın).
        rules = [
            {
                "id": "r1",
                "name": "Basınç",
                "source": "roi",
                "cam_id": "ui_screen",
                "roi_name": "basinc",
                "min": 2,
                "max": 6,
                "stop_motor": True,
            }
        ]
        readings = {("ui_screen", "basinc"): {"text": "7.5"}}
        violations, skipped = evaluate_rules(rules, readings, {})
        assert skipped == []
        assert len(violations) == 1
        v = violations[0]
        assert v.value == 7.5
        assert v.error is None
        assert v.stop_motor is True

    def test_stm_no_status_yet_with_stop_motor_true_is_a_violation(self):
        # Senaryo 5: STM32'den hiç durum gelmemiş (_evaluate_stm_rule de aynı
        # value is None yoluna giriyor) + stop_motor=True -> ihlal, motor durmalı.
        rules = [
            {
                "id": "r2",
                "name": "STM Bağlantı",
                "source": "stm",
                "timeout_s": 5,
                "stop_motor": True,
            }
        ]
        stm32_meta = {"is_connected": True, "status_age_s": None}
        violations, skipped = evaluate_rules(rules, {}, {}, stm32_meta)
        assert skipped == []
        assert len(violations) == 1
        v = violations[0]
        assert v.value is None
        assert v.stop_motor is True
        assert v.error is not None and "durum satırı gelmedi" in v.error

    def test_stm_no_status_yet_with_stop_motor_false_is_skipped(self):
        # Aynı STM senaryosu ama stop_motor=False -> davranış AYNI kalmalı.
        rules = [
            {
                "id": "r2",
                "name": "STM Bağlantı (alarm-only)",
                "source": "stm",
                "timeout_s": 5,
                "stop_motor": False,
            }
        ]
        stm32_meta = {"is_connected": True, "status_age_s": None}
        violations, skipped = evaluate_rules(rules, {}, {}, stm32_meta)
        assert violations == []
        assert len(skipped) == 1
        assert skipped[0].rule_id == "r2"
