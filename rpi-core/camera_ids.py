"""
FeedVision — kamera kimlikleri (tek gerçek kaynak) + eski isimlerden
(cam1/cam2) yeni isimlere (chamber/ui_screen) geçiş uyumluluk katmanı.

Neden yeniden adlandırıldı (15-09-2026): "AP1/AP2" WP1 sözleşmesindeki İŞ
PAKETİ kodları (Chamber izleme = AP1, UA ekran okuma = AP2) — kamera ADI
DEĞİL. Kod/UI'da AP1/AP2 ya da "1. kamera/2. kamera" kullanmak yanlış
anlaşılmaya açıyordu (iş paketiyle kamera karıştırılıyordu). Kameralar
artık İŞLEVLERİYLE adlandırılıyor: CHAMBER (chamber içini izleyen kamera)
ve UI_SCREEN (UA cihazının ekranını okuyan kamera). Sözleşme/teklif
dokümanlarındaki AP1-AP4 iş paketi kodlarına DOKUNULMADI, onlar ayrı bir
kavram ve sabit kalıyor — bu sadece kod/UI'daki kamera etiketleme sorunu.
"""

CHAMBER = "chamber"
UI_SCREEN = "ui_screen"

# cam_id (HTTP'ye/UI'a dönük isim) -> picamera2 camera_num (donanım CSI indeksi)
CAMERA_NUMS = {CHAMBER: 0, UI_SCREEN: 1}

# Eski isimlerden geçiş: roi_config.json/calibration_config.json/
# rules_config.json gibi bir Pi'de zaten oluşmuş olabilecek dosyalarda
# eski "cam1"/"cam2" anahtarları varsa veri kaybetmeden yeni isimlere
# taşımak için (bkz. roi_store.py/calibration_store.py/rules_store.py).
LEGACY_ALIASES = {"cam1": CHAMBER, "cam2": UI_SCREEN}


def migrate_legacy_camera_keys(data: dict) -> dict:
    """Bir {cam_id: ...} sözlüğünde eski cam1/cam2 anahtarları varsa yeni
    isimlere taşır. SADECE bellek içinde (in-memory) yapılır, dosyayı
    kendiliğinden YENİDEN YAZMAZ — bir "get" fonksiyonunun içinde yan
    etkisiz çağrılabilsin diye (yazma, ilgili store'un normal save
    çağrısıyla doğal olarak gerçekleşir).

    Yeni isim zaten varsa eski anahtar ATILMAZ (olduğu gibi bırakılır) —
    iki farklı veri seti çakışıyorsa sessizce birini kaybetmek yerine
    ikisini de görünür tutmak daha güvenli (operatör/Fatih fark edip
    elle çözer).
    """
    migrated = dict(data)
    for old_key, new_key in LEGACY_ALIASES.items():
        if old_key in migrated and new_key not in migrated:
            migrated[new_key] = migrated.pop(old_key)
    return migrated


def migrate_legacy_camera_id(cam_id: str | None) -> str | None:
    """Tekil bir cam_id değerini (ör. bir kuralın içindeki cam_id alanı)
    eski isimden yeniye çevirir. Zaten yeni/bilinmeyen bir isimse olduğu
    gibi döner."""
    if cam_id in LEGACY_ALIASES:
        return LEGACY_ALIASES[cam_id]
    return cam_id
