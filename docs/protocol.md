# Pi ↔ STM32 Protokolü

USB kablo üzerinden, satır bazlı JSON (`\n` ile biten tek satır komut/durum). Tüm alan adları ve değerler İngilizce (kod tabanı genelinde geçerli kural — yorumlar Türkçe, tanımlayıcılar İngilizce).

Kaynak karar: `yazilim_mimarisi.md` Bölüm 3 (Azobex WP1 vault) + `firmware/CUBEMX_KURULUM.md` Bölüm 12.

## PC/Pi → STM32 (komut)

| Komut | Anlamı |
|---|---|
| `{"cmd":"step","dir":1,"delay":500,"steps":2000}` | 2000 step at, darbe periyodu 500 µs, yön 1 |
| `{"cmd":"step","dir":1,"delay":500,"steps":2000,"accel":300}` | Aynısı ama ilk 300 ve son 300 adımda hızlanıp yavaşlıyor (rampa) — bkz. aşağıda |
| `{"cmd":"stop"}` | Step motoru anında durdur |
| `{"cmd":"dc","dir":"forward"}` | DC motor ileri, tam hız (speed verilmezse varsayılan %100) |
| `{"cmd":"dc","dir":"forward","speed":30}` | DC motor ileri, %30 hız (PWM duty) |
| `{"cmd":"dc","dir":"backward","speed":30}` | DC motor geri, %30 hız |
| `{"cmd":"dc","dir":"stop"}` | DC motor dur |
| `{"cmd":"reset"}` | İki encoder sayacını da sıfırla |
| `{"cmd":"ping"}` | Bağlantı testi |

## STM32 → Pi/PC (durum, saniyede ~20 kez)

```json
{"t":12345,"e1":1834,"e2":1801,"um1":96031,"um2":94303,"remaining":0,"running":0,"dc":0,"dcSpeed":0}
```

| Alan          | Anlamı                                                     |
| ------------- | ---------------------------------------------------------- |
| `t`           | Kart açıldığından beri geçen ms                            |
| `e1` / `e2`   | Encoder 1 / 2 toplam sayım (işaretli, geri dönünce azalır) |
| `um1` / `um2` | Aynı sayımın mikrometre karşılığı (1000'e bölünce mm)      |
| `remaining`   | Step motorun atmayı bekleyen darbe sayısı                  |
| `running`     | 1 = step motor hareket halinde                             |
| `dc`          | 0 = dur, 1 = ileri, 2 = geri                               |
| `dcSpeed`     | DC motorun o anki PWM duty'si, 0-100 (dur ise 0)           |

## DC motor PWM (22-09-2026 eklendi, backlog #109)

L9110 artık sadece ON/OFF değil, gerçek PWM ile sürülüyor (`speed` alanı 0-100,
yoksa eski davranışla uyumlu olacak şekilde varsayılan 100 = tam hız). PWM
frekansı 1 kHz (firmware'de `DC_PWM_PERIOD_TICKS`/`DC_PWM_PRESCALER`, main.c) -
bu L9110 datasheet'inde belirtilen bir değer DEĞİL (datasheet'te "optimal PWM
frekansı" diye bir bilgi yok), hobi-seviye DC motor sürücülerinde yaygın kabul
gören bir başlangıç değeri. **Sahada doğrulanacak:** çok düşük `speed` (ör.
5-10) değerinde motor gerçekten sürekli dönüyor mu (minimum çalışır duty),
ses/ısınma sorunlu mu.

**Donanım kaynağı notu:** STM32G031K8'de DC_IA1 (PB8) TIM16_CH1, DC_IB1 (PB9)
TIM17_CH1 alternate function ile PWM üretebiliyor (RM0444 + community
doğrulaması). TIM16 daha önce step darbe üretecinin (base/kesme modu) zaman
tabanıydı - PWM ile aynı anda kullanılamayacağı için (tek sayaç/ARR, step'in
sürekli değişen 20-60000 us periyoduyla PWM'in sabit periyodu çakışır) step
darbe üreteci **TIM14'e taşındı** (main.c/stm32g0xx_hal_msp.c/stm32g0xx_it.c,
fonksiyonel olarak birebir aynı, sadece hangi timer'ın kullanıldığı değişti -
yeni pin/kablo YOK). TIM17 zaten boştaydı, doğrudan kullanıldı.

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
