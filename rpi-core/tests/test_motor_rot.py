"""
FeedVision — NEMA17 dönüş ekseni: POST /motor/rot ve POST /motor/feed-start'ın
`rot_motor` modları ("dc" varsayılan / "nema17" / "both") testleri (05-10-2026).

Hepsi GERÇEK fastapi.testclient.TestClient üzerinden (lifespan dahil): senkron
endpoint'in FastAPI thread pool'unda çalışması + _sync_watcher'ın ana event
loop'a run_coroutine_threadsafe ile iletilmesi (30-09 saha bugı) ancak böyle
test edilebilir. İstisna: dosyanın sonundaki "doğrudan çağrı" testleri
(watcher fake'li, deterministik) — onlar zaten asyncio.run içinde çalışıp
motor_feed_start'ı çağırıyor (test_motor_feed_start.py ile aynı desen).
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import main


class RotBridge:
    """Donanıma dokunmayan sahte STM32Bridge. `raise_on`: (cmd, dir) çiftleri
    için seri port hatası fırlatır; `step_ok=False` step'i reddeder."""

    def __init__(self, step_ok: bool = True, raise_on: tuple = ()):
        self.sent_commands: list[dict] = []
        self.step_ok = step_ok
        self.raise_on = set(raise_on)
        self.status = {"running": 0}
        self.is_connected = True
        self.last_error = None

    def get_status_age(self):
        return 0.1

    def get_diagnostics(self):
        return {}

    def get_status(self):
        return dict(self.status)

    def send_command(self, command: dict) -> dict:
        self.sent_commands.append(command)
        if (command.get("cmd"), command.get("dir")) in self.raise_on:
            raise TimeoutError("seri port yanit vermedi")
        if command.get("cmd") == "step" and not self.step_ok:
            return {"sent": False, "raw_command": None, "command": command, "raw_reply": None, "reply": None, "timed_out": False}
        return {"sent": True, "raw_command": "...", "command": command, "raw_reply": '{"ok":true}', "reply": {"ok": True}, "timed_out": False}

    def cmds(self, name: str) -> list[dict]:
        return [c for c in self.sent_commands if c.get("cmd") == name]


@pytest.fixture(autouse=True)
def _reset_globals():
    main._sync_watcher_task = None
    main._sync_watcher_motors = set()
    main._main_event_loop = None
    yield
    main._sync_watcher_task = None
    main._sync_watcher_motors = set()
    main._main_event_loop = None


@pytest.fixture
def api(isolated_motion_params_config, isolated_rules_config, isolated_roi_config, monkeypatch, tmp_path):
    """Gerçek main.app + TestClient (lifespan dahil), sahte bridge, izole
    config'ler/journal (bkz. test_motor_feed_start.TestFeedStartRealHttpDispatch.dispatch)."""
    import journal as journal_module

    real_write_entry = journal_module.write_entry
    tmp_journal_dir = tmp_path / "journal"
    monkeypatch.setattr(journal_module, "write_entry", lambda entry, journal_dir=tmp_journal_dir: real_write_entry(entry, journal_dir))
    # Gerçek watcher testleri hızlı bitsin
    monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.05)
    monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(main, "FEED_SYNC_WAIT_MIN_TIMEOUT_S", 0.3)
    monkeypatch.setattr(main, "FEED_SYNC_WAIT_SAFETY_MARGIN_S", 0.1)
    bridge = RotBridge()
    monkeypatch.setattr(main, "bridge", bridge)
    with TestClient(main.app) as client:
        yield client, bridge


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _feed(client, **overrides):
    body = {"dir": 1, "speed_mms": 5.0, "distance_mm": 1.0, "accel_mms2": 0.0, "rpm": 10.0}
    body.update(overrides)
    return client.post("/motor/feed-start", json=body)


