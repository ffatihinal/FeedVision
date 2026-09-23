"""
FeedVision RPi Core — STM32 seri port köprüsü.

STM32 firmware'i (feedvision_test, bkz. /firmware) ile satır bazlı JSON
protokolü konuşur — protokol tanımı: /docs/protocol.md.

Nasıl çalışır: bağlantı kurulunca ayrı bir thread arka planda sürekli satır
okur (STM32 saniyede ~20 kez durum yolluyor), en son geleni bellekte tutar.
Komut gönderme ("step at", "dur" vb.) ana thread'den çağrılır, doğrudan
seri porta yazar. İki taraf ayrı thread olduğu için `_lock` ile korunuyor.

`is_connected` HESAPLANAN bir durumdur (22-09-2026 SE kararı, saha bug'ı
düzeltmesi) — "yapışkan" bir bayrak değil. Ayrıca okuma thread'i beklenmedik
bir hatayla ölürse (USB koptu, kart resetlendi vb.) otomatik yeniden bağlanma
dener (bkz. STM32Bridge.is_connected ve _reconnect_loop docstring'leri).
"""

import json
import logging
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

# is_connected'ın "hâlâ canlı mı" kararı bu eşiğe göre verilir: son durum
# satırının üzerinden bu kadar süre geçtiyse bağlantı "koptu" sayılır. STM32
# ~20Hz (50ms periyot, docs/protocol.md) yayın yapıyor — 500ms bu periyodun
# ~10 katı, USB/OS jitter'ı için makul bir pay bırakıyor. Ekstra bir
# ping/pong protokolüne gerek yok, zaten var olan durum akışı "canlılık"
# sinyali olarak kullanılıyor (22-09-2026 SE kararı).
CONNECTION_TIMEOUT_S = 0.5

# Okuma thread'i beklenmedik bir hatayla öldüğünde otomatik yeniden bağlanma
# parametreleri. Sabit (exponential değil) backoff — USB seri bağlantısı ya
# hızlıca toparlanır ya da port kalıcı olarak gitmiştir, aradaki karmaşıklığa
# gerek yok. Sonsuz/agresif retry loop'a GİRİLMEZ — RECONNECT_MAX_ATTEMPTS
# denemeden sonra pes edilir, operatör elle "Bağlan" yapmalı (22-09-2026 SE kararı).
RECONNECT_BACKOFF_S = 3.0
RECONNECT_MAX_ATTEMPTS = 5

_logger = logging.getLogger("feedvision.serial_bridge")


