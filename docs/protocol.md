# Pi ↔ STM32 Protokolü

USB kablo üzerinden, satır bazlı JSON (`\n` ile biten tek satır komut/durum). Tüm alan adları ve değerler İngilizce (kod tabanı genelinde geçerli kural — yorumlar Türkçe, tanımlayıcılar İngilizce).

Kaynak karar: `yazilim_mimarisi.md` Bölüm 3 (Azobex WP1 vault) + `firmware/CUBEMX_KURULUM.md` Bölüm 12.

## PC/Pi → STM32 (komut)

| Komut | Anlamı |
|---|---|
| `{"cmd":"step","dir":1,"delay":500,"steps":2000}` | 2000 step at, darbe periyodu 500 µs, yön 1 |
| `{"cmd":"step","dir":1,"delay":500,"steps":2000,"accel":300}` | Aynısı ama ilk 300 ve son 300 adımda hızlanıp yavaşlıyor (rampa) — bkz. aşağıda |
| `{"cmd":"stop"}` | Step motoru anında durdur |
| `{"cmd":"dc","dir":"forward"}` | DC motor ileri |
| `{"cmd":"dc","dir":"backward"}` | DC motor geri |
| `{"cmd":"dc","dir":"stop"}` | DC motor dur |
| `{"cmd":"reset"}` | İki encoder sayacını da sıfırla |
| `{"cmd":"ping"}` | Bağlantı testi |

## STM32 → Pi/PC (durum, saniyede ~20 kez)

```json
{"t":12345,"e1":1834,"e2":1801,"um1":96031,"um2":94303,"remaining":0,"running":0,"dc":0}
```

| Alan | Anlamı |
|---|---|
| `t` | Kart açıldığından beri geçen ms |
| `e1` / `e2` | Encoder 1 / 2 toplam sayım (işaretli, geri dönünce azalır) |
| `um1` / `um2` | Aynı sayımın mikrometre karşılığı (1000'e bölünce mm) |
| `remaining` | Step motorun atmayı bekleyen darbe sayısı |
| `running` | 1 = step motor hareket halinde |
| `dc` | 0 = dur, 1 = ileri, 2 = geri |

Gerçek üretim protokolü (Pi tarafı `feedvision-core`) bu test protokolünü temel alacak, komut seti büyüyecek (SE ekibinin ICD'siyle uyumlu hale gelecek).

## `delay` ve `accel` — hız ve rampa (03-09-2026 netleştirildi)

**`delay` step'in BÜYÜKLÜĞÜNÜ değil, HIZINI belirler.** Bir adımın açısı (mikroadım DIP switch'leri kapalıyken 1.8°, 200 adım/tur) sabittir — motorun fiziksel yapısı + DIP mikroadım ayarı belirler, `delay` hiç etkilemez. `delay` sadece iki darbe arasındaki süreyi (µs) değiştirir, yani ne kadar SIK adım atıldığını.

`delay` çok küçük (çok hızlı) verilirse step **küçülmez** — motor senkronu kaybedip o darbeyi hiç uygulayamaz ("step kaybı"). Firmware bunu bilmez (open-loop, geri bildirim yok), `remaining` yine düzgün azalır ama mil beklenenden az döner. Bu yüzden mikroadımsız (200 adım/tur) durumda `steps=200` verip milin tam 1 tur döndüğünü elle/gözle doğrulamak en güvenilir test yöntemidir.

**Sınırlar (firmware'de sabit, `main.c`):**
- `STEP_MIN_DELAY_US = 20` (tepe hız 50kHz — DM556'nın 200kHz limitine göre 4× güvenlik payı)
- `STEP_MAX_DELAY_US = 60000` (timer'ın 16-bit sayıcı tavanı)
- Bu firmware/sürücü limitleri — motorun GERÇEKTEN takip edebildiği hız (yük + akım ayarına göre değişir) genelde çok daha düşüktür, sahada ampirik test edilmeli.

**RPM'e çevirmek için mikroadım çarpanı gerekir:**

```
RPM = 60.000.000 / (delay_us × 200 × mikroadım_çarpanı)
```

Mikroadım kapalıyken (çarpan=1): `delay_us=500` → 600 RPM'lik darbe hızı demek — bu YÜKSEK, sıfırdan böyle başlatmak stall'a sebep olur, bu yüzden rampa var (aşağıda).

**`accel` — hızlanma/yavaşlama rampası (03-09-2026 eklendi):**

`accel` = kaç darbede hedef hıza (`delay`) çıkılacağı ve hareketin son o kadar darbesinde tekrar aynı şekilde yavaşlanacağı. `0` (veya alan hiç yoksa) = rampasız, eski davranış — sabit `delay` ile baştan başlar.

Rampa mekaniği: `STEP_RAMP_START_DELAY_US` (main.c, varsayılan 2000µs = ~güvenli/yavaş başlangıç) ile başlar, `accel` darbede doğrusal olarak komut edilen `delay`'e iner, ortada sabit hızda devam eder, son `accel` darbede tekrar `STEP_RAMP_START_DELAY_US`'a çıkar. `accel > steps/2` verilirse otomatik `steps/2`'ye küçültülür (hızlanma+yavaşlama çakışmasın diye).

`STEP_RAMP_START_DELAY_US` sahada motor/yük/akım ayarına göre ince ayar gerektirebilir (main.c'de tek satır sabit).
