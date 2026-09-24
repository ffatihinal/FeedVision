"""
FeedVision — /motor/feed-start orkestrasyon mantığı testleri (main.py).

Diğer test dosyalarından FARKLI olarak burada `main.py`'yi doğrudan import
edip endpoint fonksiyonunu (motor_feed_start) FastAPI/HTTP katmanı OLMADAN
çağırıyoruz — düz bir Python fonksiyonu olduğu için TestClient/uvicorn'a
gerek yok, `import main` de lifespan'i (kamera/arka plan görevleri) TETİKLEMEZ
(sadece ASGI sunucusu lifespan'i çalıştırır). Gerçek donanımı (bridge) ve
gerçek dosya deposunu (motion_params) FAKE/izole sürümlerle değiştiriyoruz.

Odak: "her ikisi de (step+DC dönüşümü) hardware'a DOKUNULMADAN önce
doğrulanır", "step başarısızsa DC hiç gönderilmez", "yeni çağrı önceki
watcher'ı iptal eder" gibi main.py'ye özgü DALLANMA mantığı — saf birim
dönüşüm formülleri zaten test_motion_calc.py'de kapsanıyor, burada tekrar
edilmiyor.

İSTİSNA (24-09-2026 saha bugı): dosya sonundaki TestFeedStartRealHttpDispatch
sınıfı BİLEREK yukarıdaki desenden sapıyor ve gerçek fastapi.testclient.
TestClient kullanıyor — main.motor_feed_start(cmd)'i DOĞRUDAN çağırmak zaten
çalışan bir event loop'un (asyncio.run) İÇİNDE yapılıyor, bu da gerçek
üretimdeki "senkron endpoint FastAPI'nin thread pool'unda, event loop'suz bir
thread'de çalışır" durumunu SİMÜLE ETMİYOR — tam da bu yüzden
asyncio.create_task'in orada RuntimeError fırlattığı bug 23-09'dan beri hiç
yakalanamadı. TestClient gerçek ASGI thread pool dispatch'ini kullanır.
"""

import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


class FakeBridge:
    """serial_bridge.STM32Bridge'in gerçek donanım/seri port'a hiç
    dokunmayan, davranışı testte kontrol edilebilen sahte sürümü.

    is_connected/last_error/get_status_age: gerçek STM32Bridge'in arayüzünden
    (bkz. serial_bridge.py) — bu dosyanın alt kısmındaki TestFeedStartRealHttpDispatch
    gerçek main.app'i lifespan dahil ayağa kaldırdığı için main.py'deki
    arka plan _journal_loop'u da (koşulsuz) bridge.is_connected okuyor;
    bu alanlar olmadan o döngü her turda AttributeError'a düşüp (yakalanıp
    loglanıyor, testi BOZMUYOR ama gürültü/yanlış temsil) sessizce yutuluyordu."""

    def __init__(self, step_ok: bool = True):
        self.sent_commands: list[dict] = []
        self.step_ok = step_ok
        self._status = {"running": 0}
        self.is_connected = True
        self.last_error: str | None = None

    def get_status_age(self) -> float:
        return 0.1

    def send_command(self, command: dict) -> dict:
        self.sent_commands.append(command)
        if command.get("cmd") == "step" and not self.step_ok:
            return {"sent": False, "raw_command": None, "command": command, "raw_reply": None, "reply": None, "timed_out": False}
        return {
            "sent": True,
            "raw_command": "...",
            "command": command,
            "raw_reply": '{"ok":true}',
            "reply": {"ok": True},
            "timed_out": False,
        }

    def get_status(self) -> dict:
        return dict(self._status)


@pytest.fixture(autouse=True)
def _reset_sync_watcher_task():
    """Modül seviyesindeki _sync_watcher_task testler arası sızmasın diye
    her testten önce/sonra None'a sıfırlanır (main.py'deki gerçek global)."""
    main._sync_watcher_task = None
    yield
    main._sync_watcher_task = None


