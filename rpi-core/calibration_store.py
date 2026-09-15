"""
FeedVision — ROI drift düzeltme: referans köşe kalıcı deposu

Ne yapar: Kamera başına (cam1/cam2), kalibrasyon anında tespit edilen 4
ekran köşesini bir JSON dosyasında (calibration_config.json) saklar/okur.
roi_store.py'nin aynı deseni (atomik yazım, bozuk/eksik dosyada sessizce
boş dönme) burada da uygulanıyor — iki depo kasıtlı olarak AYRI dosyalar:
ROI listesi ile referans köşeler farklı yaşam döngülerine sahip (köşeler
sadece kalibrasyon anında değişir, ROI'ler operatör her çizdiğinde değişir).
"""

import json
import threading
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "calibration_config.json"

_lock = threading.Lock()


def _read_all() -> dict[str, dict]:
    """calibration_config.json'un tamamini okur. Dosya yoksa/bozuksa bos
    sozluk doner (servis cokmesin, "henuz hic kalibrasyon yapilmamis" gibi davransin)."""
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


def get_reference(cam_id: str) -> dict | None:
    """Kamera icin kayitli referans kosheleri + kayit zamanini doner.

    Donen sozluk: {"corners": [[x,y],[x,y],[x,y],[x,y]], "calibrated_at": unix_ts}
    Hic kalibrasyon yapilmamissa None doner.
    """
    with _lock:
        data = _read_all()
    return data.get(cam_id)


def save_reference(cam_id: str, corners: list[list[float]]) -> dict:
    """Kamera icin yeni referans kosheleri kaydeder (eskisinin uzerine yazar).

    Kasitli olarak "replace" — birden fazla referans biriktirmiyoruz, her
    kalibrasyon bir oncekini gecersiz kilar (kamera pozisyonu degisti demektir).
    """
    entry = {"corners": corners, "calibrated_at": time.time()}
    with _lock:
        data = _read_all()
        data[cam_id] = entry
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
    return entry


# Tek, paylasilan depo - main.py bunu import edip kullanir (roi_store.py ile ayni desen).
