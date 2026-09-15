"""
FeedVision — Kontrol Kriterleri: kriter listesi kalıcı deposu

Ne yapar: operatörün tanımladığı izleme kriterlerini (ör. "ui_screen/basinc
2-6 bar olmali, disina cikinca motoru durdur") bir JSON dosyasinda
(rules_config.json) saklar/okur. roi_store.py / calibration_store.py ile
BİREBİR AYNI desen (atomik yazim, bozuk/eksik dosyada sessizce bos donme,
tek modul-seviyesi kilit) — tutarlilik icin kasitli olarak kopyalandi,
ortak bir taban sinifa cikarmak bu olcekte (3 kucuk dosya) gereksiz
soyutlama olurdu.

(Dosya/degisken adlari "rules"/RuleDef vb. kod tarafinda İngilizce kaldi —
proje kurali; sadece kullaniciya gorunen metin/yorumlarda "Kontrol
Kriterleri" terimi kullaniliyor, 15-09-2026 Fatih karari.)
"""

import json
import threading
from pathlib import Path

from camera_ids import migrate_legacy_camera_id

CONFIG_PATH = Path(__file__).resolve().parent / "rules_config.json"

_lock = threading.Lock()

# Eski kaynak turu (source="stm32", serbest "field" adiyla) 15-09-2026'da
# kapsam disi birakildi — artik SADECE "stm" (baglanti timeout'u, "field"
# yerine "timeout_s"). Eski kayitlarla karsilasirsak veri kaybetmeden/
# hata vermeden makul bir varsayimla (10sn timeout) tasiyoruz.
_LEGACY_STM_DEFAULT_TIMEOUT_S = 10.0


def _read_all() -> list[dict]:
    """rules_config.json'un tamamini okur. Dosya yoksa/bozuksa bos liste
    doner (servis cokmesin, "henuz hic kriter tanimlanmamis" gibi davransin)."""
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
    """Tum kayitli kriterleri doner (kayitli degilse bos liste).

    İki bellek-içi (dosyayı yeniden yazmayan) geçiş uygulanır:
    - (source="roi" ise) cam_id alani, eski "cam1"/"cam2" ise yeni isimlere
      (chamber/ui_screen) tasinir (bkz. roi_store.py'deki ayni desen).
    - (source="stm32" ise, ESKI kapsam) "stm" olarak yeniden adlandirilir,
      serbest "field" alani atilir, "timeout_s" yoksa varsayilan verilir.
    """
    with _lock:
        rules = _read_all()
    for rule in rules:
        if rule.get("source") == "roi" and "cam_id" in rule:
            rule["cam_id"] = migrate_legacy_camera_id(rule["cam_id"])
        elif rule.get("source") == "stm32":
            rule["source"] = "stm"
            rule.pop("field", None)
            rule.setdefault("timeout_s", _LEGACY_STM_DEFAULT_TIMEOUT_S)
    return rules


def save_rules(rules: list[dict]) -> None:
    """TUM kriter listesini degistirir (replace-all, tekil ekle/sil yok —
    UI zaten her zaman tam listeyi gonderiyor, roi_store.py'deki ROI
    yonetimiyle ayni desen)."""
    with _lock:
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        tmp_path.replace(CONFIG_PATH)
