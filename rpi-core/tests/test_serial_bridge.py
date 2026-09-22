"""
FeedVision — serial_bridge.py testleri (22-09-2026, saha bug düzeltmesi).

Gerçek donanıma dokunmaz — `serial.Serial` sahte nesnelerle (FakeSerial)
değiştirilir. İki odak noktası:
1) `is_connected` artık HESAPLANAN bir durum (_session_active + status_age)
2) `_listen` bozuk satırı sayar/loglar ve beklenmedik hatada thread'i
   kalıcı öldürmek yerine otomatik yeniden bağlanma başlatır.

Not: reconnect testleri gerçek threading kullanıyor (üretim koduyla aynı
yol) ama RECONNECT_BACKOFF_S/RECONNECT_MAX_ATTEMPTS monkeypatch ile
küçültülüyor ki test paketi saniyeler sürmesin.
"""

import threading
import time

import serial_bridge
from serial_bridge import STM32Bridge

# ---------------------------------------------------------------------------
# is_connected — artık hesaplanan bir property (_session_active AND status taze)
# ---------------------------------------------------------------------------


class TestIsConnectedProperty:
    def test_false_when_no_session(self):
        b = STM32Bridge()
        assert b.is_connected is False

    def test_false_when_session_active_but_no_status_yet(self):
        # connect() çağrıldı ama STM32'den henüz hiç durum satırı gelmedi
        b = STM32Bridge()
        b._session_active = True
        assert b.is_connected is False

    def test_true_when_session_active_and_status_fresh(self):
        b = STM32Bridge()
        b._session_active = True
        b._last_status_at = time.time()
        assert b.is_connected is True

    def test_false_when_status_stale(self, monkeypatch):
        # Durum akışı durmuş (kart donmuş/kablo yarı çekilmiş) — session_active
        # hâlâ True olsa da CONNECTION_TIMEOUT_S aşılınca "koptu" sayılmalı.
        monkeypatch.setattr(serial_bridge, "CONNECTION_TIMEOUT_S", 0.1)
        b = STM32Bridge()
        b._session_active = True
        b._last_status_at = time.time() - 0.5
        assert b.is_connected is False

    def test_false_immediately_once_session_marked_inactive(self):
        # Sessiz donma düzeltmesinin özü: _listen thread'i ölür ölmez
        # _session_active anında False olur — status_age'in eşiği aşmasını
        # BEKLEMEZ, tazelik hâlâ yeterli olsa bile.
        b = STM32Bridge()
        b._session_active = False
        b._last_status_at = time.time()
        assert b.is_connected is False


# ---------------------------------------------------------------------------
# get_diagnostics — bozuk satır sayacı + reconnect durumu dışarıya açık
# ---------------------------------------------------------------------------


class TestGetDiagnostics:
    def test_defaults(self):
        b = STM32Bridge()
        diag = b.get_diagnostics()
        assert diag == {"json_decode_errors": 0, "reconnect_attempts": 0, "reconnecting": False}


# ---------------------------------------------------------------------------
# _listen — bozuk JSON satırları sessizce atlanmaz, sayılır + loglanır
# ---------------------------------------------------------------------------


class FakeSerial:
    """Sahte seri port — sırayla verilen satırları döner, kuyruk boşalınca
    kısa bir bekleyle boş satır döner (gerçek pyserial'ın timeout=1 ile
    okuma davranışına benzer)."""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        if self._lines:
            return self._lines.pop(0).encode("utf-8")
        time.sleep(0.01)
        return b""

    def write(self, data):
        pass

    def close(self):
        pass


class TestListenJsonDecodeErrors:
    def test_bad_lines_counted_and_good_line_still_processed(self):
        b = STM32Bridge()
        b._serial = FakeSerial(["not-json", '{"t":1,"e1":0}', "also-bad"])
        b._running = True

        import threading

        t = threading.Thread(target=b._listen, daemon=True)
        t.start()

        deadline = time.time() + 1.0
        while b._json_decode_errors < 2 and time.time() < deadline:
            time.sleep(0.01)
        b._running = False
        t.join(timeout=1.0)

        assert b._json_decode_errors == 2
        assert b.get_status().get("t") == 1


