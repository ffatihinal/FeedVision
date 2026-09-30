"""
FeedVision — ROI drift düzeltme: referans köşe/şablon + açık-kapalı kalıcı deposu

Ne yapar: Kamera başına (chamber/ui_screen), kalibrasyon anında tespit edilen 4
ekran köşesini (bezel yöntemi) VE/VEYA sabit UI şablon(lar)ını (template
yöntemi, bkz. screen_calibration.find_template_anchor) bir JSON dosyasında
(calibration_config.json) saklar/okur. Ayrıca kalibrasyonun (drift düzeltme)
açık/kapalı olup olmadığını (calibration_enabled) tutar (2026-09-30, Görev B).
roi_store.py'nin aynı deseni (atomik yazım, bozuk/eksik dosyada sessizce
boş dönme) burada da uygulanıyor — iki depo kasıtlı olarak AYRI dosyalar:
ROI listesi ile referans köşeler farklı yaşam döngülerine sahip (köşeler
sadece kalibrasyon anında değişir, ROI'ler operatör her çizdiğinde değişir).

Şablon GÖRÜNTÜLERİ (ikili veri) JSON'da tutulmaz — calibration_templates/
altına ayrı JPEG dosyaları olarak yazılır, JSON'da sadece dosya adı + anchor
(kayıt anındaki konum) + boyut tutulur (bkz. add_template).
"""

import json
import threading
import time
from pathlib import Path

from camera_ids import migrate_legacy_camera_keys

CONFIG_PATH = Path(__file__).resolve().parent / "calibration_config.json"

# Şablon (template) görüntülerinin saklandığı klasör — calibration_config.json
# ile aynı dizinde, roi_store.py'deki "scans/" ile aynı desen (main.py SCANS_DIR).
TEMPLATES_DIR = Path(__file__).resolve().parent / "calibration_templates"

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
    # Eski "cam1"/"cam2" anahtarli bir dosya olabilir — bkz. roi_store.py
    # ayni desendeki aciklama (bellek ici tasima, dosya burada yeniden yazilmaz).
    return migrate_legacy_camera_keys(data)


def get_reference(cam_id: str) -> dict | None:
    """Kamera icin kayitli referans kosheleri + kayit zamanini doner.

    Donen sozluk: {"corners": [[x,y],[x,y],[x,y],[x,y]], "calibrated_at": unix_ts}
    Hic kalibrasyon yapilmamissa None doner.
    """
    with _lock:
        data = _read_all()
    return data.get(cam_id)


def _atomic_write(data: dict) -> None:
    """Ortak atomik yazım adımı — once .tmp'ye yaz, sonra rename (roi_store.py
    ile ayni desen). Tum "set_*"/"save_*"/"add_*"/"remove_*" fonksiyonlari
    bunu paylasir ki yazim yarida kesilirse dosya hicbir zaman bozuk kalmaz."""
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(CONFIG_PATH)


def save_reference(cam_id: str, corners: list[list[float]]) -> dict:
    """Kamera icin yeni referans kosheleri kaydeder (eskisinin uzerine yazar).

    Kasitli olarak corners/calibrated_at icin "replace" — birden fazla bezel
    referansi biriktirmiyoruz, her kalibrasyon bir oncekini gecersiz kilar
    (kamera pozisyonu degisti demektir). AMA kaydin templates/enabled
    alanlarina DOKUNULMAZ (2026-09-30 duzeltmesi — eskiden bu fonksiyon
    tum girdiyi degistiriyordu, bu da onceden secilmis referans sablonlarini
    ya da kalibrasyon acik/kapali tercihini sessizce silerdi).
    """
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        entry["corners"] = corners
        entry["calibrated_at"] = time.time()
        data[cam_id] = entry
        _atomic_write(data)
    return {"corners": entry["corners"], "calibrated_at": entry["calibrated_at"]}


def get_enabled(cam_id: str) -> bool:
    """Kalibrasyon (drift düzeltme) açık mı? Hiç ayarlanmamışsa varsayılan
    True — Görev B öncesi tek davranış zaten "referans varsa uygula" idi,
    bu yüzden yeni toggle'ın varsayılanı o eski davranışla aynı olmalı
    (sessiz bir regresyon olmasın: mevcut kurulumlarda toggle hiç
    dokunulmamışken kalibrasyon birden "kapalı" görünmemeli)."""
    with _lock:
        data = _read_all()
    return data.get(cam_id, {}).get("enabled", True)


