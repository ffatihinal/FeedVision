"""
FeedVision — Günlük Toplam Besleme Miktarı (Operasyonel Kayıt sayfası)

Ne yapar: SADECE step motor hareket halindeyken (STM32 durumundaki
`remaining > 0` olduğu anlarda) iki encoder'ın mikrometre karşılığını
(um1/um2) örnekler arası farkla toplar, mm'e çevirip GÜNLÜK TOPLAMA ekler.
main.py'deki periyodik Kontrol Kriterleri döngüsünde (_rule_engine_loop,
~2sn'de bir) çağrılır — TEK bir yerde hesaplanır, kod tekrarı yok
(15-09-2026 Fatih talimatı).

Neden "sadece hareket halindeyken": hareket yokken encoder sayımı zaten
değişmez (fark sıfır), ama STM32'den gelen ardışık iki örnek arasında
gürültü/anlık bir sıçrama olursa bunu günlük toplama YANLIŞLIKLA
eklememek için ekstra güvenlik katmanı — remaining=0 iken hiçbir ekleme
yapılmaz, sadece "referans" (prev_um) güncellenir ki motor tekrar hareket
edince ilk örnekte sahte bir sıçrama oluşmasın.

Yön-farkındalığı (29-09-2026 eklendi, Madde 5): STM32'nin periyodik durumu
step motorun O ANKİ yönünü İÇERMİYOR (bkz. docs/protocol.md — `dc` alanı
sadece DC motor yönü, step için yok). Bu yüzden main.py, en son gönderilen
step komutunun (`/motor/step` VEYA `/motor/feed-start`) `dir`'ini kendi
modül-seviyesi state'inde ayrıca tutup `update()`'e `step_dir` olarak
geçiriyor (bkz. main.py `_last_step_dir`). Sadece step motor İLERİ
(dir=0) giderken encoder artışı "besleme" (forward) sayılır; GERİ (dir=1)
giderken ayrı bir "geri çekme" (backward) toplamına gider. `step_dir`
bilinmiyorsa (servis daha hiç step komutu görmediyse, None) hiçbir tarafa
eklenmez — yön belirsizken "muhtemelen ileri" varsayıp yanlış sayım
yapmaktansa hiç saymamak tercih edildi.

Günlük tanım: main.py/journal.py'deki "gün başına yeni kayıt" ile AYNI
yerel tarih sınırı (YYYY-MM-DD). Gün değişince forward/backward toplamları
sıfırlanır.

Kalıcılık: feed_total_state.json'a her güncellemede yazılır (aynı gün
içinde servis restart olursa toplam kaybolmasın diye) — roi_store.py ile
aynı atomik yazım deseni (.tmp'ye yaz + rename). Eski şema (yön ayrımı
olmadan sadece total_mm_e1/e2 tutan) okununca sessizce yeni varsayılana
sıfırlanır — günlük bir sayaç, geriye dönük veri kaybı kabul edilebilir.

Bu dosya main.py'ye BAĞIMLI değil (kamera/seri port G/Ç'si yok) — saf
mantık + dosya G/Ç, donanımsız test edilebilir.
"""

import json
from datetime import datetime
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent / "feed_total_state.json"

# İki encoder'ın (tahrik + boşta klavuz — bkz. proje notları "patinaj"
# takibi) hesapladığı mm birbirinden bu ORANDAN fazla saparsa VE mutlak
# fark bu MİKTARDAN büyükse mismatch uyarısı verilir. Motoru DURDURMAZ,
# sadece bilgilendirme — Kontrol Kriterleri'nin motor-durdurma davranışından
# KASITLI OLARAK farklı (15-09-2026 Fatih talimatı). Başlangıç tahmini
# değerler, saha verisiyle ayarlanabilir.
MISMATCH_RATIO = 0.05
MISMATCH_MIN_MM = 5.0


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _default_state(date: str) -> dict:
    return {
        "date": date,
        "total_forward_mm_e1": 0.0,
        "total_forward_mm_e2": 0.0,
        "total_backward_mm_e1": 0.0,
        "total_backward_mm_e2": 0.0,
        "prev_um1": None,
        "prev_um2": None,
    }


def _load_state(state_path: Path = STATE_PATH) -> dict:
    today = _today_str()
    if not state_path.exists():
        return _default_state(today)
    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state(today)
    if not isinstance(data, dict) or data.get("date") != today:
        # Gün değişmiş (ya da dosya bozuk/beklenmedik alan eksik) — yeni
        # güne sıfırdan başla, eski günün verisi dosyada kalır ama
        # kullanılmaz (istenirse ileride arşivlenebilir, MVP kapsamı dışı).
        return _default_state(today)
    for key in (
        "total_forward_mm_e1",
        "total_forward_mm_e2",
        "total_backward_mm_e1",
        "total_backward_mm_e2",
        "prev_um1",
        "prev_um2",
    ):
        if key not in data:
            # Eski şema (yön ayrımı olmadan sadece total_mm_e1/e2) da BURADAN
            # sessizce sıfırlanır — anahtar isimleri farklı olduğu için zaten
            # eksik sayılır, ayrı bir migrasyon yazmaya gerek yok.
            return _default_state(today)
    return data