# --------------------------------------------------------------------------
class TestMotorRotEndpoint:
    def test_cw_sends_computed_delay_and_returns_calc(self, api):
        client, bridge = api
        r = client.post("/motor/rot", json={"dir": "cw", "rpm": 100.0})
        assert r.status_code == 200, r.text
        # D_wheel_rot=52, D_rod=10, ppr=3200 (varsayılan) -> delay 975
        assert bridge.sent_commands == [{"cmd": "rot", "dir": "cw", "delay": 975}]
        body = r.json()
        assert body["sent"] is True and body["reply"] == {"ok": True}  # /motor/dc ile aynı biçim
        assert body["rot_calc"]["delay_us"] == 975
        assert body["rot_calc"]["speed_pct_of_max"] == pytest.approx(10.256, abs=0.01)

    def test_ccw(self, api):
        client, bridge = api
        assert client.post("/motor/rot", json={"dir": "ccw", "rpm": 10.0}).status_code == 200
        assert bridge.sent_commands == [{"cmd": "rot", "dir": "ccw", "delay": 9750}]

    def test_stop_needs_no_rpm(self, api):
        client, bridge = api
        r = client.post("/motor/rot", json={"dir": "stop"})
        assert r.status_code == 200
        assert bridge.sent_commands == [{"cmd": "rot", "dir": "stop"}]
        assert "rot_calc" not in r.json()

    def test_uses_server_side_params(self, api):
        client, bridge = api
        client.post("/motion-params", json={"ROT_PPR": 6400, "D_wheel_rot_mm": 104.0})
        client.post("/motor/rot", json={"dir": "cw", "rpm": 100.0})
        # wheel_rpm = 100*10/104 = 9.615 ; pps = 9.615/60*6400 = 1025.64 -> 975
        assert bridge.sent_commands[-1] == {"cmd": "rot", "dir": "cw", "delay": 975}
        client.post("/motion-params", json={"ROT_PPR": 3200, "D_wheel_rot_mm": 52.0})
        client.post("/motor/rot", json={"dir": "cw", "rpm": 100.0})
        assert bridge.sent_commands[-1]["delay"] == 975
        client.post("/motion-params", json={"ROT_PPR": 1600})
        client.post("/motor/rot", json={"dir": "cw", "rpm": 100.0})
        assert bridge.sent_commands[-1]["delay"] == 1950  # PPR yarıya inince darbe yarı, gecikme 2x

    @pytest.mark.parametrize(
        "body",
        [
            {"dir": "cw"},  # rpm eksik
            {"dir": "cw", "rpm": 0},
            {"dir": "cw", "rpm": -5},
            {"dir": "cw", "rpm": 5000.0},  # aralık dışı (üst)
            {"dir": "ccw", "rpm": 0.5},  # aralık dışı (alt)
            {"dir": "sideways", "rpm": 10.0},
            {"dir": "forward", "rpm": 10.0},
        ],
    )
    def test_invalid_requests_400_and_hardware_untouched(self, api, body):
        client, bridge = api
        r = client.post("/motor/rot", json=body)
        assert r.status_code == 400, r.text
        assert bridge.sent_commands == []

    def test_range_error_mentions_supported_rpm_range(self, api):
        client, _ = api
        detail = client.post("/motor/rot", json={"dir": "cw", "rpm": 5000.0}).json()["detail"]
        assert "NEMA17 RPM dönüşüm hatası" in detail and "max ~975.0 RPM" in detail and "min ~1.625 RPM" in detail

    @pytest.mark.parametrize("body", [{"dir": "cw", "rpm": 10.0}, {"dir": "stop"}])
    def test_serial_error_becomes_502(self, api, body):
        client, bridge = api
        bridge.raise_on = {("rot", body["dir"])}
        r = client.post("/motor/rot", json=body)
        assert r.status_code == 502
        assert "STM32 ile haberleşme hatası" in r.json()["detail"]


class TestMotionParamsEndpointRotFields:
    def test_get_includes_new_fields(self, api):
        client, _ = api
        params = client.get("/motion-params").json()["params"]
        assert params["D_wheel_rot_mm"] == 52.0 and params["ROT_PPR"] == 3200

    def test_post_saves_and_rejects_invalid_ppr(self, api):
        client, _ = api
        assert client.post("/motion-params", json={"ROT_PPR": 6400}).json()["params"]["ROT_PPR"] == 6400
        r = client.post("/motion-params", json={"ROT_PPR": 1234})
        assert r.status_code == 400 and "200, 400, 800, 1600, 3200, 6400" in r.json()["detail"]
        assert client.get("/motion-params").json()["params"]["ROT_PPR"] == 6400  # reddedilen değer kaydedilmedi
        assert client.post("/motion-params", json={"D_wheel_rot_mm": -3}).status_code == 422