@pytest.fixture(autouse=True)
def _reset_main_event_loop():
    """_main_event_loop normalde SADECE lifespan() içinde set edilir — bu
    dosyadaki testler main.motor_feed_start()'ı lifespan'i hiç tetiklemeden
    DOĞRUDAN çağırıyor (bkz. dosya başı docstring), bu yüzden watcher spawn
    koduna ulaşan testler kendi çalışan loop'larını burada elle set eder.
    Testler arası sızmasın diye her testten önce/sonra None'a sıfırlanır."""
    main._main_event_loop = None
    yield
    main._main_event_loop = None


@pytest.fixture
def blocking_watcher(monkeypatch):
    """Gerçek _sync_watcher (bridge polling) yerine, sadece cancel()
    edilene kadar askıda kalan sahte bir watcher — motor_feed_start'ın
    watcher'ı DOĞRU parametreyle spawn ettiğini ve bir önceki watcher'ı
    gerçekten cancel() ettiğini test edebilmek için (gerçek polling'in
    zamanlamasına bağımlı olmadan, deterministik)."""
    calls: list[float] = []

    async def fake_watcher(max_wait_s: float):
        calls.append(max_wait_s)
        await asyncio.Event().wait()  # sadece cancel() ile sonlanır

    monkeypatch.setattr(main, "_sync_watcher", fake_watcher)
    return calls


def _valid_command(**overrides) -> "main.FeedStartCommand":
    defaults = dict(dir=1, speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, dc_dir="forward", rpm=10.0)
    defaults.update(overrides)
    return main.FeedStartCommand(**defaults)


class TestValidationBeforeHardwareTouch:
    """Dönüşüm hataları HİÇBİR bridge.send_command çağrısından ÖNCE
    yakalanmalı — motorun yarım/anlamsız bir komutla harekete geçmesi
    istenmiyor (bkz. motor_feed_start docstring'i)."""

    def test_invalid_dc_dir_rejected_without_touching_hardware(self, isolated_motion_params_config, monkeypatch):
        fake_bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command(dc_dir="stop")
        with pytest.raises(HTTPException) as exc_info:
            main.motor_feed_start(cmd)
        assert exc_info.value.status_code == 400
        assert fake_bridge.sent_commands == []

    def test_step_out_of_range_speed_rejected_without_touching_hardware(self, isolated_motion_params_config, monkeypatch):
        fake_bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command(speed_mms=1000.0)  # D=52mm ile desteklenen aralığın çok üstü
        with pytest.raises(HTTPException) as exc_info:
            main.motor_feed_start(cmd)
        assert exc_info.value.status_code == 400
        assert "Step dönüşüm hatası" in exc_info.value.detail
        assert fake_bridge.sent_commands == []

    def test_rpm_out_of_range_rejected_without_touching_hardware(self, isolated_motion_params_config, monkeypatch):
        # Step parametreleri GEÇERLİ ama RPM aralık dışı — kritik test: step
        # parametreleri tek başına valid olsa da, DC dönüşümü geçersizse
        # step KOMUTU DA gönderilmemeli (motor_feed_start'ın "ikisini de önce
        # doğrula" tasarım kararı).
        fake_bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command(rpm=525.0)  # varsayılan parametrelerle ulaşılamayacak kadar yüksek
        with pytest.raises(HTTPException) as exc_info:
            main.motor_feed_start(cmd)
        assert exc_info.value.status_code == 400
        assert "DC RPM dönüşüm hatası" in exc_info.value.detail
        assert fake_bridge.sent_commands == []


class TestStepFailureShortCircuitsDc:
    def test_step_not_sent_means_dc_never_sent(self, isolated_motion_params_config, monkeypatch):
        fake_bridge = FakeBridge(step_ok=False)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()
        result = main.motor_feed_start(cmd)
        assert result["success"] is False
        assert result["stage"] == "step"
        assert len(fake_bridge.sent_commands) == 1
        assert fake_bridge.sent_commands[0]["cmd"] == "step"

    def test_step_failure_does_not_spawn_watcher(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=False)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()

        async def run():
            return main.motor_feed_start(cmd)

        asyncio.run(run())
        assert main._sync_watcher_task is None
        assert blocking_watcher == []