# ---------------------------------------------------------------------------
# _listen — beklenmedik hata: thread kalıcı ölmez, otomatik yeniden bağlanır
# ---------------------------------------------------------------------------


class DyingSerial:
    """readline() her çağrıda gerçek bir donanım hatasını simüle eder
    (ör. USB kablosu çekildiğinde pyserial'ın fırlattığı SerialException)."""

    def readline(self):
        raise OSError("cihaz kayboldu")

    def close(self):
        pass


class TestUnexpectedErrorTriggersReconnect:
    def test_session_marked_inactive_immediately(self, monkeypatch):
        monkeypatch.setattr(serial_bridge, "RECONNECT_BACKOFF_S", 0.01)
        monkeypatch.setattr(serial_bridge, "RECONNECT_MAX_ATTEMPTS", 1)
        # Tüm yeniden bağlanma denemeleri de başarısız olsun (port hâlâ yok).
        monkeypatch.setattr(
            serial_bridge.serial, "Serial",
            lambda *a, **k: (_ for _ in ()).throw(OSError("port yok")),
        )

        b = STM32Bridge()
        b._port = "/dev/fake"
        b._serial = DyingSerial()
        b._running = True
        b._session_active = True

        b._listen()  # senkron çağrı — readline() hemen patlar, break'e düşer

        assert b._session_active is False
        assert b.is_connected is False

        # Bu hata bir arka plan reconnect thread'i başlattı (RECONNECT_MAX_ATTEMPTS=1) -
        # test bitmeden tamamlanmasını bekle. Aksi halde thread, bu testin
        # monkeypatch'i geri alındıktan SONRA hâlâ sleep'ten uyanıp modül-seviyeli
        # serial.Serial'i çağırabilir — o an sırada çalışan bir SONRAKİ testin
        # kendi monkeypatch'ine denk gelip onun sayaçlarını bozar (gerçek bir kod
        # hatası değil, salt test izolasyonu meselesi).
        deadline = time.time() + 1.0
        while b._reconnecting and time.time() < deadline:
            time.sleep(0.01)

    def test_gives_up_after_max_attempts_and_sets_last_error(self, monkeypatch):
        monkeypatch.setattr(serial_bridge, "RECONNECT_BACKOFF_S", 0.01)
        monkeypatch.setattr(serial_bridge, "RECONNECT_MAX_ATTEMPTS", 2)
        attempts = []

        def fake_serial_ctor(*a, **k):
            attempts.append(a)
            raise OSError("port hala yok")

        monkeypatch.setattr(serial_bridge.serial, "Serial", fake_serial_ctor)

        b = STM32Bridge()
        b._port = "/dev/fake"
        b._serial = DyingSerial()
        b._running = True
        b._session_active = True

        b._listen()

        deadline = time.time() + 2.0
        while b._reconnecting and time.time() < deadline:
            time.sleep(0.01)

        assert len(attempts) == 2  # RECONNECT_MAX_ATTEMPTS kadar denendi, sonra pes edildi
        assert b._reconnect_attempts == 2
        assert "elle" in b.last_error.lower()
        assert b.is_connected is False

    def test_intentional_disconnect_does_not_trigger_reconnect(self, monkeypatch):
        monkeypatch.setattr(serial_bridge, "RECONNECT_BACKOFF_S", 0.01)
        called = []
        monkeypatch.setattr(
            serial_bridge.serial, "Serial",
            lambda *a, **k: called.append(1) or DyingSerial(),
        )

        b = STM32Bridge()
        b._port = "/dev/fake"
        b._serial = DyingSerial()
        b._running = True
        b._session_active = True
        b._intentional_disconnect = True  # disconnect() az önce çağrılmış gibi

        b._listen()
        time.sleep(0.1)  # bir reconnect thread'i yanlışlıkla başlamışsa yakalansın diye kısa bekleme

        assert b._reconnecting is False
        assert not called  # serial.Serial() hiç çağrılmamalı — yeniden bağlanma denenmedi