# --------------------------------------------------------------------------
class TestFeedStartDefaultDcRegression:
    """rot_motor verilmezse ("dc" varsayılan) davranış ESKİSİYLE aynı: yalnızca
    step + dc komutları, watcher yalnızca dc stop gönderir, rot'a dokunulmaz."""

    def test_command_sequence_and_response_unchanged(self, api):
        client, bridge = api
        r = _feed(client, speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, dc_dir="forward", rpm=10.0)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True and body["dc_calc"]["duty"] == 2
        assert body["rot_calc"] is None and body["rot_result"] is None
        assert [c["cmd"] for c in bridge.sent_commands[:2]] == ["step", "dc"]
        assert bridge.sent_commands[0] == {"cmd": "step", "dir": 1, "delay": 1276, "steps": 15671, "accel": 23}
        assert bridge.sent_commands[1] == {"cmd": "dc", "dir": "forward", "speed": 2}
        assert bridge.cmds("rot") == []

    def test_watcher_stops_only_dc(self, api):
        client, bridge = api
        assert _feed(client).status_code == 200
        assert _wait_for(lambda: {"cmd": "dc", "dir": "stop"} in bridge.sent_commands)
        time.sleep(0.2)
        assert bridge.cmds("rot") == []

    def test_dc_rpm_out_of_range_still_rejected_before_hardware(self, api):
        client, bridge = api
        r = _feed(client, rpm=525.0)
        assert r.status_code == 400 and "DC RPM dönüşüm hatası" in r.json()["detail"]
        assert bridge.sent_commands == []

    def test_explicit_dc_equals_default(self, api):
        client, bridge = api
        assert _feed(client, rot_motor="dc").status_code == 200
        assert [c["cmd"] for c in bridge.sent_commands[:2]] == ["step", "dc"]


class TestFeedStartNema17:
    def test_sequence_step_then_rot_no_dc(self, api):
        client, bridge = api
        r = _feed(client, rot_motor="nema17", rot_dir="ccw", rpm=100.0)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True and body["rot_calc"]["delay_us"] == 975 and body["dc_calc"] is None
        assert [c["cmd"] for c in bridge.sent_commands[:2]] == ["step", "rot"]
        assert bridge.sent_commands[1] == {"cmd": "rot", "dir": "ccw", "delay": 975}
        assert bridge.cmds("dc") == []

    def test_watcher_stops_rot_not_dc(self, api):
        client, bridge = api
        assert _feed(client, rot_motor="nema17", rpm=100.0).status_code == 200
        assert _wait_for(lambda: {"cmd": "rot", "dir": "stop"} in bridge.sent_commands)
        time.sleep(0.2)
        assert bridge.cmds("dc") == []

    def test_rpm_is_nema17_rod_rpm_not_dc_rpm(self, api):
        # 525 RPM DC için ulaşılamaz (duty>100) ama NEMA17 için geçerli (<=975)
        client, bridge = api
        assert _feed(client, rot_motor="nema17", rpm=525.0).status_code == 200

    def test_rot_conversion_error_400_nothing_sent(self, api):
        client, bridge = api
        r = _feed(client, rot_motor="nema17", rpm=5000.0)
        assert r.status_code == 400 and "NEMA17 RPM dönüşüm hatası" in r.json()["detail"]
        assert bridge.sent_commands == []

    def test_step_conversion_error_400_nothing_sent(self, api):
        client, bridge = api
        r = _feed(client, rot_motor="nema17", rpm=100.0, speed_mms=1000.0)
        assert r.status_code == 400 and "Step dönüşüm hatası" in r.json()["detail"]
        assert bridge.sent_commands == []

    def test_step_rejected_means_no_rotation_started_and_no_watcher(self, api):
        client, bridge = api
        bridge.step_ok = False
        body = _feed(client, rot_motor="nema17", rpm=100.0).json()
        assert body["success"] is False and body["stage"] == "step"
        assert [c["cmd"] for c in bridge.sent_commands] == ["step"]
        assert main._sync_watcher_task is None

    def test_step_serial_error_502_nothing_else_sent(self, api):
        client, bridge = api
        bridge.raise_on = {("step", 1)}
        r = _feed(client, rot_motor="nema17", rpm=100.0)
        assert r.status_code == 502 and "STM32 ile haberleşme hatası" in r.json()["detail"]
        assert [c["cmd"] for c in bridge.sent_commands] == ["step"]

    def test_rot_serial_error_502_and_no_watcher(self, api):
        client, bridge = api
        bridge.raise_on = {("rot", "cw")}
        r = _feed(client, rot_motor="nema17", rpm=100.0, rot_dir="cw")
        assert r.status_code == 502 and "STM32 ile haberleşme hatası" in r.json()["detail"]
        assert main._sync_watcher_task is None


