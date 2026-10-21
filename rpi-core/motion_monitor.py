"""
FeedVision — Hareket durum lambası mantığı (21-10-2026, backlog #120 madde 5)

Ne yapar: Besleme sırasında "komut gitti ama çubuk hareket etmiyor" durumunu
operatörün tek bakışta görmesi için, STM32 durum akışından (encoder sayaçları
e1/e2 + `running`) çubuğun GERÇEKTEN hareket edip etmediğini çıkarır.
Yalnızca NEMA23 (step) encoder ile doğrulanır; rotasyon/yardımcı/DC için
encoder yok, onların göstergeleri sadece firmware'in "komut aldım" durumunu
(rot/assist/dc alanları) yansıtır ve burada DEĞERLENDİRİLMEZ.

Neden sunucuda: mantık Python'da olunca CI'da test edilir (JS test altyapısı
yok) ve Operatör/Admin/duvar ekranı aynı sonucu görür. Arayüz yalnızca
`snapshot()` çıktısını çizer (/ws/status içinde `motion` alanı).

Durumlar (`state`):
  idle      gri    — boşta
  starting  amber  — komut onaylandı (running=1), hareket henüz görülmedi, süre dolmadı
  moving    yeşil  — encoder hareketi var
  stalled   kırmızı— running=1 ama STALL_TIMEOUT_S boyunca encoder hareketi yok
              (HATA SAYILMAZ, otomatik durdurma yok; sadece gösterge)
  fault     kırmızı— bir HATA oluştu ve tüm motorlar durduruldu (21-10-2026,
              bkz. main._trigger_fault); operatör Dur'a basana ya da yeni bir
              besleme başarıyla başlayana kadar kalır
`diag` (teşhis kodu, arayüz metne çevirir): idle, disconnected, cmd_not_sent,
starting, moving, moving_no_step, stalled, fault.

Saf mantık: zaman dışarıdan verilir (`now`, saniye), bridge/FastAPI'ye
bağımlılığı yok -> sahte saatle deterministik test edilir.
"""

import threading
from collections import deque
from typing import Optional

# running=1 iken bu süre boyunca encoder hareketi görülmezse "hareket yok".
STALL_TIMEOUT_S = 1.5
# Hareket tespiti penceresi (net yer değiştirme bu pencerede ölçülür). Stall
# süresiyle aynı tutuldu: en düşük hızlarda bile (~2 sayım/sn) pencere içinde
# eşiği aşacak kadar sayım birikir.
MOTION_WINDOW_S = 1.5
# Pencerede net en az bu kadar sayım değişmeli. 1 sayımlık titreşim (kuadratür
# kenarında gidip gelme) hareket sayılmaz.
MIN_COUNTS_FOR_MOTION = 2
# Başarısız komut bilgisi bu süre sonra silinir (eski hata ekranda asılı kalmasın).
CMD_FAILURE_TTL_S = 30.0