def set_enabled(cam_id: str, enabled: bool) -> dict:
    """Açık/kapalı tercihini kaydeder — referans köşelere/şablonlara DOKUNMAZ."""
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        entry["enabled"] = enabled
        data[cam_id] = entry
        _atomic_write(data)
    return {"enabled": enabled}


def get_templates(cam_id: str) -> list[dict]:
    """Kamera icin kayitli referans sablon listesini doner (hic yoksa []).

    Her eleman: {"filename": "...", "anchor": [x, y], "size": [w, h],
    "captured_at": unix_ts} — "filename" TEMPLATES_DIR icindeki JPEG dosyasi,
    "anchor" o sablonun kayit anindaki (referans) sol-ust piksel konumu.
    """
    with _lock:
        data = _read_all()
    return data.get(cam_id, {}).get("templates", [])


def add_template(cam_id: str, anchor: list[float], size: list[int], image_bytes: bytes) -> dict:
    """Yeni bir referans şablonu EKLER (var olanlara, replace degil) —
    Görev A: en az 1, tercihen 2+ (birbirinden uzak) şablon kaydedilebilsin
    diye compute_warp_matrix_from_anchors 2+ noktayla dönme+ölçek de
    tahmin edebiliyor (tek noktayla sadece öteleme).

    Görüntü (ikili veri) JSON'a değil, TEMPLATES_DIR altına ayrı bir JPEG
    dosyasına atomik olarak yazılır (once .tmp'ye, sonra rename) — JSON
    sadece dosya adi + anchor + boyut + zaman damgasi tutar.
    """
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        templates = list(entry.get("templates", []))
        filename = f"{cam_id}_{len(templates)}_{int(time.time() * 1000)}.jpg"
        tmp_img_path = TEMPLATES_DIR / (filename + ".tmp")
        img_path = TEMPLATES_DIR / filename
        with open(tmp_img_path, "wb") as f:
            f.write(image_bytes)
        tmp_img_path.replace(img_path)

        template_entry = {
            "filename": filename,
            "anchor": [float(anchor[0]), float(anchor[1])],
            "size": [int(size[0]), int(size[1])],
            "captured_at": time.time(),
        }
        templates.append(template_entry)
        entry["templates"] = templates
        data[cam_id] = entry
        _atomic_write(data)
    return template_entry


def remove_template(cam_id: str, index: int) -> list[dict]:
    """Verilen index'teki sablonu (metadata + diskteki JPEG dosyasi) siler.

    Gecersiz index sessizce yok sayilir (liste degismeden doner) — cagiran
    taraf (Admin UI) zaten kendi gorup sildigi bir index'i gonderiyor, ama
    iki sekme ayni anda acikken index kaymasi gibi bir yarisa karsi
    "sessizce hicbir sey yapma" en guvenli davranis (yanlislikla baska bir
    sablonu silmekten iyi)."""
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        templates = list(entry.get("templates", []))
        if 0 <= index < len(templates):
            removed = templates.pop(index)
            (TEMPLATES_DIR / removed["filename"]).unlink(missing_ok=True)
            entry["templates"] = templates
            data[cam_id] = entry
            _atomic_write(data)
        result = templates
    return result


def read_template_image_bytes(cam_id: str, filename: str) -> bytes | None:
    """Diskteki sablon JPEG'ini ham bayt olarak okur (main.py bunu
    cv2.imdecode ile goruntuye cevirir — calibration_store kasitli olarak
    cv2'ye bagimli degil, sadece dosya G/C'si yapar, ayni roi_store.py/
    vision_settings_store.py felsefesi). Dosya yoksa/okunamiyorsa None
    (cagiran taraf bu sablonu sessizce atlar, bkz. main.py).

    cam_id parametresi su an dosya yolunu etkilemiyor (dosya adi zaten
    cam_id ile basliyor, bkz. add_template) — imza, ileride kamera basina
    alt-klasorlenirse cagiran taraflari degistirmeden genisleyebilsin diye
    simdiden bu sekilde tutuldu."""
    del cam_id
    path = TEMPLATES_DIR / filename
    try:
        return path.read_bytes()
    except OSError:
        return None


# Tek, paylasilan depo - main.py bunu import edip kullanir (roi_store.py ile ayni desen).
