# Pi ↔ STM32 Protokolü

USB kablo üzerinden, satır bazlı JSON (`\n` ile biten tek satır komut/durum).
Kaynak karar: `yazilim_mimarisi.md` Bölüm 3 (Azobex WP1 vault) + `feedvision_test/firmware/CUBEMX_KURULUM.md` Bölüm 12.

## PC/Pi → STM32 (komut)

| Komut | Anlamı |
|---|---|
| `{"cmd":"step","dir":1,"delay":500,"steps":2000}` | 2000 step at, darbe periyodu 500 µs, yön 1 |
| `{"cmd":"stop"}` | Step motoru anında durdur |
| `{"cmd":"dc","dir":"ileri"}` | DC motor ileri |
| `{"cmd":"dc","dir":"geri"}` | DC motor geri |
| `{"cmd":"dc","dir":"dur"}` | DC motor dur |
| `{"cmd":"sifirla"}` | İki encoder sayacını da sıfırla |
| `{"cmd":"ping"}` | Bağlantı testi |

## STM32 → Pi/PC (durum, saniyede ~20 kez)

```json
{"t":12345,"e1":1834,"e2":1801,"um1":96031,"um2":94303,"kalan":0,"calisiyor":0,"dc":0}
```

| Alan | Anlamı |
|---|---|
| `t` | Kart açıldığından beri geçen ms |
| `e1` / `e2` | Encoder 1 / 2 toplam sayım (işaretli, geri dönünce azalır) |
| `um1` / `um2` | Aynı sayımın mikrometre karşılığı (1000'e bölünce mm) |
| `kalan` | Step motorun atmayı bekleyen darbe sayısı |
| `calisiyor` | 1 = step motor hareket halinde |
| `dc` | 0 = dur, 1 = ileri, 2 = geri |

Gerçek üretim protokolü (Pi tarafı `feedvision-core`) bu test protokolünü temel alacak, komut seti büyüyecek (SE ekibinin ICD'siyle uyumlu hale gelecek).