class STM32Bridge:
    def __init__(self):
        self._serial: Optional[serial.Serial] = None
        self._port: Optional[str] = None  # son bağlanılan port — otomatik yeniden bağlanma bunu kullanır
        self._lock = threading.Lock()
        self._cmd_lock = threading.Lock()  # tek seferde tek komut+yanıt döngüsü (yarış durumunu önler)
        self._last_status: dict = {}
        self._last_status_at: Optional[float] = None  # time.time() — Kontrol Kriterleri'nin
        # "bağlantı koptu/timeout" kriteri icin (bkz. get_status_age()); hic
        # durum satiri gelmediyse None kalir.
        self._last_reply: Optional[dict] = None
        self._last_reply_raw: Optional[str] = None
        self._reply_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._session_active = False  # connect() başarıyla tamamlanınca True; disconnect() ya da thread ölümünde ANINDA False
        self._intentional_disconnect = False  # disconnect() bilerek çağrıldıysa True — _listen bunu görünce yeniden bağlanmaya kalkışmaz
        self._json_decode_errors = 0  # bozuk/parse edilemeyen satır sayacı — sıklığı görünür kılmak için (get_diagnostics())
        self._reconnect_attempts = 0
        self._reconnecting = False
        self.last_error: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        """Hesaplanan bağlantı durumu — iki koşulun AND'i:
        1) `_session_active`: connect() başarıyla tamamlandı, disconnect()
           çağrılmadı ve okuma thread'i beklenmedik şekilde ölmedi.
        2) `get_status_age() < CONNECTION_TIMEOUT_S`: STM32'nin periyodik
           (~20Hz) durum akışı hâlâ TAZE. Akış dururca (kablo çekilmesi,
           kart donması/resetlenmesi, sessizce atlanan bozuk satırlar) bu
           otomatik False'a döner — ayrı bir ping/pong protokolü icat
           etmeye gerek yok, zaten var olan durum akışı "canlılık" sinyali.

        NOT: rule_engine.py'nin "stm" kriteri bu property'den BAĞIMSIZ —
        kendi ayrı timeout_s'i var (admin'den girilir). Sadece ölçüm kaynağı
        (get_status_age()) ortak, eşik/tüketici ayrı kalır.
        """
        if not self._session_active:
            return False
        age = self.get_status_age()
        return age is not None and age < CONNECTION_TIMEOUT_S

    def list_available_ports(self) -> list[str]:
        """Mac/Pi'de o an takılı olan seri portların listesi (kullanıcı UI'dan seçsin diye)."""
        return [p.device for p in list_ports.comports()]

    def connect(self, port: str) -> bool:
        """Operatör/UI'nin elle "Bağlan" çağrısı. Yeni bir oturum başlatır —
        önceki bir otomatik yeniden bağlanma denemesi sürüyorsa sayaçlarını
        sıfırlar (bu artık kullanıcının bilerek başlattığı taze bir deneme)."""
        self._reconnect_attempts = 0
        self._reconnecting = False
        return self._connect_once(port)

    def _connect_once(self, port: str) -> bool:
        """Asıl bağlanma mantığı — hem elle "Bağlan" (connect()) hem otomatik
        yeniden bağlanma (_reconnect_loop()) bunu çağırır.

        DİKKAT: Port dosyasının açılabilmesi (macOS'ta sanal/hata ayıklama
        portları dahil çoğu port için kolayca başarılı olur) STM32'nin
        gerçekten orada olduğu anlamına GELMEZ. Bu yüzden port açıldıktan
        sonra STM32'nin kendiliğinden gönderdiği durum satırını (saniyede
        ~20 kez) kısa bir süre bekliyoruz — HANDSHAKE_TIMEOUT_S içinde
        geçerli bir JSON satırı gelmezse "bağlı değil" diyoruz ve portu
        kapatıyoruz, yanlış pozitif "Bağlı" göstermeyelim diye."""
        self._intentional_disconnect = False
        with self._lock:
            self._last_status = {}
            self._last_status_at = None
        try:
            self._serial = serial.Serial(port, BAUD, timeout=1)
        except Exception as e:
            self._session_active = False
            self.last_error = str(e)
            return False

        self._port = port
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

        # STM32'den gerçek veri gelene kadar (ya da zaman aşımına kadar) bekle.
        # DİKKAT (22-09-2026 test bulgusu düzeltmesi): bu bekleme HANDSHAKE_TIMEOUT_S
        # (2sn) sürebiliyor — bu pencere içinde operatör/otomatik akış disconnect()
        # çağırırsa (`_intentional_disconnect` True olur), veri o sırada gerçekten
        # gelmiş olsa bile bunu "bağlandı" SAYMIYORUZ. Aksi halde operatörün az önce
        # verdiği "bağlantıyı kes" kararı sessizce geri alınırdı (tekrar üretilen
        # gerçek bug, azobex-software-tester bulgusu).
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._intentional_disconnect:
                break
            with self._lock:
                got_data = bool(self._last_status)
            if got_data:
                self._session_active = True
                self.last_error = None
                # Karta bir "ping" gönder — bu, kartın host_confirmed
                # bayrağını set edip LED'i yavaştan hızlıya geçirir. Sahada
                # ekrana bakmadan da "gerçekten bağlı" görsel teyidi verir.
                self.send_command({"cmd": "ping"})
                return True
            time.sleep(0.05)

        # Buraya iki yoldan gelinir: zaman aşımı (port açıldı ama karşı taraf
        # STM32 değil/cevap vermiyor) YA DA disconnect() ile yarıda kesildi.
        # `_serial`'i güvenle kapat — _listen thread'i de aynı anda kapatmaya
        # çalışıyor olabilir (disconnect() zaten kapatmış olabilir), bu yüzden
        # referansı önce yerel değişkene alıp iki kez kapatma/None referansı
        # riskini önlüyoruz.
        self._running = False
        self._session_active = False
        serial_ref, self._serial = self._serial, None
        if serial_ref is not None:
            try:
                serial_ref.close()
            except Exception:
                pass
        if not self._intentional_disconnect:
            self.last_error = "Port açıldı ama STM32'den veri gelmedi (yanlış port ya da kart bağlı değil)"
        return False

    def disconnect(self):
        """Operatör bilerek bağlantıyı kapatıyor. Önce `_intentional_disconnect`
        bayrağını kaldırıyoruz ki _listen bu kapanışı (readline() sırasında
        portun kapanmasından doğabilecek bir istisnayı) "beklenmedik kopma"
        sanıp otomatik yeniden bağlanmaya kalkışmasın."""
        self._try_send_bye()
        self._intentional_disconnect = True
        self._running = False
        self._session_active = False
        if self._serial:
            self._serial.close()

    def _try_send_bye(self):
        """Portu kapatmadan ÖNCE STM32'ye {"cmd":"bye"} göndermeyi dener —
        firmware bunu alınca LED'i 'bağlı değil' durumuna döndürür (bkz.
        firmware bye handler'ı, commit edb56ae). Bu bir iyi niyet gönderimi:
        port zaten bozuksa/seri hata verirse sessizce yutulur, disconnect()
        her koşulda tamamlanmalı (23-09-2026 saha bug'ı — bye hiç
        gönderilmiyordu, LED hızlı yanıp sönmeye takılı kalıyordu)."""
        if not self._serial or not self._session_active:
            return
        try:
            self.send_command({"cmd": "bye"})
        except Exception:
            _logger.debug("bye komutu gönderilemedi, disconnect yine de devam ediyor", exc_info=True)

    def _listen(self):
        """Arka planda sürekli satır okur. STM32 iki tür satır gönderiyor:
        - periyodik DURUM satırı (t/e1/e2/... alanları) — _last_status'a yazılır
        - bir komuta doğrudan YANIT satırı ({"ok":...} veya {"err":...}) —
          _last_reply'e yazılır ve _reply_event tetiklenir.
        Bunları ayırmazsak (eskiden ikisi de aynı 'last_status' alanına
        yazılıyordu), yanıt satırı ~50ms içinde bir sonraki durum satırıyla
        ezilip UI'a hiç ulaşamıyordu — bir komut reddedilse (err) bile
        kullanıcı bunu göremiyordu.

        Bozuk/eksik bir satır gelirse (JSONDecodeError) atlanır — çökmez,
        ama sessizce de değil: `_json_decode_errors` sayaçlanır ve sıklık
        (her 20 hatada bir) log'a düşer, `get_diagnostics()` ile dışarıya
        açılır (22-09-2026 düzeltmesi — önceden bu satırlar hiçbir iz
        bırakmadan atlanıyordu).

        Beklenmedik bir Exception (ör. USB kablosu çekildi, OS seri portu
        kapattı) alınırsa artık thread'i kalıcı olarak öldürüp bırakmıyoruz —
        `_session_active` anında False'a çekilir (is_connected hemen
        yansıtır) ve otomatik yeniden bağlanma denemesi başlatılır (bkz.
        _start_reconnect_thread), TABİİ bu kopuş operatörün kendi disconnect()
        çağrısından kaynaklanmıyorsa (`_intentional_disconnect`)."""
        while self._running and self._serial:
            try:
                raw = self._serial.readline().decode("utf-8", errors="ignore").strip()
                if not raw:
                    continue
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._json_decode_errors += 1
                if self._json_decode_errors % 20 == 1:  # her satırda değil, sıklığı belli etsin diye periyodik uyarı
                    _logger.warning(
                        "Bozuk/parse edilemeyen seri satır (bu oturumda toplam %d kez, port=%s)",
                        self._json_decode_errors, self._port,
                    )
                continue
            except Exception as e:
                self.last_error = str(e)
                self._session_active = False
                self._close_serial_safely()
                if self._intentional_disconnect:
                    # Operatör zaten disconnect() çağırdı — beklenen bir
                    # kopma, yeniden bağlanmaya kalkışma.
                    break
                _logger.warning("Seri port okuma hatası, bağlantı koptu: %s (port=%s)", e, self._port)
                self._start_reconnect_thread()
                break

            if "ok" in data or "err" in data:
                with self._lock:
                    self._last_reply = data
                    self._last_reply_raw = raw
                self._reply_event.set()
            else:
                with self._lock:
                    self._last_status = data
                    self._last_status_at = time.time()

    def _close_serial_safely(self):
        """`_listen` beklenmedik bir hatadan çıkarken portu güvenle kapatır —
        kapatma sırasında ikinci bir istisna çıkarsa (port zaten koptuysa)
        onu da yutar, thread'in temiz sonlanmasını engellemesin diye."""
        self._running = False
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def _start_reconnect_thread(self):
        """`_listen` thread'i beklenmedik bir hatayla öldüğünde çağrılır (USB
        kablosu çekilmesi, kart resetlenmesi, OS seri port hatası vb.). Ayrı
        bir arka plan thread'inde (bu şekilde çağıran _listen thread'i hemen
        sonlanabilir) birkaç kez, aralarla yeniden bağlanmayı dener. Aynı
        anda birden fazla reconnect thread'i başlamasın diye `_reconnecting`
        bayrağıyla korunuyor."""
        if self._reconnecting or not self._port:
            return
        self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self):
        """RECONNECT_BACKOFF_S (3sn) bekleyip `_connect_once` ile tekrar
        dener; en fazla RECONNECT_MAX_ATTEMPTS (5) kez. Başarılı olursa
        sayaçları sıfırlayıp çıkar. Tüm denemeler tükenirse pes eder ve
        `last_error`'a yazar — UI zaten last_error'ı gösteriyor, operatör
        elle "Bağlan" yapmak zorunda kalır. Operatör bu sırada bilerek
        disconnect() çağırırsa (`_intentional_disconnect`) ya da başka bir
        yoldan bağlantı zaten kurulduysa (`_session_active`) döngü sessizce
        durur."""
        port = self._port
        while self._reconnect_attempts < RECONNECT_MAX_ATTEMPTS and not self._intentional_disconnect:
            if self._session_active:
                break  # başka bir yoldan (ör. operatör elle "Bağlan" dedi) zaten bağlandı
            self._reconnect_attempts += 1
            attempt = self._reconnect_attempts
            msg = f"Bağlantı koptu, yeniden deneniyor ({attempt}/{RECONNECT_MAX_ATTEMPTS}), {RECONNECT_BACKOFF_S:.0f} sn sonra..."
            _logger.warning("%s (port=%s)", msg, port)
            self.last_error = msg
            time.sleep(RECONNECT_BACKOFF_S)
            if self._intentional_disconnect or self._session_active:
                break
            if self._connect_once(port):
                _logger.info("Yeniden bağlanma başarılı (%d. denemede, port=%s)", attempt, port)
                self._reconnect_attempts = 0
                self._reconnecting = False
                return

        # DİKKAT (22-09-2026 test bulgusu düzeltmesi): last_error'ı `_reconnecting`
        # False olmadan ÖNCE güncelliyoruz. Sıra tersi olsaydı, `_reconnecting`i
        # "işlem bitti" sinyali sayan bir gözlemci (ör. testler, ileride UI) bu
        # ikisi arasındaki mikro-pencerede last_error'ın hâlâ eski/ara değerde
        # olduğunu görebilirdi (tekrar üretilen gerçek yarış durumu).
        give_up = (
            not self._intentional_disconnect
            and not self._session_active
            and self._reconnect_attempts >= RECONNECT_MAX_ATTEMPTS
        )
        if give_up:
            final_msg = f"Bağlantı {RECONNECT_MAX_ATTEMPTS} denemede kurulamadı, pes edildi — elle 'Bağlan' gerekiyor."
            _logger.error("%s (port=%s)", final_msg, port)
            self.last_error = final_msg
        self._reconnecting = False

    def get_status(self) -> dict:
        """UI'ın (WebSocket üzerinden) periyodik olarak sorguladığı, en son bilinen durum.
        Artık SADECE gerçek durum satırlarını içeriyor — ok/err yanıtları karışmıyor."""
        with self._lock:
            return dict(self._last_status)

    def get_status_age(self) -> Optional[float]:
        """Son gerçek durum satırının üzerinden kaç saniye geçtiğini döner.

        Hiç durum satırı gelmediyse (bağlantı hiç kurulmadı/yeni açıldı)
        None döner — Kontrol Kriterleri'nin "bağlantı koptu/timeout"
        kriteri bunu 'henüz değerlendirilemez' (violation değil, skipped)
        olarak ele alır (bkz. rule_engine.py). Aynı ölçüm, `is_connected`
        property'sinin CONNECTION_TIMEOUT_S ile kıyasladığı kaynak — ama
        eşik/tüketici birbirinden bağımsız (bkz. is_connected docstring)."""
        with self._lock:
            last_at = self._last_status_at
        if last_at is None:
            return None
        return time.time() - last_at

    def get_diagnostics(self) -> dict:
        """UI/log görünürlüğü için bridge'in kendi tanılama bilgisi —
        STM32'nin get_status()'ta dönen protokol alanlarıyla KARIŞMASIN
        diye ayrı bir metod (bunlar STM32'nin değil, bridge'in kendi
        durumu)."""
        return {
            "json_decode_errors": self._json_decode_errors,
            "reconnect_attempts": self._reconnect_attempts,
            "reconnecting": self._reconnecting,
        }

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
