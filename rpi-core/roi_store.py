"""
FeedVision — ROI (region of interest) kalici depolama modulu

Ne yapar: Kamera basina (cam1/cam2) adlandirilmis ROI listesini bir JSON
dosyasinda (roi_config.json) saklar/okur. screen_reader.py'deki sabit
DEFAULT_ROI'nin yerini alir — operatorun canvas uzerinde cizdigi ROI'ler
servis restart'inda kaybolmasin diye diske yazilir.

Neden ayri dosya: main.py/vision.py/screen_reader.py'nin cekirdek ROI
kirpma-OCR-renk mantigina dokunmadan, "hangi ROI'ler okunacak" sorusunu
izole cozer.
"""

import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "roi_config.json"

# Ayni surec icinde esz amanli GET/POST cagrilari dosyayi yarida
# okumasin/yazmasin diye tek bir kilit kullaniyoruz (FastAPI ayni process
# icinde thread pool ile senkron endpoint'leri paralel calistirabilir).
_lock = threading.Lock()


def _read_all() -> dict[str, list[dict]]:
    """roi_config.json'un tamamini okur. Dosya yoksa/bozuksa bos sozluk doner
    (servis cokmesin, "henuz hic ROI cizilmemis" gibi davransin)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def get_rois(cam_id: str) -> list[dict]:
    """Kamera icin kayitli ROI listesini doner (kayitli degilse bos liste)."""
    with _lock:
        data = _read_all()
    return data.get(cam_id, [])


def save_rois(cam_id: str, rois: list[dict]) -> None:
    """Kamera icin TUM ROI listesini degistirir (replace-all, tekil ekle/sil yok)."""
    with _lock:
        data = _read_all()
        data[cam_id] = rois
        # Atomik yazim: once .tmp'ye yaz, sonra rename — yazim yarida kesilirse
        # (guc kesintisi, crash) roi_config.json hicbir zaman yari-yazilmis/bozuk
        # kalmaz, ya eski hali ya da yeni hali tam olarak durur.
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
