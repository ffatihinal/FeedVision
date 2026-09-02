"""
FeedVision RPi Core — STM32 seri port köprüsü.

STM32 firmware'i (feedvision_test, bkz. /firmware) ile satır bazlı JSON
protokolü konuşur — protokol tanımı: /docs/protocol.md.

Nasıl çalışır: bağlantı kurulunca ayrı bir thread arka planda sürekli satır
okur (STM32 saniyede ~20 kez durum yolluyor), en son geleni bellekte tutar.
Komut gönderme ("step at", "dur" vb.) ana thread'den çağrılır, doğrudan
seri porta yazar. İki taraf ayrı thread olduğu için `_lock` ile korunuyor.
"""

import json
import threading
import time
from typing import Optional

import serial
from serial.tools import list_ports

BAUD = 115200

# Port fiziksel olarak açılabilir ama karşı taraf STM32 olmayabilir (Mac'in
# kendi sanal portları gibi) — gerçek cihazı doğrulamak için bu süre kadar
# STM32'nin otomatik durum yayınını (saniyede ~20 satır) bekliyoruz.
HANDSHAKE_TIMEOUT_S = 2.0


class STM32Bridge:
    def __init__(self):
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._last_status: dict = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.is_connected = False
        self.last_error: Optional[str] = None

    def list_available_ports(self) -> list[str]:
        """Mac/Pi'de o an takılı olan seri portların listesi (kullanıcı UI'dan seçsin diye)."""
        return [p.device for p in list_ports.comports()]

    def connect(self, port: str) -> bool:
        """Belirtilen porta bağlanmayı dener.

        DİKKAT: Port dosyasının açılabilmesi (macOS'ta sanal/hata ayıklama
        portları dahil çoğu port için kolayca başarılı olur) STM32'nin
        gerçekten orada olduğu anlamına GELMEZ. Bu yüzden port açıldıktan
        sonra STM32'nin kendiliğinden gönderdiği durum satırını (saniyede
        ~20 kez) kısa bir süre bekliyoruz — HANDSHAKE_TIMEOUT_S içinde
        geçerli bir JSON satırı gelmezse "bağlı değil" diyoruz ve portu
        kapatıyoruz, yanlış pozitif "Bağlı" göstermeyelim diye."""
        with self._lock:
            self._last_status = {}
        try:
            self._serial = serial.Serial(port, BAUD, timeout=1)
        except Exception as e:
            self.is_connected = False
            self.last_error = str(e)
            return False

        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

        # STM32'den gerçek veri gelene kadar (ya da zaman aşımına kadar) bekle.
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._lock:
                got_data = bool(self._last_status)
            if got_data:
                self.is_connected = True
                self.last_error = None
                return True
            time.sleep(0.05)

        # Zaman aşımı — port açıldı ama karşı taraf STM32 değil/cevap vermiyor.
        self._running = False
        self._serial.close()
        self._serial = None
        self.is_connected = False
        self.last_error = "Port açıldı ama STM32'den veri gelmedi (yanlış port ya da kart bağlı değil)"
        return False

    def disconnect(self):
        self._running = False
        if self._serial:
            self._serial.close()
        self.is_connected = False

    def _listen(self):
        """Arka planda sürekli satır okur, JSON'a çevirip bellekte tutar.
        Bozuk/eksik bir satır gelirse (kablo gürültüsü vb.) atlar, çökmez."""
        while self._running and self._serial:
            try:
                raw = self._serial.readline().decode("utf-8", errors="ignore").strip()
                if not raw:
                    continue
                data = json.loads(raw)
                with self._lock:
                    self._last_status = data
            except json.JSONDecodeError:
                continue
            except Exception as e:
                self.last_error = str(e)
                self.is_connected = False
                break

    def get_status(self) -> dict:
        """UI'ın (WebSocket üzerinden) periyodik olarak sorguladığı, en son bilinen durum."""
        with self._lock:
            return dict(self._last_status)

    def send_command(self, command: dict) -> bool:
        """Tek satır JSON komutu STM32'ye yollar. Bağlantı yoksa False döner."""
        if not self._serial or not self.is_connected:
            return False
        try:
            line = json.dumps(command) + "\n"
            self._serial.write(line.encode("utf-8"))
            return True
        except Exception as e:
            self.last_error = str(e)
            return False


# Tek, paylaşılan köprü nesnesi — main.py bunu import edip kullanır.
bridge = STM32Bridge()