class MotionMonitor:
    def __init__(self, stall_timeout_s: float = STALL_TIMEOUT_S, window_s: float = MOTION_WINDOW_S):
        self._lock = threading.Lock()
        self.stall_timeout_s = stall_timeout_s
        self.window_s = window_s
        self._samples: deque = deque()  # (t, e1, e2)
        self._running = False
        self._connected = False
        self._running_since: Optional[float] = None
        self._last_motion_at: Optional[float] = None
        self._cmd_failure: Optional[dict] = None  # {"at", "reason"}
        self._fault: Optional[dict] = None  # {"id","line","motor","detail","stopped_all"}
        self._fault_seq = 0

    # ---- girişler ------------------------------------------------------
    def reset_samples(self) -> None:
        """Encoder sayaçları sıfırlandı (reset komutu): eski örnekler yanlış
        bir sıçrama (hareket) üretmesin."""
        with self._lock:
            self._samples.clear()

    def note_command(self, ok: bool, reason: Optional[str], now: float) -> None:
        """NEMA23 komutunun sonucu. ok=False: komut gitmedi / reddedildi
        (bağlı değil, yanıt yok, firmware err). ok=True: başarısızlık kaydını temizler."""
        with self._lock:
            self._cmd_failure = None if ok else {"at": now, "reason": reason or "bilinmeyen neden"}

    def note_stop(self) -> None:
        """Operatör Dur'a bastı: eski komut hatası ve HATA durumu ekranda kalmasın."""
        with self._lock:
            self._cmd_failure = None
            self._fault = None

    def note_fault(self, line: str, motor: str, detail: str, stopped_all: bool) -> dict:
        """Bir HATA kaydeder (lamba kırmızı + banner + konsol satırı). Her kayıt yeni bir
        `id` alır; arayüz yeni id'yi görünce konsola bir kez yazar. Dönen sözlük arayüze gider."""
        with self._lock:
            self._fault_seq += 1
            self._fault = {"id": self._fault_seq, "line": line, "motor": motor, "detail": detail, "stopped_all": stopped_all}
            return dict(self._fault)

    def clear_fault(self) -> None:
        """Yeni bir besleme başarıyla başladı: eski HATA durumu kalkar."""
        with self._lock:
            self._fault = None

    def observe(self, status: dict, connected: bool, now: float) -> None:
        """Bir durum örneği işler (arka plan döngüsü ~10 Hz çağırır)."""
        with self._lock:
            self._connected = bool(connected)
            if not connected or not status:
                self._samples.clear()
                self._set_running(False, now)
                return
            e1, e2 = status.get("e1"), status.get("e2")
            if isinstance(e1, (int, float)) and isinstance(e2, (int, float)):
                self._samples.append((now, e1, e2))
            while self._samples and now - self._samples[0][0] > self.window_s:
                self._samples.popleft()
            self._set_running(bool(status.get("running")), now)
            if self._motion_detected():
                self._last_motion_at = now

    # ---- iç yardımcılar (kilit altında çağrılır) ----------------------
    def _set_running(self, running: bool, now: float) -> None:
        if running and not self._running:
            self._running_since = now
        if not running:
            self._running_since = None
        self._running = running

    def _deltas(self) -> tuple[float, float, float]:
        """Penceredeki (net Δe1, net Δe2, süre)."""
        if len(self._samples) < 2:
            return 0.0, 0.0, 0.0
        t0, a0, b0 = self._samples[0]
        t1, a1, b1 = self._samples[-1]
        return a1 - a0, b1 - b0, t1 - t0

    def _motion_detected(self) -> bool:
        d1, d2, _ = self._deltas()
        return max(abs(d1), abs(d2)) >= MIN_COUNTS_FOR_MOTION

    # ---- çıkış -----------------------------------------------------------
    def snapshot(self, now: float) -> dict:
        """Arayüzün çizeceği durum. `enc1_cps`/`enc2_cps`: pencere ortalaması,
        sayım/sn (işaretli); arayüz kendi teker çapıyla mm/s'e çevirir."""
        with self._lock:
            d1, d2, dt = self._deltas()
            enc1_cps = d1 / dt if dt >= 0.3 else 0.0
            enc2_cps = d2 / dt if dt >= 0.3 else 0.0
            moving = self._motion_detected()

            failure = self._cmd_failure
            if failure is not None and now - failure["at"] > CMD_FAILURE_TTL_S:
                failure = None
                self._cmd_failure = None

            if self._fault is not None:
                state, diag = "fault", "fault"
            elif not self._connected:
                state, diag = "idle", "disconnected"
            elif self._running:
                if moving:
                    state, diag = "moving", "moving"
                else:
                    waited_since = max(
                        t for t in (self._running_since, self._last_motion_at) if t is not None
                    )
                    if now - waited_since >= self.stall_timeout_s:
                        state, diag = "stalled", "stalled"
                    else:
                        state, diag = "starting", "starting"
            elif moving:
                # step komutu yok ama encoder dönüyor: rotasyon/yardımcı/DC ya da elle
                state, diag = "moving", "moving_no_step"
            elif failure is not None:
                state, diag = "idle", "cmd_not_sent"
            else:
                state, diag = "idle", "idle"

            return {
                "state": state,
                "diag": diag,
                "reason": (
                    self._fault["line"] if diag == "fault"
                    else failure["reason"] if (failure is not None and diag == "cmd_not_sent") else None
                ),
                "fault": dict(self._fault) if self._fault is not None else None,
                "running": self._running,
                "enc1_cps": round(enc1_cps, 2),
                "enc2_cps": round(enc2_cps, 2),
                "stall_timeout_s": self.stall_timeout_s,
            }


# Tek, paylaşılan izleyici — main.py bunu import edip kullanır (bridge ile aynı desen).
motion_monitor = MotionMonitor()