class TestFeedStartBoth:
    def test_sequence_and_independent_directions(self, api):
        client, bridge = api
        r = _feed(client, rot_motor="both", rot_dir="ccw", dc_dir="backward", dc_speed_pct=35, rpm=100.0)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dc_calc"]["duty"] == 35 and body["rot_calc"]["delay_us"] == 975
        assert [c["cmd"] for c in bridge.sent_commands[:3]] == ["step", "rot", "dc"]
        assert bridge.sent_commands[1] == {"cmd": "rot", "dir": "ccw", "delay": 975}
        assert bridge.sent_commands[2] == {"cmd": "dc", "dir": "backward", "speed": 35}

    def test_dc_is_raw_percent_not_rpm_converted(self, api):
        # rpm=100 -> DC RPM->duty olsaydı %19 çıkardı; "both"ta ham yüzde kullanılmalı
        client, bridge = api
        _feed(client, rot_motor="both", dc_speed_pct=60, rpm=100.0)
        assert bridge.cmds("dc")[0]["speed"] == 60

    def test_watcher_stops_both(self, api):
        client, bridge = api
        assert _feed(client, rot_motor="both", dc_speed_pct=30, rpm=100.0).status_code == 200
        assert _wait_for(lambda: {"cmd": "dc", "dir": "stop"} in bridge.sent_commands and {"cmd": "rot", "dir": "stop"} in bridge.sent_commands)

    @pytest.mark.parametrize("pct", [None, 0, -5, 100.5, 1000])
    def test_dc_speed_pct_required_and_bounded_400(self, api, pct):
        client, bridge = api
        extra = {} if pct is None else {"dc_speed_pct": pct}
        r = _feed(client, rot_motor="both", rpm=100.0, **extra)
        assert r.status_code == 400 and "dc_speed_pct" in r.json()["detail"]
        assert bridge.sent_commands == []

    def test_rot_conversion_error_means_nothing_sent_even_dc(self, api):
        client, bridge = api
        r = _feed(client, rot_motor="both", dc_speed_pct=30, rpm=5000.0)
        assert r.status_code == 400
        assert bridge.sent_commands == []

    def test_dc_failure_after_rot_started_stops_rot_and_502(self, api):
        client, bridge = api
        bridge.raise_on = {("dc", "forward")}
        r = _feed(client, rot_motor="both", dc_speed_pct=30, rpm=100.0)
        assert r.status_code == 502
        # Başlatılmış NEMA17 kontrolsüz dönmesin: açık rot stop gönderilmiş olmalı
        assert bridge.sent_commands[-1] == {"cmd": "rot", "dir": "stop"}
        assert main._sync_watcher_task is None

    def test_step_rejected_touches_neither_motor(self, api):
        client, bridge = api
        bridge.step_ok = False
        body = _feed(client, rot_motor="both", dc_speed_pct=30, rpm=100.0).json()
        assert body["success"] is False
        assert bridge.cmds("rot") == [] and bridge.cmds("dc") == []


class TestFeedStartInvalidSelectors:
    @pytest.mark.parametrize(
        "overrides",
        [{"rot_motor": "stepper"}, {"rot_motor": "NEMA17"}, {"rot_dir": "forward"}, {"rot_dir": "stop"}, {"dc_dir": "stop"}],
    )
    def test_400_nothing_sent(self, api, overrides):
        client, bridge = api
        r = _feed(client, **overrides)
        assert r.status_code == 400
        assert bridge.sent_commands == []


# --------------------------------------------------------------------------
class TestSyncWatcherStopsAlways:
    """Gerçek main._sync_watcher: fail-safe/koşulsuz durdurma NEMA17 için de geçerli."""

    def test_rot_stop_sent_even_if_arm_never_observed(self, monkeypatch):
        bridge = RotBridge()  # running hiç 1 olmuyor (ARM kaçırıldı)
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.2, stop_dc=False, stop_rot=True))
        assert bridge.sent_commands == [{"cmd": "rot", "dir": "stop"}]

    def test_failsafe_timeout_stops_both(self, monkeypatch):
        bridge = RotBridge()
        bridge.status = {"running": 1}  # hiç bitmiyor
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.05, stop_dc=True, stop_rot=True))
        assert {"cmd": "dc", "dir": "stop"} in bridge.sent_commands
        assert {"cmd": "rot", "dir": "stop"} in bridge.sent_commands

    def test_default_args_keep_old_dc_only_behavior(self, monkeypatch):
        bridge = RotBridge()
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.1))
        assert bridge.sent_commands == [{"cmd": "dc", "dir": "stop"}]

    def test_one_stop_failing_does_not_block_the_other(self, monkeypatch):
        bridge = RotBridge(raise_on=[("dc", "stop")])
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.1, stop_dc=True, stop_rot=True))
        assert {"cmd": "rot", "dir": "stop"} in bridge.sent_commands

    def test_cancel_sends_no_stop_commands(self, monkeypatch):
        # İptal (yeni feed-start/DUR): eski davranış — CancelledError yutulmaz, komut gönderilmez
        bridge = RotBridge()
        bridge.status = {"running": 1}
        monkeypatch.setattr(main, "bridge", bridge)

        async def run():
            task = asyncio.ensure_future(main._sync_watcher(5.0, stop_dc=True, stop_rot=True))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        assert bridge.sent_commands == []

    def test_motor_stop_endpoint_cancels_watcher_of_nema17_session(self, api, monkeypatch):
        client, bridge = api
        bridge.status = {"running": 1}  # watcher WAIT fazında asılı kalır
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_MIN_TIMEOUT_S", 30.0)
        assert _feed(client, rot_motor="nema17", rpm=100.0).status_code == 200
        task = main._sync_watcher_task
        assert client.post("/motor/stop").status_code == 200
        assert _wait_for(task.done)
        assert {"cmd": "stop"} in bridge.sent_commands


