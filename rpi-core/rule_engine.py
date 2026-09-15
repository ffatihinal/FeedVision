"""
FeedVision — İzleme + Kural Motoru (madde 1: ROI kural mantığı, madde 2:
güvenlik interlock, madde 7: aralık dışı alarm — üçü TEK motor)

Ne yapar: operatörün tanımladığı kurallar (ör. "cam2/basinc ROI'si 2-6 bar
aralığında olmalı") ile o an okunan değerleri (ROI OCR metni ya da STM32
durum alanı) karşılaştırır, aralık dışına çıkanları "ihlal" olarak döner.
main.py bu ihlalleri kullanarak motoru durdurur + UI'da alarm gösterir
(bkz. main.py'deki periyodik kontrol döngüsü).

Neden tek motor: RIO/ROI kural mantığı, motor-durduran interlock ve "aralık
dışı değer alarmı" aslında aynı sorunun üç yüzü — "izlenen bir değerin kuralı
var, ihlal olunca ne olur" sorusu. Ayrı ayrı yazmak tutarsız davranışa yol
açardı (ör. interlock'un baktığı eşikle alarmın baktığı eşik farklı yerlerde
tanımlı olabilirdi).

Bu dosya SADECE saf değerlendirme mantığını içerir — kamera/seri port G/Ç'si
main.py'de kalıyor, bu yüzden donanımsız test edilebilir.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuleViolation:
    """Bir kuralın ihlal edildiği anki bilgisi — UI banner'ı ve motor
    durdurma kararı bu nesneden besleniyor."""

    rule_id: str
    rule_name: str
    value: float
    min: float | None
    max: float | None
    stop_motor: bool
    source_label: str  # ör. "cam2 / basinc" ya da "stm32 / e1" — kullanıcıya gösterilecek kısa açıklama


@dataclass
class RuleEvalSkipped:
    """Bir kural DEĞERLENDİRİLEMEDİ (ihlal değil) — ör. OCR metni sayıya
    çevrilemedi, ya da STM32 durumunda o alan henüz gelmedi.

    Neden ihlal SAYILMIYOR: okunamayan bir değeri "aralık dışı" sanıp motoru
    durdurmak, gerçek bir arızadan daha kötü bir yanlış pozitif olurdu (bkz.
    proje notu: "sessizce hatalı veri üretmek, hiç okumamaktan daha kötü").
    Bunun yerine ayrı bir "okunamadı" listesi olarak taşınır, operatör
    görebilir ama motor durmaz.
    """

    rule_id: str
    rule_name: str
    reason: str


def _try_parse_number(text: str) -> float | None:
    """OCR'dan gelen serbest metinden bir ondalık sayı çıkarmayı dener.

    Tesseract çoğu zaman etrafında birim/boşluk/gürültü karakteriyle birlikte
    metin döner (ör. "23.4 C", " 5,2bar") — burada sadece basit bir temizlik
    yapılıyor (virgül->nokta, izin verilmeyen karakterleri at). Karmaşık/
    çok-sayılı metinlerde (ör. "12:30") yanlış sayı çıkarma riskini azaltmak
    için TÜM metin sayıya çevrilemiyorsa None dönülür — kısmi/tahmin ile
    sayı üretilmez (yanlış pozitif riski, bkz. RuleEvalSkipped notu).
    """
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_value(rule: dict[str, Any], roi_readings: dict, stm32_status: dict) -> tuple[float | None, str | None]:
    """Bir kural için o anki değeri okur.

    roi_readings: main.py'nin ürettiği {(cam_id, roi_name): {"text": ..., "avg_color_hsv": ...}} sözlüğü.
    stm32_status: bridge.get_status() çıktısı (ör. {"e1": 120, "e2": 118, "t": 4521, ...}).

    Döner: (deger, hata_mesaji) — deger bulunduysa hata_mesaji None, aksi halde deger None.
    """
    source = rule.get("source")
    if source == "roi":
        key = (rule.get("cam_id"), rule.get("roi_name"))
        reading = roi_readings.get(key)
        if reading is None:
            return None, f"ROI '{rule.get('roi_name')}' bu karede okunmadı (kamera/ROI eşleşmedi)"
        text = reading.get("text", "")
        value = _try_parse_number(text)
        if value is None:
            return None, f"OCR metni sayı değil: \"{text}\""
        return value, None
    if source == "stm32":
        field_name = rule.get("field")
        if field_name not in stm32_status:
            return None, f"STM32 durumunda '{field_name}' alanı yok (henüz veri gelmemiş olabilir)"
        raw = stm32_status[field_name]
        if not isinstance(raw, (int, float)):
            return None, f"STM32 alanı '{field_name}' sayısal değil: {raw!r}"
        return float(raw), None
    return None, f"Bilinmeyen kural kaynağı: {source!r}"


def _source_label(rule: dict[str, Any]) -> str:
    if rule.get("source") == "roi":
        return f"{rule.get('cam_id')} / {rule.get('roi_name')}"
    return f"stm32 / {rule.get('field')}"


def evaluate_rules(
    rules: list[dict[str, Any]],
    roi_readings: dict,
    stm32_status: dict,
) -> tuple[list[RuleViolation], list[RuleEvalSkipped]]:
    """Tüm AKTİF (enabled=True) kuralları değerlendirir.

    Döner: (ihlaller, atlananlar). İhlaller motor durdurma + alarm için
    kullanılır; atlananlar sadece bilgi amaçlı (operatöre "şu kural şu an
    okunamıyor" göstermek için) — motor kararını ETKİLEMEZ.
    """
    violations: list[RuleViolation] = []
    skipped: list[RuleEvalSkipped] = []

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        rule_id = rule.get("id", "")
        rule_name = rule.get("name", rule_id)

        value, error = _extract_value(rule, roi_readings, stm32_status)
        if value is None:
            skipped.append(RuleEvalSkipped(rule_id=rule_id, rule_name=rule_name, reason=error or "bilinmeyen hata"))
            continue

        min_v = rule.get("min")
        max_v = rule.get("max")
        out_of_range = (min_v is not None and value < min_v) or (max_v is not None and value > max_v)
        if out_of_range:
            violations.append(
                RuleViolation(
                    rule_id=rule_id,
                    rule_name=rule_name,
                    value=value,
                    min=min_v,
                    max=max_v,
                    stop_motor=bool(rule.get("stop_motor", False)),
                    source_label=_source_label(rule),
                )
            )

    return violations, skipped
