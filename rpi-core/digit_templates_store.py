"""
FeedVision — Rakam (digit) şablon deposu: kamera başına, etiketli (0-9,.,-)
karakter şablon görüntülerinin kalıcı depolanması.

Ne yapar: digit_reader.DigitTemplateSet'in ROI okumasında kullanacağı GERÇEK
(operatörün sahada yakaladığı) karakter şablonlarını kamera başına bir JSON
dosyasında (digit_templates_config.json) + digit_templates/ altında ayrı JPEG
dosyaları olarak saklar/okur. calibration_store.py'nin AYNI deseni (atomik
yazım, bozuk/eksik dosyada sessizce boş dönme, görüntü ikili verisi JSON'da
DEĞİL ayrı dosyada) burada da uygulanıyor (2026-09-30, Görev B).

Neden ayrı dosya (calibration_templates değil): iki depo FARKLI yaşam
döngüsüne/anlama sahip — kalibrasyon şablonları "ekranın SABİT bir UI öğesi
nerede" sorusuna (drift düzeltme) cevap verirken, burası "her karakter
(0-9,.,-) nasıl GÖRÜNÜYOR" sorusuna (rakam tanıma) cevap verir; ikisini aynı
dosyada tutmak kavramsal karışıklık + gelecekte birini değiştirirken
diğerini kırma riski yaratırdı.

Kasıtlı olarak cv2/numpy/PIL'e BAĞIMLI DEĞİL — calibration_store.py ile aynı
felsefe: bu modül sadece dosya G/Ç'si yapar, görüntü kod çözme/render işini
çağıran taraf (main.py, decode; digit_reader.py, render) üstlenir. Varsayılan/
sentetik şablon üretimi (generate_default_templates) bu yüzden BURADA değil,
digit_reader.py'de — DigitTemplateSet zaten orada tanımlı, ek bir dairesel
bağımlılık yaratmadan aynı yerde durması daha doğal.

Bu depo, calibration_store'un AKSİNE etiket (label) BAZINDA anahtarlanır
(index değil) — "0 rakamının şablonu" kavramı sıralı bir liste elemanından
çok, sabit bir sözlük anahtarı: aynı etiket için yeni bir yakalama ESKİSİNİN
ÜZERİNE YAZAR (calibration_store.add_template'in "ekle, biriktir" mantığının
AKSİNE) — bir rakamın tek, güncel bir görünümü olur, aynı etiket için birden
fazla varyant biriktirmenin (hangisi kullanılacak belirsizleşir) faydası yok.
"""

import json
import threading
import time
from pathlib import Path

from camera_ids import migrate_legacy_camera_keys

CONFIG_PATH = Path(__file__).resolve().parent / "digit_templates_config.json"

# Şablon (template) görüntülerinin saklandığı klasör — calibration_store.py'deki
# TEMPLATES_DIR ile aynı desen, ama KASITLI olarak AYRI bir klasör (yukarıdaki
# modül docstring'inde açıklanan "iki depo farklı yaşam döngüsü" gerekçesiyle).
TEMPLATES_DIR = Path(__file__).resolve().parent / "digit_templates"

_lock = threading.Lock()

# Dosya adında '.' ve '-' karakterlerini doğrudan kullanmak kafa karıştırıcı
# olur ('-' bazı araçlarda "opsiyon" sanılabilir, '.' uzantı karışıklığı
# yaratır) — okunaklı isimlere çevrilir. Sadece dosya adı için, JSON'daki
# "label" anahtarı hep orijinal karakteri (".", "-") taşır.
_SAFE_LABEL_NAMES = {".": "dot", "-": "minus"}


def _safe_label_for_filename(label: str) -> str:
    return _SAFE_LABEL_NAMES.get(label, label)


def _read_all() -> dict[str, dict]:
    """digit_templates_config.json'un tamamını okur. Dosya yoksa/bozuksa boş
    sözlük döner (servis çökmesin, "henüz hiç şablon yakalanmamış" gibi davransın)."""
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


def _atomic_write(data: dict) -> None:
    """calibration_store._atomic_write ile aynı desen — önce .tmp'ye yaz,
    sonra rename, yazım yarıda kesilirse dosya hiçbir zaman bozuk kalmaz."""
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(CONFIG_PATH)