def _save_state(state: dict, state_path: Path = STATE_PATH) -> None:
    tmp_path = state_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(state_path)


class FeedTotalizer:
    """Günlük besleme toplamını tutan tekil nesne — main.py bunu import edip
    her Kontrol Kriterleri döngü turunda update() çağırır.

    Sınıf olarak tasarlandı (modül-seviyesi global yerine) ki testler kendi
    izole state_path'iyle bağımsız birer örnek oluşturabilsin — gerçek
    feed_total_state.json'a dokunmadan test edilebilir.
    """

    def __init__(self, state_path: Path = STATE_PATH):
        self._state_path = state_path
        self._state = _load_state(state_path)

    def update(self, status: dict, step_dir: int | None = None) -> dict:
        """Bir STM32 durum örneğini işler, gerekiyorsa günlük toplama ekler.

        status: bridge.get_status() çıktısı (um1/um2/remaining alanları
        beklenir — eksikse sessizce atlanır, servis çökmesin).
        step_dir: en son gönderilen step komutunun yönü (0=ileri, 1=geri —
        main.py `_last_step_dir` state'i, bkz. modül docstring'i). None ise
        (henüz hiç step komutu görülmedi) pozitif delta HİÇBİR tarafa
        eklenmez, sadece prev_um referansı güncellenir.
        Döner: güncel günlük özet (bkz. get_summary()).
        """
        today = _today_str()
        if self._state["date"] != today:
            self._state = _default_state(today)

        um1 = status.get("um1")
        um2 = status.get("um2")
        is_moving = (status.get("remaining") or 0) > 0

        if um1 is not None and um2 is not None:
            prev1 = self._state["prev_um1"]
            prev2 = self._state["prev_um2"]
            if is_moving and prev1 is not None and prev2 is not None:
                # mm = mikrometre / 1000. Negatif farkı (encoder resetlendi/
                # geri döndü) toplama dahil ETMİYORUZ — sahte/negatif
                # "besleme" eklenmesin, sadece pozitif ilerleme sayılır.
                delta1_mm = max(0.0, (um1 - prev1) / 1000.0)
                delta2_mm = max(0.0, (um2 - prev2) / 1000.0)
                if step_dir == 0:
                    self._state["total_forward_mm_e1"] += delta1_mm
                    self._state["total_forward_mm_e2"] += delta2_mm
                elif step_dir == 1:
                    self._state["total_backward_mm_e1"] += delta1_mm
                    self._state["total_backward_mm_e2"] += delta2_mm
                # step_dir None -> yön bilinmiyor, sessizce sayılmaz (yukarıdaki
                # docstring'de gerekçe var).
            self._state["prev_um1"] = um1
            self._state["prev_um2"] = um2
            _save_state(self._state, self._state_path)

        return self.get_summary()

    def get_summary(self) -> dict:
        """Günlük özet.

        `total_mm_e1`/`total_mm_e2`/`total_mm_average` artık NET (ileri-geri
        fark) — Fatih'in 29-09-2026 talebi: "ileri 100mm + geri 20mm + ileri
        20mm" senaryosunda headline rakam 140mm değil 100mm olmalı. Ayrıca
        ham ileri/geri ayrımı (`total_forward_mm_*`/`total_backward_mm_*`)
        şeffaflık için ayrıca dönüyor.

        `mismatch_warning` NET üzerinden DEĞİL, iki encoder'ın KAT ETTİĞİ TOPLAM
        mesafe (ileri+geri, yön farketmeksizin) üzerinden hesaplanıyor — patinaj
        iki yönde de simetrik olursa net fark üzerinden gizlenebilirdi, bu
        yüzden bilinçli olarak ham mesafe kıyaslanıyor (mekanik kaynaklı
        uyuşmazlık sinyali, net beslemeden bağımsız).
        """
        fwd1 = self._state["total_forward_mm_e1"]
        fwd2 = self._state["total_forward_mm_e2"]
        bwd1 = self._state["total_backward_mm_e1"]
        bwd2 = self._state["total_backward_mm_e2"]
        net1 = fwd1 - bwd1
        net2 = fwd2 - bwd2
        net_average = (net1 + net2) / 2.0

        dist1 = fwd1 + bwd1
        dist2 = fwd2 + bwd2
        dist_diff = abs(dist1 - dist2)
        mismatch = dist_diff > MISMATCH_MIN_MM and dist_diff > MISMATCH_RATIO * max(dist1, dist2, 1.0)

        return {
            "date": self._state["date"],
            "total_mm_e1": round(net1, 2),
            "total_mm_e2": round(net2, 2),
            "total_mm_average": round(net_average, 2),
            "total_forward_mm_e1": round(fwd1, 2),
            "total_forward_mm_e2": round(fwd2, 2),
            "total_backward_mm_e1": round(bwd1, 2),
            "total_backward_mm_e2": round(bwd2, 2),
            "mismatch_warning": mismatch,
        }


# Tek, paylaşılan örnek — main.py bunu import edip kullanır (roi_store.py/
# serial_bridge.py ile aynı desen: modül-seviyesi tekil nesne).
totalizer = FeedTotalizer()
