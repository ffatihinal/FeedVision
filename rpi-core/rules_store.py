"""
FeedVision — Kural Motoru: kural listesi kalıcı deposu

Ne yapar: operatörün tanımladığı izleme kurallarını (ör. "cam2/basinc 2-6 bar
olmali, disina cikinca motoru durdur") bir JSON dosyasinda (rules_config.json)
saklar/okur. roi_store.py / calibration_store.py ile BİREBİR AYNI desen
(atomik yazim, bozuk/eksik dosyada sessizce bos donme, tek modul-seviyesi
kilit) — tutarlilik icin kasitli olarak kopyalandi, ortak bir taban sinifa
cikarmak bu olcekte (3 kucuk dosya) gereksiz soyutlama olurdu.
"""

import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "rules_config.json"

_lock = threading.Lock()


def _read_all() -> list[dict]:
    """rules_config.json'un tamamini okur. Dosya yoksa/bozuksa bos liste
    doner (servis cokmesin, "henuz hic kural tanimlanmamis" gibi davransin)."""
    if not CONFIG_PATH.exists():
        return []
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def get_rules() -> list[dict]:
    """Tum kayitli kurallari doner (kayitli degilse bos liste)."""
    with _lock:
        return _read_all()


def save_rules(rules: list[dict]) -> None:
    """TUM kural listesini degistirir (replace-all, tekil ekle/sil yok —
    UI zaten her zaman tam listeyi gonderiyor, roi_store.py'deki ROI
    yonetimiyle ayni desen)."""
    with _lock:
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
