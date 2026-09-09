# FeedVision — Bakım ve Güncelleme Kılavuzu

Bu dosya kullanıcı/bakım manuelinin ilk parçasıdır — ileride kapsam büyürse (kurulum, sorun giderme, donanım bakımı vb.) ayrı bölümler/dosyalar olarak eklenebilir.

## Kod Güncellemesi Nasıl Yapılır

### Neden tek başına `git pull` yetmiyor

`git pull` sadece diskteki dosyaları günceller. Ama Raspberry Pi'de arka planda zaten çalışan bir sunucu süreci var — bu süreç, başladığı andaki kodu hafızasında tutar. `git pull` yeni kodu diske yazsa da, restart edilmeden o çalışan süreç eski koduyla çalışmaya devam eder. Yani yeni bir backend özelliği (örnek: `/system/temp` endpoint'i) koda eklenmiş olsa bile, servis yeniden başlatılana kadar devrede olmaz.

**İstisna:** Arayüz (HTML/frontend) dosyaları her istekte diskten taze okunur, bu yüzden onlar restart gerektirmeden güncellenir. Bu asimetri kafa karıştırıcı olabilir: "arayüzdeki değişiklik hemen göründü ama yeni buton çalışmıyor" gibi bir durumda akla ilk gelmesi gereken şey, backend'in henüz restart edilmediğidir.

### Doğru prosedür — tek komut

```bash
bash rpi-core/scripts/update_and_restart.sh
```

Bu script sırasıyla:
1. `git pull` — kodu günceller
2. Gerekiyorsa yeni Python bağımlılıklarını kurar (`pip install -r requirements.txt`)
3. Servisi yeniden başlatır (`systemctl restart feedvision`)
4. Sağlık kontrolü yapar — servisin gerçekten ayağa kalktığını doğrular ve son logları gösterir

### Elle yapmak isteyen / script çalışmazsa

```bash
git pull
sudo systemctl restart feedvision
sudo systemctl status feedvision
```

Son satır (`status`) servisin "active (running)" olduğunu göstermeli.

### Sorun giderme

Yeni bir endpoint veya özellik "çalışmıyor" görünüyorsa ilk kontrol edilecek şey, servisin gerçekten yeniden başlatılıp başlatılmadığıdır:

```bash
sudo systemctl status feedvision
```

Bu komutun çıktısındaki "Active: active (running) since ..." satırı servisin ne zamandır ayakta olduğunu gösterir — eğer bu tarih son `git pull`'dan önceyse, servis hâlâ eski kodla çalışıyor demektir; restart gerekir.

Bu bilgiyi (ne zaman başladı) artık web app'in en altında da görebilirsin, ayrıca terminale gitmene gerek kalmadan.