class ExplodingBridge(FakeBridge):
    """send_command çağrıldığında (belirtilen cmd için) seri port hatasını
    taklit eden exception fırlatan sahte bridge — Madde 1: bu artık çıplak
    500 değil, HTTPException(502, ...) olarak yakalanmalı."""

    def __init__(self, fail_on_cmd: str):
        super().__init__(step_ok=True)
        self._fail_on_cmd = fail_on_cmd

    def send_command(self, command: dict) -> dict:
        if command.get("cmd") == self._fail_on_cmd:
            raise TimeoutError("seri port yanit vermedi")
        return super().send_command(command)


class TestSerialErrorsBecomeCleanHttpException:
    """Madde 1 (24-09-2026 saha bulgusu): bridge.send_command'in fırlattığı
    beklenmeyen exception'lar (timeout, bağlantı kopması) artık çıplak 500
    olarak dışarı sızmıyor, motion_calc'ın ValueError yakalama deseniyle
    tutarlı şekilde açıklamalı HTTPException(502, ...)'e çevriliyor."""

    def test_step_send_command_exception_becomes_502(self, isolated_motion_params_config, monkeypatch):
        fake_bridge = ExplodingBridge(fail_on_cmd="step")
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()
        with pytest.raises(HTTPException) as exc_info:
            main.motor_feed_start(cmd)
        assert exc_info.value.status_code == 502
        assert "STM32 ile haberleşme hatası" in exc_info.value.detail

    def test_dc_send_command_exception_becomes_502(self, isolated_motion_params_config, monkeypatch):
        fake_bridge = ExplodingBridge(fail_on_cmd="dc")
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()
        with pytest.raises(HTTPException) as exc_info:
            main.motor_feed_start(cmd)
        assert exc_info.value.status_code == 502
        assert "STM32 ile haberleşme hatası" in exc_info.value.detail
        # step komutu zaten gönderilmişti (dc aşamasında patladı)
        assert any(c["cmd"] == "step" for c in fake_bridge.sent_commands)


