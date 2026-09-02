# FeedVision Test Firmware — Adım Adım CubeMX Kurulumu

Kart: **NUCLEO-G031K8 (STM32G031K8T6, LQFP32)**
Amaç: Nema23 (DM556) + 6V DC motor (L9110) + 2× encoder test firmware'i.

---

## 0. Önce doğrulanmış pin bilgisi (bunu bir kez oku)

Aşağıdaki pinler ST'nin kendi resmi pin veritabanından (`STM32_open_pin_data`, dosya
`mcu/STM32G031K(4-6-8)Tx.xml`) **birebir doğrulandı** — LQFP32 paketinde fiziksel pin
konumları ve o pinin destekediği fonksiyonlar:

| STM32 pini | LQFP32 fiziksel pin no | Kullanacağımız fonksiyon | Doğrulama |
|---|---|---|---|
| PA2 / PA3 | 9 / 10 | USART2_TX / USART2_RX (ST-LINK sanal COM portu) | XML'de `USART2_TX` / `USART2_RX` listeli |
| PA8 | 18 | TIM1_CH1 | XML: PA8 → `TIM1_CH1` |
| PA9 | 19 | TIM1_CH2 | XML: PA9 → `TIM1_CH2` |
| PA6 | 13 | TIM3_CH1 | XML: PA6 → `TIM3_CH1` |
| PA7 | 14 | TIM3_CH2 | XML: PA7 → `TIM3_CH2` |
| PA15 | 26 | GPIO Output (STEP / PUL+) | XML'de GPIO |
| PB1 | 16 | GPIO Output (DIR+) | XML'de GPIO |
| PB8 | 32 | GPIO Output (L9110 IA1) | XML'de GPIO |
| PB9 | 1 | GPIO Output (L9110 IB1) | XML'de GPIO |

**Önemli not (bir yanlış anlaşılmayı önlemek için):** STM32G0'ın küçük paketlerinde
(TSSOP20, UFQFPN28 vb.) PA9/PA10 kendi pinlerine sahip değildir, PA11/PA12 pini üzerinden
"remap" edilir. **Bizim paketimizde (LQFP32) böyle bir sorun YOK** — PA9 pin 19'da kendi
başına duruyor, PA11 ise ayrı bir pin (pin 22). Yani hiçbir SYSCFG remap ayarı yapmana
gerek yok, PA8/PA9'u doğrudan TIM1 encoder olarak kullanabilirsin.

Encoder 1 = **D9 (PA8)** ve **D5 (PA9)**, Encoder 2 = **A6 (PA6)** ve **A7 (PA7)**
(Arduino header karşılıkları — kart üstündeki baskılı yazıyla teyit et).

---

## 1. Proje oluştur

