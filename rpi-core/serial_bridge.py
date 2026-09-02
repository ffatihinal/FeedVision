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
from typing import Optional

import serial
from serial.tools import list_ports

BAUD = 115200


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
        """Belirtilen porta bağlanmayı dener. Başarılıysa arka plan dinleme
        thread'ini başlatır. Başarısızsa (kart takılı değil, port meşgul vb.)
        False döner, hatayı `last_error`'da bırakır — çökmez."""
        try:
            self._serial = serial.Serial(port, BAUD, timeout=1)
            self.is_connected = True
            self.last_error = None
            self._running = True
            self._thread = threading.Thread(target=self._listen, daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            self.is_connected = False
            self.last_error = str(e)
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