class TestSuccessPath:
    def test_success_sends_step_then_dc_with_expected_converted_values(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        # speed=5, distance=100, accel=50, D_drive=52 (varsayılan) -> steps=15671, delay_us=1276, accel_steps=23
        # rpm=10, D_wheel_dc=52, D_rod=10, RPM_MAX_NOLOAD=100 (varsayılan) -> duty=2
        cmd = _valid_command(dir=1, speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, dc_dir="forward", rpm=10.0)

        async def run():
            main._main_event_loop = asyncio.get_running_loop()  # bkz. _reset_main_event_loop
            return main.motor_feed_start(cmd)

        result = asyncio.run(run())

        assert result["success"] is True
        assert result["step_calc"] == {"steps": 15671, "delay_us": 1276, "accel_steps": 23, "mm_per_step": pytest.approx(0.006381360077604268)}
        assert result["dc_calc"]["duty"] == 2

        assert len(fake_bridge.sent_commands) == 2
        step_cmd, dc_cmd = fake_bridge.sent_commands
        assert step_cmd == {"cmd": "step", "dir": 1, "delay": 1276, "steps": 15671, "accel": 23}
        assert dc_cmd == {"cmd": "dc", "dir": "forward", "speed": 2}

    def test_success_spawns_watcher_with_expected_max_wait(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command(speed_mms=5.0, distance_mm=100.0)  # 100/5=20s teorik -> 20*3+5=65s

        async def run():
            main._main_event_loop = asyncio.get_running_loop()  # bkz. _reset_main_event_loop
            main.motor_feed_start(cmd)
            assert main._sync_watcher_task is not None
            assert not main._sync_watcher_task.done()
            # run_coroutine_threadsafe eski create_task'tan FARKLI olarak
            # call_soon_threadsafe ile bir ekstra loop turu üzerinden Task'ı
            # oluşturuyor — tek bir sleep(0) (eski create_task ile yeterliydi)
            # artık gövdenin (calls.append) çalışmasını garantilemiyor, bu
            # yüzden kısa ama sıfır olmayan bir bekleme kullanıyoruz.
            await asyncio.sleep(0.01)
            main._sync_watcher_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wrap_future(main._sync_watcher_task)

        asyncio.run(run())
        assert blocking_watcher == [65.0]

    def test_short_move_clamps_to_minimum_wait_timeout(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        # distance=1, speed=5 -> 0.2s teorik -> 0.2*3+5=5.6s < FEED_SYNC_WAIT_MIN_TIMEOUT_S(10) -> 10'a kırpılır
        cmd = _valid_command(speed_mms=5.0, distance_mm=1.0, accel_mms2=0.0)

        async def run():
            main._main_event_loop = asyncio.get_running_loop()  # bkz. _reset_main_event_loop
            main.motor_feed_start(cmd)
            # bkz. test_success_spawns_watcher_with_expected_max_wait yorumu —
            # run_coroutine_threadsafe'in ekstra loop turu için sleep(0) yetmez.
            await asyncio.sleep(0.01)
            main._sync_watcher_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wrap_future(main._sync_watcher_task)

        asyncio.run(run())
        assert blocking_watcher == [10.0]

    def test_new_feed_start_cancels_previous_watcher(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()

        async def run():
            main._main_event_loop = asyncio.get_running_loop()  # bkz. _reset_main_event_loop
            main.motor_feed_start(cmd)
            first_task = main._sync_watcher_task

            # DİKKAT: iki motor_feed_start() çağrısı arasında hiç `await` yok,
            # yani event loop'un ilk task'ı bir kez bile ÇALIŞTIRMAYA fırsatı
            # olmuyor — cancel() isteği task hiç başlamadan işleniyor (aynı
            # davranış run_coroutine_threadsafe'in döndürdüğü Future için de
            # geçerli: henüz zincirlenmeden cancel edilirse gövde hiç
            # yürütülmez). Bu yüzden blocking_watcher'a SADECE ikinci (aktif
            # kalan) task'ın değeri düşecek.
            main.motor_feed_start(cmd)
            second_task = main._sync_watcher_task

            assert first_task is not second_task
            await asyncio.sleep(0.01)  # cancel() isteğinin islenmesi icin bir tik dongu ver
            assert first_task.cancelled()

            second_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wrap_future(second_task)

        asyncio.run(run())
        assert blocking_watcher == [65.0]


class TestSyncWatcherAlwaysStopsDc:
    """24-09-2026 saha bulgusu: kısa/hızlı bir step hareketi ARM fazının
    100ms'lik poll aralığının arasından tamamen kaçabiliyor (running:0->1->0
    ARM penceresi içinde başlayıp bitebiliyor). Eski kod bu durumda 'senkron
    DC durdurma iptal edildi' deyip DC'ye hiç dokunmadan return ediyordu —
    DC sonsuza dek dönmeye devam ediyordu (sahada gözlemlendi). Bu testler
    gerçek `main._sync_watcher`'ı (fake'siz) doğrudan çağırır.

    Regresyon kanıtı: eski gövde (ARM gözlenmezse `return`) geçici olarak
    geri getirilip bu testin FAIL verdiği doğrulandı, düzeltmeyle PASS'e
    döndü — yani bu gerçek bir davranış testi, sadece mevcut kodu yankılayan
    bir test değil."""

    def test_dc_stop_sent_even_when_running_flag_never_observed_as_1(self, monkeypatch):
        # running ARM fazı boyunca hiç 1 görülmüyor (poll aralığının
        # arasından tamamen kaçmış bir hareket senaryosu).
        fake_bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", fake_bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)

        asyncio.run(main._sync_watcher(max_wait_s=0.2))

        assert {"cmd": "dc", "dir": "stop"} in fake_bridge.sent_commands

    def test_dc_stop_sent_after_normal_arm_and_finish_sequence(self, monkeypatch):
        # Normal senaryo: running 0 -> 1 (ARM fazında görülür) -> 0 (BEKLE
        # fazında görülür). Regresyona karşı: bu yol hâlâ çalışıyor mu?
        fake_bridge = FakeBridge()
        fake_bridge._status = {"running": 0}
        monkeypatch.setattr(main, "bridge", fake_bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)

        async def flip_running():
            await asyncio.sleep(0.02)
            fake_bridge._status = {"running": 1}
            await asyncio.sleep(0.02)
            fake_bridge._status = {"running": 0}

        async def run():
            await asyncio.gather(main._sync_watcher(max_wait_s=0.3), flip_running())

        asyncio.run(run())

        assert {"cmd": "dc", "dir": "stop"} in fake_bridge.sent_commands

    def test_dc_stop_sent_via_failsafe_when_arm_missed_and_never_finishes(self, monkeypatch):
        # ARM hiç görülmez VE running hiçbir zaman 0'a dönmez (bağlantı
        # kopmuş gibi) — fail-safe max_wait_s dolunca yine de DC durmalı.
        fake_bridge = FakeBridge()
        fake_bridge._status = {"running": 1}  # BEKLE fazı boyunca hiç 0 olmuyor
        monkeypatch.setattr(main, "bridge", fake_bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)

        asyncio.run(main._sync_watcher(max_wait_s=0.05))

        assert {"cmd": "dc", "dir": "stop"} in fake_bridge.sent_commands


class TestMotorStopCancelsWatcher:
    def test_motor_stop_cancels_active_watcher(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()

        async def run():
            main._main_event_loop = asyncio.get_running_loop()  # bkz. _reset_main_event_loop
            main.motor_feed_start(cmd)
            task = main._sync_watcher_task
            main.motor_stop()
            await asyncio.sleep(0.01)
            assert task.cancelled()

        asyncio.run(run())
        # DUR'un kendi davranışı (step'i durdurma komutu) DEĞİŞMEDİ — hâlâ gönderiliyor.
        assert {"cmd": "stop"} in fake_bridge.sent_commands


class TestMainEventLoopNotReadyDefensiveBranch:
    """_main_event_loop teorik olarak (pratikte imkansız — lifespan startup
    her zaman ilk istekten önce tamamlanır) None kalmış olsaydı, artık çıplak
    500/RuntimeError yerine açıklamalı 503 dönüyor."""

    def test_returns_503_when_main_event_loop_unset(self, isolated_motion_params_config, monkeypatch):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        assert main._main_event_loop is None  # bkz. _reset_main_event_loop autouse fixture
        cmd = _valid_command()

        with pytest.raises(HTTPException) as exc_info:
            main.motor_feed_start(cmd)

        assert exc_info.value.status_code == 503
        # step+dc zaten gönderilmiş olabilir (hardware'e dokunulduktan sonraki
        # tek guard noktası burası) — asıl garanti çıplak 500 DEĞİL, anlamlı
        # bir hata dönmesi.
        assert any(c["cmd"] == "step" for c in fake_bridge.sent_commands)


class TestFeedStartRealHttpDispatch:
    """24-09-2026 saha bugı — GERÇEK regresyon testi: /motor/feed-start SENKRON
    bir endpoint (main.py'de `def`, `async def` değil), FastAPI/Starlette onu
    bir THREAD POOL işçi thread'inde çalıştırır (anyio.to_thread.run_sync); o
    thread'in kendi çalışan bir asyncio event loop'u YOKTUR. Eski kod orada
    doğrudan asyncio.create_task(...) çağırıyordu ve bu satırda
    'RuntimeError: no running event loop' ile patlıyordu — step+DC komutu
    ZATEN gönderilmiş oluyordu (motor fiziksel olarak hareket ediyordu) ama
    _sync_watcher_task hiç doğmuyordu, yani step bitince DC'yi otomatik
    durduracak hiçbir şey çalışmıyordu (24-09 sahadaki "step durunca DC
    durmuyor" + "sunucu 500" şikayetlerinin GERÇEK kök nedeni).

    Bu sınıftaki testler main.motor_feed_start()'ı DOĞRUDAN çağırmaz — gerçek
    fastapi.testclient.TestClient ile GERÇEK bir HTTP POST atar, böylece
    yukarıdaki dosyadaki diğer testlerin YAKALAYAMADIĞI gerçek thread-pool
    dispatch senaryosunu tetikler."""

    @pytest.fixture
    def dispatch(self, isolated_motion_params_config, isolated_rules_config, isolated_roi_config, monkeypatch, tmp_path):
        """main.app'i gerçek TestClient ile (lifespan dahil) ayağa kaldırır.

        rules/roi config'lerini boş tmp_path'e yönlendirmek (mevcut
        conftest fixture'ları) main.py'nin lifespan()'da başlattığı arka
        plan döngülerini (_rule_engine_loop, _vision_raw_log_loop) etkisiz
        hale getirir — kayıtlı kriter/ROI olmadığından ne kamera karesi
        yakalamaya çalışırlar ne de fake_bridge'e (aşağıda) beklenmedik ekstra
        komut (ör. interlock "stop") gönderirler; testin asıl odağı olan
        step/dc komut sayısı sayımları bu yüzden bozulmaz.

        journal.write_entry SADECE bu iki fixture'la izole edilemiyor —
        varsayılan journal_dir parametresi fonksiyon TANIMLANDIĞINDA
        (JOURNAL_DIR) bağlanıyor, sonradan journal.JOURNAL_DIR'i
        monkeypatch etmek bu bağlı varsayılanı DEĞİŞTİRMEZ (klasik Python
        gotcha'sı) — _journal_loop her turda koşulsuz write_entry çağırdığı
        için (ROI boş olsa bile) gerçek write_entry'yi tmp_path'e yönlendiren
        bir sarmalayıcıyla değiştiriyoruz, testler ASLA gerçek rpi-core/
        journal/ klasörüne yazmasın diye (conftest.py'deki izolasyon kuralı)."""
        import journal as journal_module

        real_write_entry = journal_module.write_entry
        tmp_journal_dir = tmp_path / "journal"
        monkeypatch.setattr(
            journal_module,
            "write_entry",
            lambda entry, journal_dir=tmp_journal_dir: real_write_entry(entry, journal_dir),
        )

        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)

        with TestClient(main.app) as client:
            yield client, fake_bridge

    def test_feed_start_does_not_crash_under_real_thread_pool_dispatch(self, dispatch):
        """Mutation-doğrulama: asyncio.run_coroutine_threadsafe geçici olarak
        eski asyncio.create_task'a geri alınıp bu testin 500 ile FAIL verdiği
        (journalctl'deki gerçek RuntimeError'la birebir) doğrulandı, fix geri
        konunca PASS'e döndü."""
        client, fake_bridge = dispatch

        response = client.post(
            "/motor/feed-start",
            json={"dir": 1, "speed_mms": 5.0, "distance_mm": 100.0, "accel_mms2": 50.0, "dc_dir": "forward", "rpm": 10.0},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert any(c["cmd"] == "step" for c in fake_bridge.sent_commands)
        assert any(c["cmd"] == "dc" for c in fake_bridge.sent_commands)

    def test_sync_watcher_genuinely_runs_on_main_loop_and_stops_dc(self, dispatch, monkeypatch):
        """Sadece 'crash etmiyor' değil — watcher'ın gerçekten ana event
        loop'ta (TestClient'ın portal thread'i) ÇALIŞTIĞINI, işçi thread'inde
        (isteği işleyen thread) DEĞİL, doğrular. running hiçbir zaman 0'a
        dönmüyor (bağlantı kopmuş senaryosu) — fail-safe deadline'ı test hızlı
        bitsin diye kısaltılıyor."""
        client, fake_bridge = dispatch
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_MIN_TIMEOUT_S", 0.2)
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_SAFETY_MARGIN_S", 0.1)
        fake_bridge._status = {"running": 1}  # ARM hemen gözlenir, BEKLE fazında hiç 0 olmaz

        response = client.post(
            "/motor/feed-start",
            json={"dir": 1, "speed_mms": 5.0, "distance_mm": 1.0, "accel_mms2": 0.0, "dc_dir": "forward", "rpm": 10.0},
        )
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

        # Watcher, isteği işleyen thread pool thread'inden DEĞİL, gerçek ana
        # event loop'tan (portal thread) çalışır — test thread'i o loop'un
        # dışında olduğu için burada normal (bloklayan) time.sleep kullanmak
        # güvenli, portal'ın kendi thread'ini engellemez.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if {"cmd": "dc", "dir": "stop"} in fake_bridge.sent_commands:
                break
            time.sleep(0.05)

        assert {"cmd": "dc", "dir": "stop"} in fake_bridge.sent_commands