**En garantili yol (02-09'da doğrulandı): bağımsız STM32CubeMX uygulamasından git, CubeIDE'nin kendi sihirbazından değil.**
Neden: CubeIDE'nin "File → New → STM32 Project" sihirbazı arka planda CubeMX'i çağırır ve bazı Mac kurulumlarında bunu bulamayıp "Error opening STM32CubeMX" hatası veriyor. CubeMX → CubeIDE yönü (Generate Code → Open Project) her zaman sağlam çalışıyor.

1. **STM32CubeMX** uygulamasını aç → **File → New Project**.
2. **Board Selector** sekmesinde (⚠️ **MCU Selector değil** — sağdaki çıplak çip listesinden seçersen SWD/debug pinleri boş kalır, kart flaşlanamayabilir) arama kutusuna `NUCLEO-G031K8` yaz → seç.
3. Board Selector ile açıldığını doğrula: pin şemasında PA2/PA3 (VCP), PC6 (LD3 LED), PA13/PA14 (JTMS/JTCK) **otomatik yeşil/ayarlı** gelmeli. Gelmiyorsa yanlış yoldasın, projeyi kapat baştan başla.

---

## 2. Saat (Clock) ayarı — 64 MHz

**Clock Configuration** sekmesine geç:

1. `HCLK (MHz)` kutusuna **64** yaz, Enter'a bas.
2. CubeMX "PLL kullanmam gerekiyor, otomatik çözeyim mi?" diye sorar → **OK / Yes**.
3. Sonuç şöyle olmalı: HSI 16 MHz → PLL → **SYSCLK 64 MHz**, `APB1 timer clocks = 64 MHz`.

**Neden 64 MHz:** step darbelerini üreten timer kesmesi çok sık çalışacak (en hızlı ayarda
her 10 µs'de bir). 16 MHz'de işlemcinin buna yetişme payı dar kalır, 64 MHz'de rahat.
64 MHz bu çipin (STM32G031) maksimum hızıdır.

---

## 3. USART2 (PC ile konuşma hattı)

**Pinout & Configuration → Connectivity → USART2**:

1. Mode: **Asynchronous** (Board Selector zaten böyle ayarlamış olmalı)
2. Parameter Settings:
   - Baud Rate: **115200**
   - Word Length: **8 Bits** ⚠️ CubeMX'in varsayılanı bazen **7 Bits** geliyor, kontrol et — JSON/ASCII metin göndereceğiz, 7 bit'te bazı karakterler (`{`, `}`, `"`) bozulabilir.
   - Parity: None
   - Stop Bits: 1
3. **NVIC Settings** sekmesi → `USART2 global interrupt` → **Enabled** ✔
   (Bu kutuyu işaretlemezsen kart PC'den gelen komutları duymaz.)
4. Pinlerin PA2 (TX) / PA3 (RX) olduğunu pinout görünümünden teyit et. Bu pinler
   kartta ST-LINK'e bağlı, USB kablosunu taktığında PC'de bir COM portu olarak görünür.

---

## 4. TIM1 — Encoder 1 (motorlu tekerlek)

**Timers → TIM1**:

1. `Combined Channels` açılır listesi → **Encoder Mode**
   → PA8 ve PA9 otomatik olarak TIM1_CH1 / TIM1_CH2 olur.
2. **Parameter Settings** sekmesinde:
   - `Prescaler` : **0**
   - `Counter Period (AutoReload Register)` : **65535** ← **BU ÇOK ÖNEMLİ**
   - `Encoder Mode` : **Encoder Mode TI1 and TI2** (A ve B kanalının 4 kenarını da sayar)
   - Channel 1 ve Channel 2 için:
     - `Polarity` : Rising Edge
     - `Input Filter` : **15** (en yüksek filtreleme)
3. NVIC ayarı **gerekmiyor** (encoder'ı kesmeyle değil, sorgulayarak okuyacağız).

> **`Counter Period = 65535` neden önemli:** CubeMX bu alanı varsayılan olarak 0 bırakır.
> 0 bırakırsan sayaç her 1 darbede başa döner ve encoder'dan hiç anlamlı sayı okuyamazsın.
> 65535 yazınca sayaç 16 bitin tamamını kullanır.

> **`Input Filter = 15` neden:** encoder kabloları ~150 cm ve sinyal 4.7k pull-up ile
> çekiliyor; filtre gürültüden gelen sahte darbeleri eler. 15 ayarı ~4 µs'den kısa
> darbeleri yok sayar. Motor 300 dev/dak'ta bile darbeler arası ~80 µs olduğu için
> gerçek sinyali kaybetmezsin. Sayım kaçırdığını görürsen bu değeri düşür.

---

## 5. TIM3 — Encoder 2 (boşta tekerlek)

**Timers → TIM3** → TIM1 ile **birebir aynı** ayarlar:

1. `Combined Channels` → **Encoder Mode** (PA6 / PA7 otomatik atanır)
2. Prescaler **0**, Counter Period **65535**
3. Encoder Mode: **TI1 and TI2**, iki kanal da Rising Edge, Input Filter **15**

---

## 6. TIM16 — Step darbesi üreteci

**Timers → TIM16**:

1. `Activate` kutusunu işaretle (Mode: sadece "Activated", hiçbir kanal seçme —
   böylece TIM16 hiçbir pini işgal etmez, sadece zamanlayıcı olarak çalışır).
2. **Parameter Settings**:
   - `Prescaler` : **63**
   - `Counter Period` : **499**
   - `auto-reload preload` : **Disable**
   - `Repetition Counter` : 0
3. **NVIC Settings** → `TIM16 global interrupt` → **Enabled** ✔
4. **NVIC** (sol menüdeki `System Core → NVIC`) → öncelik ayarı:
   - `TIM16 global interrupt` → Preemption Priority = **0**
   - `USART2 global interrupt` → Preemption Priority = **2**
   - `Time base: System tick timer` → Preemption Priority = **3**

> **Prescaler 63 hesabı:** timer saati 64 MHz. 64 MHz / (63+1) = **1 MHz** → sayacın her
> tıkı tam **1 mikrosaniye**. Böylece kodda "500 µs bekle" demek için doğrudan 500 yazabiliyoruz.
> `Counter Period 499` sadece başlangıç değeri; gerçek değeri kod çalışırken PC'den gelen
> hız komutuna göre değiştiriyor.

> **Öncelik neden böyle:** step darbesi zamanlaması bozulursa motor adım kaçırır, bu yüzden
> TIM16 en yüksek öncelikte (0). Seri porttan komut gelmesi birkaç mikrosaniye gecikse
> hiçbir şey olmaz, o yüzden daha düşük (2).

---

## 7. GPIO pinleri (motor sinyalleri)

Pinout görünümünde **sol tıkla → fonksiyon seç**, sonra **sağ tıkla → Enter User Label**:

| Pin | Fonksiyon | User Label (aynen yaz) |
|---|---|---|
| PA15 | GPIO_Output | `STEP` |
| PB1 | GPIO_Output | `DIR` |
| PB8 | GPIO_Output | `DC_IA1` |
| PB9 | GPIO_Output | `DC_IB1` |

Sonra **System Core → GPIO** sekmesinde bu 4 pin için:

- `GPIO output level` : **Low**
- `GPIO mode` : Output Push Pull
- `GPIO Pull-up/Pull-down` : No pull-up and no pull-down
- `Maximum output speed` : **High**

Encoder pinleri (PA6/PA7/PA8/PA9) için ekstra bir şey yapma — Encoder Mode seçtiğinde
CubeMX doğru ayarı zaten yapıyor. (Kartın dışında zaten 4.7k pull-up dirençleri var.)

> **User Label'ları neden yazıyoruz:** CubeMX bu etiketlerden `STEP_Pin`, `STEP_GPIO_Port`
> gibi tanımlar üretir; verdiğim kod bunları kullanıyor. Etiket yazmazsan da kod çalışır
> (içinde yedek tanımlar var) ama etiketleri yazman daha temiz olur.

**ENA (DM556 enable):** şimdilik bağlanmıyor, CubeMX'te hiçbir pin ayırma.
DM556'da ENA boşta bırakılırsa sürücü sürekli aktif kalır — test için istediğimiz bu.

---

## 8. Proje ayarları ve kod üretme

1. **Project Manager → Project** (⚠️ bu ikisi CubeMX'te bazen boş/yanlış geliyor, Generate Code'dan ÖNCE mutlaka kontrol et):
   - **Project Name**: boş gelebilir → elle `feedvision_test` yaz.
   - **Toolchain / IDE**: varsayılan olarak **EWARM (IAR)** gelebilir → mutlaka **STM32CubeIDE** seç. EWARM'da kalırsa CubeMX, CubeIDE'nin **açamayacağı** proje dosyaları üretir — tüm iş baştan.
   - Minimum Heap Size: `0x200`
   - Minimum Stack Size: `0x400`
2. **Project Manager → Code Generator**:
   - ✔ `Generate peripheral initialization as a pair of '.c/.h' files per peripheral`
     (isteğe bağlı, sadece düzen için)
   - ✔ `Keep User Code when re-generating`  ← **mutlaka işaretli olsun**
3. **GENERATE CODE** butonuna bas → "Generate code?" → **Yes**. Başarı diyaloğunda **"Open Project"**'e bas (CubeIDE'yi projeyle açar).

---

## 9. Kullanıcı kodunu yapıştır

- `Core/Src/main.c` → `main_user_code.c` dosyasındaki blokları, dosyada belirtilen
  `/* USER CODE BEGIN ... */` işaretlerinin **arasına** sırasıyla yapıştır.
- `Core/Src/stm32g0xx_it.c` → `stm32g0xx_it_user_code.c` dosyasını oku; oraya
  **eklenecek bir şey yok**, sadece kontrol listesi var.
- ⚠️ **Her blok sonrası `Cmd+S` ile kaydet.** CubeIDE'de autosave yok (PyCharm/VSCode'a alışkınsan bu tuzağa düşülüyor) — kaydetmeden Build alırsan eski/boş hali derlenir.
- "Import Projects" / "Open Projects from File System" ile proje **zaten workspace'te** görünüyorsa ("Folder already imported as project" uyarısı) o pencereyi Cancel'la, Project Explorer'ı kontrol et (Window → Show View → Project Explorer) — proje muhtemelen zaten orada, sadece görünür değil.

---

## 10. Derleme ve ilk test

1. **Project → Build All** (`Ctrl+B`). 0 hata olmalı.
2. Kartı USB ile bağla → **Run → Run As → STM32 C/C++ Application**.
3. Kart üstündeki LED (LD3) yanıp sönmeye başlamalı — firmware çalışıyor demektir.
4. PC'de seri portu aç (STM32CubeIDE'nin kendi terminali veya `pc_ui/feedvision_test_ui.py`).
   115200 baud'da saniyede ~20 satır JSON akmalı:
   ```
   {"t":1234,"e1":0,"e2":0,"um1":0,"um2":0,"remaining":0,"running":0,"dc":0}
   ```
5. Encoder milini elle çevir → `e1` / `e2` değerleri değişmeli (bir yöne artmalı,
   diğer yöne azalmalı). Değişmiyorsa: kabloyu, 4.7k pull-up'ı ve `Counter Period = 65535`
   ayarını kontrol et.

---

## 11. Sahada mutlaka kontrol edilecekler

1. **DM556 mikroadım ayarı (SW5–SW8):** kart üzerinden fiziksel olarak ayarlanan
   mikroadım değeri not alınmalı — kod şu an "kaç step = kaç mm" hesabını yapmıyor
   (o hesap encoder ile ampirik yapılıyor, bkz. madde 2), ama ileride açık-çevrim
   mesafe hesabı eklenirse bu değer gerekecek.
2. **Tekerlek çapı:** koddaki `WHEEL_DIAMETER_MM` sabiti şu an **40.0 mm varsayım**.
   Gerçek tekerlek gelince kumpasla ölç ve bu sabiti düzelt — encoder mm hesabı buna bağlı.
3. **Encoder PPR:** koddaki `ENC_PPR` = 600. Encoder etiketinde yazan değerle karşılaştır.
4. **Step darbe genişliği:** DM556 datasheet'i "en yüksek frekans cevabı 200 kHz" diyor
   (yani en kısa darbe periyodu 5 µs). Kod güvenlik için alt sınırı **20 µs**'de tutuyor.
   İlk çalıştırmada osiloskopla PUL+ sinyaline bakıp darbenin düzgün göründüğünü teyit et
   — 3.3V ile optokuplör sürüyoruz, kenarlar beklenenden yavaş çıkabilir.
5. **Motor titriyor ama dönmüyorsa:** motor faz kabloları (A+/A-/B+/B-) yanlış eşleşmiştir.

---

## 12. Komut formatı (referans)

PC → STM32 (her komut tek satır, sonunda `\n`):

| Komut | Anlamı |
|---|---|
| `{"cmd":"step","dir":1,"delay":500,"steps":2000}` | 2000 step at, darbe periyodu 500 µs, yön 1 |
| `{"cmd":"stop"}` | Step motoru anında durdur |
| `{"cmd":"dc","dir":"forward"}` | DC motor ileri |
| `{"cmd":"dc","dir":"backward"}` | DC motor geri |
| `{"cmd":"dc","dir":"stop"}` | DC motor dur |
| `{"cmd":"reset"}` | İki encoder sayacını da sıfırla |
| `{"cmd":"ping"}` | Bağlantı testi |

STM32 → PC (saniyede 20 kez):

```json
{"t":12345,"e1":1834,"e2":1801,"um1":96031,"um2":94303,"remaining":0,"running":0,"dc":0}
```

| Alan | Anlamı |
|---|---|
| `t` | Kart açıldığından beri geçen ms |
| `e1` / `e2` | Encoder 1 / 2 toplam sayım (işaretli, geri dönünce azalır) |
| `um1` / `um2` | Aynı sayımın **mikrometre** karşılığı (1000'e bölünce mm) |
| `remaining` | Step motorun atmayı bekleyen darbe sayısı |
| `running` | 1 = step motor hareket halinde |
| `dc` | 0 = dur, 1 = ileri, 2 = geri |

> **Neden mm değil mikrometre gönderiyoruz:** STM32CubeIDE varsayılan olarak `printf`'te
> ondalık sayı (float) desteğini kapalı getirir; açmadan `%f` yazarsan ekrana çöp basar.
> Tam sayı olarak mikrometre gönderip bölme işlemini PC tarafında yapmak bu tuzağı
> tamamen ortadan kaldırıyor.
