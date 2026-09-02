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
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._son_durum: dict = {}
        self._calisiyor = False
        self._thread: Optional[threading.Thread] = None
        self.baglantida_mi = False
        self.son_hata: Optional[str] = None

    def bagli_portlari_listele(self) -> list[str]:
        """Mac/Pi'de o an takılı olan seri portların listesi (kullanıcı UI'dan seçsin diye)."""
        return [p.device for p in list_ports.comports()]

    def baglan(self, port: str) -> bool:
        """Belirtilen porta bağlanmayı dener. Başarılıysa arka plan dinleme
        thread'ini başlatır. Başarısızsa (kart takılı değil, port meşgul vb.)
        False döner, hatayı `son_hata`'da bırakır — çökmez."""
        try:
            self._ser = serial.Serial(port, BAUD, timeout=1)
            self.baglantida_mi = True
            self.son_hata = None
            self._calisiyor = True
            self._thread = threading.Thread(target=self._dinle, daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            self.baglantida_mi = False
            self.son_hata = str(e)
            return False

    def kapat(self):
        self._calisiyor = False
        if self._ser:
            self._ser.close()
        self.baglantida_mi = False

    def _dinle(self):
        """Arka planda sürekli satır okur, JSON'a çevirip bellekte tutar.
        Bozuk/eksik bir satır gelirse (kablo gürültüsü vb.) atlar, çökmez."""
        while self._calisiyor and self._ser:
            try:
                ham = self._ser.readline().decode("utf-8", errors="ignore").strip()
                if not ham:
                    continue
                veri = json.loads(ham)
                with self._lock:
                    self._son_durum = veri
            except json.JSONDecodeError:
                continue
            except Exception as e:
                self.son_hata = str(e)
                self.baglantida_mi = False
                break

    def son_durum(self) -> dict:
        """UI'ın (WebSocket üzerinden) periyodik olarak sorguladığı, en son bilinen durum."""
        with self._lock:
            return dict(self._son_durum)

    def komut_gonder(self, komut: dict) -> bool:
        """Tek satır JSON komutu STM32'ye yollar. Bağlantı yoksa False döner."""
        if not self._ser or not self.baglantida_mi:
            return False
        try:
            satir = json.dumps(komut) + "\n"
            self._ser.write(satir.encode("utf-8"))
            return True
        except Exception as e:
            self.son_hata = str(e)
            return False


# Tek, paylaşılan köprü nesnesi — main.py bunu import edip kullanır.
bridge = STM32Bridge()
