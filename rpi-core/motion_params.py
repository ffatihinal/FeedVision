"""
FeedVision — Hareket parametreleri kalıcı deposu (motion_params.json)

Ne yapar: step/DC motor fiziksel birim dönüşümlerinde (mm/s, mm, RPM <-> ham
firmware komutları — bkz. motion_calc.py) kullanılan donanım ölçülerini
saklar/okur: tahrik tekerleği çapı (step+DC ortak, D_drive_mm), DC motorun
sürtünme tekerleği çapı (D_wheel_dc_mm — D_drive ile aynı değer olması
beklenir ama ayrı alan, "özdeş" olsa da garanti olsun diye), besleme çubuğu
çapı (D_rod_mm), DC motorun %100 duty'deki tahmini boşta dönüş hızı
(RPM_MAX_NOLOAD — sahada ölçülene kadar placeholder).

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

# Sahada henüz kesinleşmemiş/tahmini değerler için varsayılanlar (23-09-2026
# spesifikasyonu) — Admin panelinden değiştirilebilir; buradaki değerler
# SADECE dosya hiç yoksa veya bir alan eksikse kullanılır.
DEFAULTS: dict[str, float] = {
    "D_drive_mm": 52.0,  # tahrik tekerleği (step+DC, özdeş) çapı — sahada kesin efektif değer teyit edilecek
    "D_wheel_dc_mm": 52.0,  # DC motorun sürtünme tekeri çapı — D_drive ile aynı beklenir, ayrı alan
    "D_rod_mm": 10.0,  # besleme çubuğu çapı — mevcut stok, ileride değişebilir
    "RPM_MAX_NOLOAD": 100.0,  # DC motor milinin %100 duty'deki tahmini boşta dönüş hızı — PLACEHOLDER, sahada takometreyle ölçülecek
}


def _read_all() -> dict[str, float]:
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
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            merged[key] = float(value)
    return merged


def get_params() -> dict[str, float]:
    """Şu an geçerli TÜM hareket parametrelerini döner (eksik alanlar
    DEFAULTS ile tamamlanmış olarak — çağıran taraf hiçbir zaman None/eksik
    alan görmez)."""
    with _lock:
        return _read_all()


def save_params(params: dict[str, float]) -> dict[str, float]:
    """Verilen alanları kaydeder (kısmi güncelleme — DEFAULTS anahtarları
    dışındaki alanlar yok sayılır, eksik bırakılan alanlar eski değerinde
    kalır). Atomik yazım: önce .tmp'ye yaz, sonra rename (roi_store.py ile
    aynı desen)."""
    with _lock:
        current = _read_all()
        for key in DEFAULTS:
            value = params.get(key)
            if isinstance(value, (int, float)) and value > 0:
                current[key] = float(value)
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
        return current


# Tek, paylaşılan depo — main.py bunu import edip kullanır (roi_store.py/calibration_store.py ile aynı desen).
