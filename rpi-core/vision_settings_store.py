"""
FeedVision — kamera pozlama (exposure) + on-isleme (preprocess) ayarlarinin
kalici deposu

Ne yapar: Kamera basina (chamber/ui_screen), manuel pozlama/kazanc
ayarlarini ve CLAHE on-isleme toggle'ini bir JSON dosyasinda
(vision_settings.json) saklar/okur — roi_store.py/calibration_store.py ile
AYNI desen (atomik yazim, bozuk/eksik dosyada sessizce varsayilana donme).

Neden ayri dosya: Bu ayarlar ROI listesinden/kalibrasyon referansindan
farkli bir yasam dongusune sahip (kamera baslatildiginda bir kere okunup
donanima uygulanir) — ayri store, mevcut store'lara dokunmadan izole eklenebilsin diye.
"""

import json
import threading
from pathlib import Path

from camera_ids import migrate_legacy_camera_keys

CONFIG_PATH = Path(__file__).resolve().parent / "vision_settings.json"

_lock = threading.Lock()

# Kamera acilamamis/hic ayar yapilmamis durumda kullanilan varsayilan:
# otomatik pozlama acik, CLAHE kapali (mevcut davranis bozulmasin).
DEFAULT_SETTINGS: dict = {
    "exposure": {"auto": True, "exposure_time": None, "gain": None},
    "preprocess": {"clahe_enabled": False},
}


def _read_all() -> dict[str, dict]:
    """vision_settings.json'un tamamini okur. Dosya yoksa/bozuksa bos sozluk
    doner (servis cokmesin, "hic ayar yapilmamis" gibi davransin)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return migrate_legacy_camera_keys(data)


def get_settings(cam_id: str) -> dict:
    """Kamera icin kayitli ayarlari doner — hic kayit yoksa/kismi kayitsa
    DEFAULT_SETTINGS ile birlestirilmis (eksik alanlar varsayilanla
    doldurulmus) tam bir sozluk doner, cagiran taraf None kontrolu yapmak
    zorunda kalmasin diye."""
    with _lock:
        data = _read_all()
    stored = data.get(cam_id, {})
    return {
        "exposure": {**DEFAULT_SETTINGS["exposure"], **stored.get("exposure", {})},
        "preprocess": {**DEFAULT_SETTINGS["preprocess"], **stored.get("preprocess", {})},
    }


def _save(cam_id: str, section: str, value: dict) -> dict:
    """Ortak yazim yolu: mevcut kaydin SADECE ilgili bolumunu (exposure ya da
    preprocess) degistirir, digerine dokunmaz; atomik yazim (roi_store.py ile
    ayni desen)."""
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        entry[section] = value
        data[cam_id] = entry
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
    return value


def save_exposure(
    cam_id: str, auto: bool, exposure_time: int | None = None, gain: float | None = None
) -> dict:
    """Pozlama ayarini kaydeder. auto=True ise exposure_time/gain None'a
    zorlanir (tutarsiz "hem otomatik hem manuel deger" durumu diskte
    tutulmasin diye)."""
    value = {
        "auto": auto,
        "exposure_time": None if auto else exposure_time,
        "gain": None if auto else gain,
    }
    return _save(cam_id, "exposure", value)


def save_preprocess(cam_id: str, clahe_enabled: bool) -> dict:
    """On-isleme (CLAHE) toggle'ini kaydeder."""
    return _save(cam_id, "preprocess", {"clahe_enabled": clahe_enabled})
