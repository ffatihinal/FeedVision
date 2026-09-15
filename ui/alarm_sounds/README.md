# Alarm Sesleri (MP3)

Bu klasöre bırakılan `.mp3` dosyaları, Admin panelindeki Kontrol Kriterleri
tablosunda "Alarm Sesi" seçeneği olarak otomatik listelenir — kod değişikliği
gerekmez, sunucu her istek geldiğinde klasörü yeniden tarar.

Bir kritere ses seçilmezse mevcut Web Audio ton sistemi (kritik: 880Hz x3,
uyarı: 440Hz x1) devreye girer (varsayılan/fallback davranış).

Dosya adı kısıtları: sadece `.mp3` uzantılı, klasörün doğrudan içinde
(alt klasör desteklenmiyor), dosya adında `/` ya da `\` olamaz.
