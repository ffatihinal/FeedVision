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
"""

import asyncio

import pytest
from fastapi import HTTPException

import main


class FakeBridge:
    """serial_bridge.STM32Bridge'in gerçek donanım/seri port'a hiç
    dokunmayan, davranışı testte kontrol edilebilen sahte sürümü."""

    def __init__(self, step_ok: bool = True):
        self.sent_commands: list[dict] = []
        self.step_ok = step_ok
        self._status = {"running": 0}

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


class TestSuccessPath:
    def test_success_sends_step_then_dc_with_expected_converted_values(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        # speed=5, distance=100, accel=50, D_drive=52 (varsayılan) -> steps=15671, delay_us=1276, accel_steps=23
        # rpm=10, D_wheel_dc=52, D_rod=10, RPM_MAX_NOLOAD=100 (varsayılan) -> duty=2
        cmd = _valid_command(dir=1, speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, dc_dir="forward", rpm=10.0)

        async def run():
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
            main.motor_feed_start(cmd)
            assert main._sync_watcher_task is not None
            assert not main._sync_watcher_task.done()
            await asyncio.sleep(0)  # task'ın gövdesinin (calls.append) en az bir kez çalışmasına izin ver
            main._sync_watcher_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await main._sync_watcher_task

        asyncio.run(run())
        assert blocking_watcher == [65.0]

    def test_short_move_clamps_to_minimum_wait_timeout(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        # distance=1, speed=5 -> 0.2s teorik -> 0.2*3+5=5.6s < FEED_SYNC_WAIT_MIN_TIMEOUT_S(10) -> 10'a kırpılır
        cmd = _valid_command(speed_mms=5.0, distance_mm=1.0, accel_mms2=0.0)

        async def run():
            main.motor_feed_start(cmd)
            await asyncio.sleep(0)
            main._sync_watcher_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await main._sync_watcher_task

        asyncio.run(run())
        assert blocking_watcher == [10.0]

    def test_new_feed_start_cancels_previous_watcher(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()

        async def run():
            main.motor_feed_start(cmd)
            first_task = main._sync_watcher_task

            # DİKKAT: iki motor_feed_start() çağrısı arasında hiç `await` yok,
            # yani event loop'un ilk task'ı bir kez bile ÇALIŞTIRMAYA fırsatı
            # olmuyor — cancel() isteği task hiç başlamadan işleniyor (gerçek
            # asyncio davranışı: hiç başlamamış bir task cancel edilince
            # gövdesi bir kez bile yürütülmez). Bu yüzden blocking_watcher'a
            # SADECE ikinci (aktif kalan) task'ın değeri düşecek.
            main.motor_feed_start(cmd)
            second_task = main._sync_watcher_task

            assert first_task is not second_task
            await asyncio.sleep(0.01)  # cancel() isteğinin islenmesi icin bir tik dongu ver
            assert first_task.cancelled()

            second_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_task

        asyncio.run(run())
        assert blocking_watcher == [65.0]


class TestMotorStopCancelsWatcher:
    def test_motor_stop_cancels_active_watcher(self, isolated_motion_params_config, monkeypatch, blocking_watcher):
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        cmd = _valid_command()

        async def run():
            main.motor_feed_start(cmd)
            task = main._sync_watcher_task
            main.motor_stop()
            await asyncio.sleep(0.01)
            assert task.cancelled()

        asyncio.run(run())
        # DUR'un kendi davranışı (step'i durdurma komutu) DEĞİŞMEDİ — hâlâ gönderiliyor.
        assert {"cmd": "stop"} in fake_bridge.sent_commands
