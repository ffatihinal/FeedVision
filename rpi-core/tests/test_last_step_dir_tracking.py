"""
FeedVision — `main._last_step_dir` tracking testleri (Madde 5, 29-09-2026).

STM32'nin periyodik durumu step motor yönünü içermediği için (bkz.
feed_totalizer.py modül docstring'i), main.py `/motor/step` VE
`/motor/feed-start` endpoint'lerinin en son gönderdiği `dir`'i modül
seviyesinde ayrıca tutuyor (`_last_step_dir`) — bu, `_rule_engine_loop`'un
`feed_totalizer.update()`'e geçirdiği değer. Burada SADECE bu state'in
doğru güncellendiği test ediliyor; feed_totalizer'ın yön-farkındalıklı
toplama mantığı test_feed_totalizer.py'de ayrıca ve kapsamlı test edildi.
"""

import asyncio

import main


class FakeBridge:
    def __init__(self, step_ok: bool = True):
        self.sent_commands: list[dict] = []
        self.step_ok = step_ok
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
        return {"running": 0}


def _valid_feed_start_command(**overrides) -> "main.FeedStartCommand":
    defaults = dict(dir=1, speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, dc_dir="forward", rpm=10.0)
    defaults.update(overrides)
    return main.FeedStartCommand(**defaults)


class ResetLastStepDir:
    """Testler arası sızmasın diye (diğer test dosyalarındaki
    _reset_sync_watcher_task/_reset_main_event_loop ile aynı desen)."""

    def __enter__(self):
        main._last_step_dir = None
        return self

    def __exit__(self, *exc):
        main._last_step_dir = None


def test_motor_step_sets_last_step_dir_forward():
    with ResetLastStepDir():
        fake_bridge = FakeBridge()
        orig_bridge = main.bridge
        main.bridge = fake_bridge
        try:
            main.motor_step(main.StepCommand(dir=0, delay=500, steps=100, accel=0))
            assert main._last_step_dir == 0
        finally:
            main.bridge = orig_bridge


def test_motor_step_sets_last_step_dir_backward():
    with ResetLastStepDir():
        fake_bridge = FakeBridge()
        orig_bridge = main.bridge
        main.bridge = fake_bridge
        try:
            main.motor_step(main.StepCommand(dir=1, delay=500, steps=100, accel=0))
            assert main._last_step_dir == 1
        finally:
            main.bridge = orig_bridge


def test_motor_step_sets_last_step_dir_even_when_not_sent():
    # Bağlı değilken (sent=False) bile "niyet edilen yön" tutulur — zararsız,
    # çünkü encoder zaten hareket etmediği için totalizer'da hiçbir delta
    # oluşmaz (bkz. main.py motor_step yorumu).
    with ResetLastStepDir():
        fake_bridge = FakeBridge(step_ok=False)
        orig_bridge = main.bridge
        main.bridge = fake_bridge
        try:
            main.motor_step(main.StepCommand(dir=1, delay=500, steps=100, accel=0))
            assert main._last_step_dir == 1
        finally:
            main.bridge = orig_bridge


def test_motor_feed_start_success_sets_last_step_dir(isolated_motion_params_config, monkeypatch):
    with ResetLastStepDir():
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        monkeypatch.setattr(main, "_sync_watcher_task", None)
        monkeypatch.setattr(main, "_main_event_loop", asyncio.new_event_loop())

        async def run():
            main._main_event_loop = asyncio.get_running_loop()
            return main.motor_feed_start(_valid_feed_start_command(dir=0))

        result = asyncio.run(run())
        assert result["success"] is True
        assert main._last_step_dir == 0


def test_motor_feed_start_validation_failure_does_not_touch_last_step_dir(isolated_motion_params_config, monkeypatch):
    # dc_dir gecersiz -> donanima HICBIR sey gonderilmeden 400 -> _last_step_dir
    # ONCEKI degerinde kalmali (degismemeli), cunku hicbir step komutu
    # gonderilmedi.
    with ResetLastStepDir():
        main._last_step_dir = 1  # onceden bilinen bir yon
        fake_bridge = FakeBridge(step_ok=True)
        monkeypatch.setattr(main, "bridge", fake_bridge)
        from fastapi import HTTPException

        try:
            main.motor_feed_start(_valid_feed_start_command(dir=0, dc_dir="sideways"))
            raise AssertionError("HTTPException bekleniyordu")
        except HTTPException:
            pass
        assert main._last_step_dir == 1
        assert fake_bridge.sent_commands == []
