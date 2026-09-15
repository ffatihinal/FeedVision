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

Günlük tanım: main.py/journal.py'deki "gün başına yeni kayıt" ile AYNI
yerel tarih sınırı (YYYY-MM-DD). Gün değişince toplam sıfırlanır.

Kalıcılık: feed_total_state.json'a her güncellemede yazılır (aynı gün
içinde servis restart olursa toplam kaybolmasın diye) — roi_store.py ile
aynı atomik yazım deseni (.tmp'ye yaz + rename).

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
        "total_mm_e1": 0.0,
        "total_mm_e2": 0.0,
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
    for key in ("total_mm_e1", "total_mm_e2", "prev_um1", "prev_um2"):
        if key not in data:
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

    def update(self, status: dict) -> dict:
        """Bir STM32 durum örneğini işler, gerekiyorsa günlük toplama ekler.

        status: bridge.get_status() çıktısı (um1/um2/remaining alanları
        beklenir — eksikse sessizce atlanır, servis çökmesin).
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
                self._state["total_mm_e1"] += delta1_mm
                self._state["total_mm_e2"] += delta2_mm
            self._state["prev_um1"] = um1
            self._state["prev_um2"] = um2
            _save_state(self._state, self._state_path)

        return self.get_summary()

    def get_summary(self) -> dict:
        """Günlük özet: iki encoder toplamı + ortalama (headline rakam) + uyuşmazlık uyarısı."""
        e1 = self._state["total_mm_e1"]
        e2 = self._state["total_mm_e2"]
        average = (e1 + e2) / 2.0
        diff = abs(e1 - e2)
        mismatch = diff > MISMATCH_MIN_MM and diff > MISMATCH_RATIO * max(e1, e2, 1.0)
        return {
            "date": self._state["date"],
            "total_mm_e1": round(e1, 2),
            "total_mm_e2": round(e2, 2),
            "total_mm_average": round(average, 2),
            "mismatch_warning": mismatch,
        }


# Tek, paylaşılan örnek — main.py bunu import edip kullanır (roi_store.py/
# serial_bridge.py ile aynı desen: modül-seviyesi tekil nesne).
totalizer = FeedTotalizer()
