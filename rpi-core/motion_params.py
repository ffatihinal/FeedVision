"""
FeedVision — Hareket parametreleri kalıcı deposu (motion_params.json)

Ne yapar: step/DC motor fiziksel birim dönüşümlerinde (mm/s, mm, RPM <-> ham
firmware komutları — bkz. motion_calc.py) kullanılan donanım ölçülerini
saklar/okur: tahrik tekerleği çapı (step+DC ortak, D_drive_mm), DC motorun
sürtünme tekerleği çapı (D_wheel_dc_mm — D_drive ile aynı değer olması
beklenir ama ayrı alan, "özdeş" olsa da garanti olsun diye), besleme çubuğu
çapı (D_rod_mm), DC motorun %100 duty'deki tahmini boşta dönüş hızı
(RPM_MAX_NOLOAD — sahada ölçülene kadar placeholder), NEMA17 dönüş
ekseninin sürtünme tekerleği çapı (D_wheel_rot_mm) ve TB6600 sürücünün
darbe/tur ayarı (ROT_PPR — DIP switch ile AYNI olmalı, bkz. aşağıdaki not).

Neden server-side: bu değerler artık SADECE görsel (ör. admin.html'deki
"Hareket Analizi" grafik teker çapları, hâlâ localStorage'da ayrı duruyor)
değil, GERÇEK komut hesaplamasında kullanılıyor — tarayıcı localStorage'ı
farklı cihaz/tarayıcı açılışlarında kaybolur, bu da yanlış motor komutuna
yol açar (23-09-2026, Sistem Müh. + Elektronik spesifikasyonu).

Aynı desen: calibration_store.py / roi_store.py (atomik yazım, bozuk/eksik
dosyada sessizce varsayılanlara dönme, tek paylaşılan JSON dosyası).
"""

import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "motion_params.json"

_lock = threading.Lock()

# TB6600 etiket tablosundaki darbe/tur değerleri (200 tam adım × mikroadım
# çarpanı: 1, 2, 4, 8, 16, 32). ROT_PPR SADECE bunlardan biri olabilir; TB6600
# üzerindeki S1-S3 DIP ayarıyla AYNI olmalı (1/16 = 3200: S1=OFF,S2=OFF,S3=ON,
# 1/32 = 6400). Koda gömülü bir varsayım değil, Admin'den değiştirilebilir
# parametre — DM556'da koddaki varsayım ile DIP uyuşmayınca 32 kat hata
# yaşandı (05-10-2026), bu tuzak tekrarlanmasın diye.
ALLOWED_ROT_PPR: tuple[int, ...] = (200, 400, 800, 1600, 3200, 6400)

# Sahada henüz kesinleşmemiş/tahmini değerler için varsayılanlar (23-09-2026
# spesifikasyonu) — Admin panelinden değiştirilebilir; buradaki değerler
# SADECE dosya hiç yoksa veya bir alan eksikse kullanılır.
DEFAULTS: dict[str, float | int] = {
    "D_drive_mm": 52.0,  # tahrik tekerleği (step+DC, özdeş) çapı — sahada kesin efektif değer teyit edilecek
    "D_wheel_dc_mm": 52.0,  # DC motorun sürtünme tekeri çapı — D_drive ile aynı beklenir, ayrı alan
    "D_rod_mm": 10.0,  # besleme çubuğu çapı — mevcut stok, ileride değişebilir
    "RPM_MAX_NOLOAD": 100.0,  # DC motor milinin %100 duty'deki tahmini boşta dönüş hızı — PLACEHOLDER, sahada takometreyle ölçülecek
    "D_wheel_rot_mm": 52.0,  # NEMA17 dönüş ekseni sürtünme tekeri çapı — DC tekerlekle aynı varsayım, sahada teyit edilecek
    "ROT_PPR": 3200,  # TB6600 darbe/tur (1/16 mikroadım) — DIP switch ile aynı olmalı, yalnızca ALLOWED_ROT_PPR değerleri
}


def _valid_param_value(key: str, value) -> float | int | None:
    """Alan için geçerli bir değerse normalize edilmiş halini, değilse None
    döner. ROT_PPR yalnızca ALLOWED_ROT_PPR değerlerini (int olarak) kabul
    eder; diğer alanlar 0'dan büyük sayı. bool sayı sayılmaz."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if key == "ROT_PPR":
        return int(value) if value in ALLOWED_ROT_PPR else None
    return float(value) if value > 0 else None


def _read_all() -> dict[str, float | int]:
    """motion_params.json'un tamamını okur. Dosya yoksa/bozuksa/bir alan
    eksikse o alan(lar) için DEFAULTS kullanılır (servis hiçbir zaman
    eksik/None bir parametreyle komut hesaplamaya çalışmasın diye)."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    for key in DEFAULTS:
        value = _valid_param_value(key, data.get(key))
        if value is not None:
            merged[key] = value
    return merged


def get_params() -> dict[str, float | int]:
    """Şu an geçerli TÜM hareket parametrelerini döner (eksik alanlar
    DEFAULTS ile tamamlanmış olarak — çağıran taraf hiçbir zaman None/eksik
    alan görmez)."""
    with _lock:
        return _read_all()


def save_params(params: dict[str, float | int]) -> dict[str, float | int]:
    """Verilen alanları kaydeder (kısmi güncelleme — DEFAULTS anahtarları
    dışındaki alanlar yok sayılır, eksik bırakılan alanlar eski değerinde
    kalır; geçersiz değer — ör. ROT_PPR izinli listede değilse — sessizce yok
    sayılır, API katmanı main.py bunu 400 ile önceden reddeder). Atomik
    yazım: önce .tmp'ye yaz, sonra rename (roi_store.py ile aynı desen)."""
    with _lock:
        current = _read_all()
        for key in DEFAULTS:
            value = _valid_param_value(key, params.get(key))
            if value is not None:
                current[key] = value
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
        return current


# Tek, paylaşılan depo — main.py bunu import edip kullanır (roi_store.py/calibration_store.py ile aynı desen).