class TestWatcherMotorHandover:
    """Yeni feed-start eski watcher'ı iptal ederken onun durdurmakla yükümlü
    olduğu motor(lar) yeni watcher'a devredilir (ör. önceki 'both', yeni
    'nema17' -> DC de durdurulmalı). Watcher fake'li, deterministik."""

    @pytest.fixture
    def recording_watcher(self, monkeypatch):
        calls = []

        async def fake_watcher(max_wait_s, **kwargs):
            calls.append(kwargs)
            await asyncio.Event().wait()

        monkeypatch.setattr(main, "_sync_watcher", fake_watcher)
        return calls

    def _cmd(self, **kw):
        base = dict(dir=1, speed_mms=5.0, distance_mm=100.0, accel_mms2=0.0, rpm=100.0)
        base.update(kw)
        return main.FeedStartCommand(**base)

    def _run(self, monkeypatch, *commands):
        monkeypatch.setattr(main, "bridge", RotBridge())

        async def run():
            main._main_event_loop = asyncio.get_running_loop()
            for cmd in commands:
                main.motor_feed_start(cmd)
            await asyncio.sleep(0.02)
            main._sync_watcher_task.cancel()

        asyncio.run(run())

    def test_dc_only_calls_watcher_exactly_like_before(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, self._cmd(rpm=10.0))
        assert recording_watcher == [{}]

    def test_nema17_only_passes_stop_rot_only(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, self._cmd(rot_motor="nema17"))
        assert recording_watcher == [{"stop_dc": False, "stop_rot": True}]

    def test_both_passes_both(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, self._cmd(rot_motor="both", dc_speed_pct=30))
        assert recording_watcher == [{"stop_dc": True, "stop_rot": True}]

    def test_new_session_inherits_previous_watchers_motors(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, self._cmd(rot_motor="both", dc_speed_pct=30), self._cmd(rot_motor="nema17"))
        # İkinci çağrı yalnızca NEMA17 kullanıyor ama DC'yi de durdurmakla yükümlü
        assert recording_watcher[-1] == {"stop_dc": True, "stop_rot": True}


class TestWebSocketStatusPassesRotFields:
    def test_rot_and_rdelay_reach_the_client(self, api):
        client, bridge = api
        bridge.status = {"running": 0, "dc": 0, "rot": 2, "rdelay": 1500}
        with client.websocket_connect("/ws/status") as ws:
            msg = ws.receive_json()
        assert msg["status"]["rot"] == 2
        assert msg["status"]["rdelay"] == 1500


class TestRealBridgeKeepsRotFields:
    def test_listen_stores_rot_and_rdelay_in_status(self):
        """Gerçek serial_bridge._listen periyodik durum satırını alan filtrelemeden
        saklar -> rot/rdelay ek bir ayrıştırma gerektirmeden WebSocket'e geçer."""
        import threading

        from serial_bridge import STM32Bridge

        class OneLineSerial:
            def __init__(self):
                self._lines = ['{"t":5,"running":0,"dc":0,"rot":1,"rdelay":975}']

            def readline(self):
                if self._lines:
                    return self._lines.pop(0).encode("utf-8")
                time.sleep(0.01)
                return b""

            def write(self, data):
                pass

            def close(self):
                pass

        b = STM32Bridge()
        b._serial = OneLineSerial()
        b._running = True
        t = threading.Thread(target=b._listen, daemon=True)
        t.start()
        assert _wait_for(lambda: b.get_status().get("rot") == 1, timeout=1.0)
        b._running = False
        t.join(timeout=1.0)
        assert b.get_status()["rdelay"] == 975