def get_templates(cam_id: str) -> dict[str, dict]:
    """Kamera için kayıtlı {label: {"filename", "captured_at"}} sözlüğünü
    döner (hiç yakalanmamışsa {}). label anahtarları "0".."9", ".", "-"
    olabilir (bkz. digit_reader.CHARACTER_LABELS)."""
    with _lock:
        data = _read_all()
    return data.get(cam_id, {}).get("templates", {})


def add_template(cam_id: str, label: str, image_bytes: bytes) -> dict:
    """Bir karakter (label) için yeni şablon görüntüsü kaydeder — AYNI
    etiketle tekrar çağrılırsa eskisinin ÜZERİNE YAZAR (bkz. modül
    docstring'i). Görüntü (ikili veri) JSON'a değil, TEMPLATES_DIR altına
    ayrı bir JPEG dosyasına atomik olarak yazılır.

    Yeni dosya başarıyla yazılıp JSON güncellendikten SONRA eski dosya
    silinir (varsa) — yazım yarıda kesilirse eski şablon hâlâ geçerli kalsın
    diye sıralama kasıtlı: önce yeni+JSON, sonra eski dosya temizliği.
    """
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        templates = dict(entry.get("templates", {}))
        previous = templates.get(label)

        # time.time_ns() (nanosaniye) -- time.time()*1000 (milisaniye) kullanan
        # calibration_store.add_template'in AKSİNE burada daha yüksek
        # çözünürlük şart: ayni etiket icin ard arda (aynı milisaniye içinde)
        # iki kayıt yapılırsa (ör. testler, ya da operatör hızlıca düzeltme
        # yaparsa) dosya adı ÇAKIŞIRSA aşağıdaki "eski dosyayı sil" adımı
        # YENİ yazılan dosyayı silme riskiyle karşı karşıya kalır (ampirik
        # bulgu, 30-09-2026 -- testte yakalandı).
        filename = f"{cam_id}_{_safe_label_for_filename(label)}_{time.time_ns()}.jpg"
        tmp_img_path = TEMPLATES_DIR / (filename + ".tmp")
        img_path = TEMPLATES_DIR / filename
        with open(tmp_img_path, "wb") as f:
            f.write(image_bytes)
        tmp_img_path.replace(img_path)

        template_entry = {"filename": filename, "captured_at": time.time()}
        templates[label] = template_entry
        entry["templates"] = templates
        data[cam_id] = entry
        _atomic_write(data)

        if previous and previous.get("filename") and previous["filename"] != filename:
            (TEMPLATES_DIR / previous["filename"]).unlink(missing_ok=True)
    return template_entry


def remove_template(cam_id: str, label: str) -> dict[str, dict]:
    """Verilen etiketin şablonunu (metadata + diskteki JPEG dosyası) siler.

    Kayıtlı olmayan bir etiket sessizce yok sayılır (liste değişmeden döner)
    — calibration_store.remove_template'teki "geçersiz index'i sessizce
    yok say" davranışıyla aynı gerekçe (iki sekme aynı anda açıkken yarış
    durumuna karşı en güvenli davranış)."""
    with _lock:
        data = _read_all()
        entry = data.get(cam_id, {})
        templates = dict(entry.get("templates", {}))
        removed = templates.pop(label, None)
        if removed is not None:
            (TEMPLATES_DIR / removed["filename"]).unlink(missing_ok=True)
            entry["templates"] = templates
            data[cam_id] = entry
            _atomic_write(data)
        result = templates
    return result


def read_template_image_bytes(cam_id: str, filename: str) -> bytes | None:
    """Diskteki şablon JPEG'ini ham bayt olarak okur (main.py bunu
    cv2.imdecode ile görüntüye çevirir — bu modül kasıtlı olarak cv2'ye
    bağımlı değil, bkz. modül docstring'i). Dosya yoksa/okunamıyorsa None
    (çağıran taraf bu şablonu sessizce atlar)."""
    del cam_id
    path = TEMPLATES_DIR / filename
    try:
        return path.read_bytes()
    except OSError:
        return None


# Tek, paylaşılan depo — main.py bunu import edip kullanır (calibration_store.py/
# roi_store.py ile aynı desen).
