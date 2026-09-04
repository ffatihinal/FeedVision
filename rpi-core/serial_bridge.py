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
        self._cmd_lock = threading.Lock()  # tek seferde tek komut+yanıt döngüsü (yarış durumunu önler)
        self._last_status: dict = {}
        self._last_reply: Optional[dict] = None
        self._last_reply_raw: Optional[str] = None
        self._reply_event = threading.Event()
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
                # Karta bir "ping" gönder — bu, kartın host_confirmed
                # bayrağını set edip LED'i yavaştan hızlıya geçirir. Sahada
                # ekrana bakmadan da "gerçekten bağlı" görsel teyidi verir.
                self.send_command({"cmd": "ping"})
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
        """Arka planda sürekli satır okur. STM32 iki tür satır gönderiyor:
        - periyodik DURUM satırı (t/e1/e2/... alanları) — _last_status'a yazılır
        - bir komuta doğrudan YANIT satırı ({"ok":...} veya {"err":...}) —
          _last_reply'e yazılır ve _reply_event tetiklenir.
        Bunları ayırmazsak (eskiden ikisi de aynı 'last_status' alanına
        yazılıyordu), yanıt satırı ~50ms içinde bir sonraki durum satırıyla
        ezilip UI'a hiç ulaşamıyordu — bir komut reddedilse (err) bile
        kullanıcı bunu göremiyordu. Bozuk/eksik bir satır gelirse atlar, çökmez."""
        while self._running and self._serial:
            try:
                raw = self._serial.readline().decode("utf-8", errors="ignore").strip()
                if not raw:
                    continue
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            except Exception as e:
                self.last_error = str(e)
                self.is_connected = False
                break

            if "ok" in data or "err" in data:
                with self._lock:
                    self._last_reply = data
                    self._last_reply_raw = raw
                self._reply_event.set()
            else:
                with self._lock:
                    self._last_status = data

    def get_status(self) -> dict:
        """UI'ın (WebSocket üzerinden) periyodik olarak sorguladığı, en son bilinen durum.
        Artık SADECE gerçek durum satırlarını içeriyor — ok/err yanıtları karışmıyor."""
        with self._lock:
            return dict(self._last_status)

    def send_command(self, command: dict, reply_timeout: float = 0.3) -> dict:
        """Tek satır JSON komutu STM32'ye yollar ve kartın {"ok":...}/{"err":...}
        yanıtını kısa süre bekler. UI'ın hem giden komutu hem gelen yanıtı ham
        haliyle gösterebilmesi için ikisini de döndürür.

        Dönen sözlük:
          sent         — komut seri porta yazılabildi mi
          raw_command  — kartına gönderilen tam satır (JSON metni)
          command      — aynı komut, sözlük olarak
          raw_reply    — karttan gelen ham yanıt satırı (varsa)
          reply        — aynı yanıt, sözlük olarak (varsa)
          timed_out    — reply_timeout içinde hiç yanıt gelmediyse True
        """
        result: dict = {
            "sent": False, "raw_command": None, "command": command,
            "raw_reply": None, "reply": None, "timed_out": False,
        }
        if not self._serial or not self.is_connected:
            return result

        with self._cmd_lock:  # aynı anda 2 komut birbirinin yanıtını çalmasın
            line = json.dumps(command) + "\n"
            result["raw_command"] = line.strip()
            try:
                self._reply_event.clear()
                self._serial.write(line.encode("utf-8"))
                result["sent"] = True
            except Exception as e:
                self.last_error = str(e)
                return result

            if self._reply_event.wait(reply_timeout):
                with self._lock:
                    result["reply"] = self._last_reply
                    result["raw_reply"] = self._last_reply_raw
            else:
                result["timed_out"] = True

        return result


# Tek, paylaşılan köprü nesnesi — main.py bunu import edip kullanır.
bridge = STM32Bridge()
