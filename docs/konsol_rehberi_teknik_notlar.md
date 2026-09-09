# Konsol Rehberi — Teknik Notlar (geliştirici referansı)

> Bu dosya, web app'teki "ℹ️ Konsol Rehberi" pop-up'ının **son kullanıcıya sadeleştirilmeden önceki** hâlidir — geliştirici/bakım referansı olarak burada tutulur. Kullanıcıya gösterilen pop-up daha sade bir dille yazılmıştır (`ui/index.html`).

## Anoloji

Konsol = "ben backend'e şunu sordum, backend gerçekte şunu söyledi" kaydı.

Bu web uygulamasının iki tarafı var: **Frontend** (tarayıcı sayfası) ve **Backend** (Pi veya Mac'te çalışan gerçek sunucu — motor, kamera ve seri portla fiilen konuşan taraf). Konsol'daki her satır, bir işlemin backend'den gelen **GERÇEK** cevabıdır — uydurma veya tahmini metin yazılmaz (kamera panelinde geçmişte böyle bir sorun vardı, 2026-09-09'da düzeltildi).

## Kategoriler

| Kategori | Kaynağı | Ne zaman görünür | Gerçekten neyi gösterir | İlişkili başka bir yer |
|---|---|---|---|---|
| 🎥 Kamera (AP1/AP2) | `vision.py`, gerçek Picamera2 donanım denemesi | Kamera butonuna basınca | `/vision/{id}/snapshot` isteğinin gerçek HTTP cevabı — başarılıysa content-type/uzunluk, başarısızsa backend'in gerçek hata sebebi (`vision.errors`) | — |
| 📜 Sistem Logları | `journalctl -u feedvision` (systemd) | "Sistem Logları" pop-up'ı açılınca/yenilenince | Backend SÜRECİNİN kendi sağlığı (başlama/durma/hata) — motorun ne yaptığıyla ilgili değil | Journal'ın ham içeriği Konsol'da değil, ayrı "Sistem Logları" pop-up'ında görünür; Konsol'a sadece "Loglar yüklendi." / "Başarısız: ..." özet satırı düşer |
| 🔌 Seri Port (Yenile / Bağlan / Bağlantıyı Kes) | Backend'in işletim sisteminden yaptığı gerçek USB/seri port taraması (pyserial) | İlgili butona basınca | O an sistemde gerçekten bulunan portlar ve bağlantı işleminin gerçek sonucu | — |
| ⚙️ Motor Komutları (Step At, Dur, DC, Encoder Sıfırla) | Backend, komutu seri port üzerinden STM32'ye gönderir | İlgili motor butonuna basınca | STM32'nin gerçek JSON cevabı — backend bunu okuyup Konsol'a yazar | "Giden Komut / Gelen Yanıt" panelinde aynı alışveriş daha detaylı gösterilir; Konsol'daki sadece o panelin kısa günlüğüdür |
| 📶 Canlı Durum (WebSocket) | STM32 ↔ backend bağlantı durumu | Bağlantı kurulduğunda/koptuğunda — hiçbir butona basmadan, kendiliğinden | Bağlantının o anki gerçek durumu; spam olmasın diye sadece durum değiştiğinde bir kez yazılır | — |

## Diğer davranışlar

- Konsol en fazla 500 satır tutar, eskiler otomatik düşer.
- "Konsolu Temizle" butonuyla elle de boşaltılabilir.

---
*Kaynak: 2026-09-09 tarihli picamera2/kamera-Konsol düzeltmesi oturumu. Web app'teki pop-up içeriği son kullanıcı için ayrıca sadeleştirilmiştir — güncel kullanıcı metni için `ui/index.html`'e bakılmalı.*