class SlowThenDataSerial:
    """İlk bir süre boş döner (gerçek pyserial'ın timeout'lu okumasına
    benzer), SONRA bir durum satırı verir — `_connect_once`'un handshake
    bekleme penceresinde henüz veri gelmemişken test kodunun disconnect()
    çağırabilmesi için kasıtlı bir gecikme sağlar."""

    def __init__(self, delay_before_data_s: float):
        self._deadline = time.time() + delay_before_data_s
        self._sent = False

    def readline(self):
        if not self._sent and time.time() >= self._deadline:
            self._sent = True
            return b'{"t":1,"e1":0}\n'
        time.sleep(0.02)
        return b""

    def write(self, data):
        pass

    def close(self):
        pass


class TestDisconnectDuringHandshake:
    def test_disconnect_mid_handshake_wins_even_if_data_arrives_after(self, monkeypatch):
        # Bulgu (azobex-software-tester, 22-09-2026, tekrar üretilebilir):
        # _connect_once HANDSHAKE_TIMEOUT_S kadar (varsayılan 2sn) STM32'den
        # veri bekliyor. Bu pencere içinde disconnect() çağrılırsa, veri o
        # sırada gerçekten gelmiş olsa bile bağlantı "kuruldu" SAYILMAMALI —
        # operatörün disconnect kararı kazanmalı. Düzeltmeden önce bu test
        # 10/10 denemede FAIL ediyordu (bağlantı sessizce "diriliyordu").
        monkeypatch.setattr(serial_bridge, "HANDSHAKE_TIMEOUT_S", 2.0)
        monkeypatch.setattr(
            serial_bridge.serial, "Serial",
            lambda *a, **k: SlowThenDataSerial(delay_before_data_s=0.15),
        )

        b = STM32Bridge()
        result_holder: dict = {}

        def run_connect():
            result_holder["ok"] = b._connect_once("/dev/fake")

        t = threading.Thread(target=run_connect)
        t.start()
        time.sleep(0.03)  # handshake bekleme döngüsü başlasın, veri henüz gelmedi
        b.disconnect()
        t.join(timeout=2.0)

        assert result_holder["ok"] is False
        assert b.is_connected is False
        assert b._session_active is False


class OkSerial:
    """Bir kez bozulup sonra düzgün çalışan port — reconnect'in başarılı
    olduğu senaryoyu simüle eder."""

    def __init__(self):
        self._lines = ['{"t":1,"e1":0}']

    def readline(self):
        if self._lines:
            return self._lines.pop(0).encode("utf-8")
        time.sleep(0.01)
        return b""

    def write(self, data):
        pass

    def close(self):
        pass


class TestReconnectSuccess:
    def test_recovers_and_resets_counters(self, monkeypatch):
        monkeypatch.setattr(serial_bridge, "RECONNECT_BACKOFF_S", 0.01)
        monkeypatch.setattr(serial_bridge, "RECONNECT_MAX_ATTEMPTS", 5)
        monkeypatch.setattr(serial_bridge, "HANDSHAKE_TIMEOUT_S", 1.0)
        attempts = {"n": 0}

        def fake_serial_ctor(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise OSError("gecici hata")
            return OkSerial()

        monkeypatch.setattr(serial_bridge.serial, "Serial", fake_serial_ctor)

        b = STM32Bridge()
        b._port = "/dev/fake"
        b._serial = DyingSerial()
        b._running = True
        b._session_active = True

        b._listen()  # ilk hata — reconnect thread'i tetikler

        deadline = time.time() + 2.0
        while b._reconnecting and time.time() < deadline:
            time.sleep(0.01)

        assert attempts["n"] == 2  # ilk deneme başarısız, ikincisi başarılı
        assert b._session_active is True
        assert b.is_connected is True
        assert b._reconnect_attempts == 0  # başarılı olunca sıfırlanır

        b._running = False  # arka planda kalan yeni _listen thread'ini durdur
