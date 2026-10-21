"""
FeedVision — 6 motorlu sistem: Besleme Başlat'ın kanal bazlı davranışı,
bağımsız /motor/assist, /motor/stop-all, /motion-limits, /motion-params yeni
alanları ve durum akışı (21-10-2026, backlog #120).

Hepsi GERÇEK fastapi.testclient.TestClient üzerinden (lifespan dahil): senkron
endpoint'in FastAPI thread pool'unda çalışması + _sync_watcher'ın ana event
loop'a run_coroutine_threadsafe ile iletilmesi (24-09 "no running event loop"
bug sınıfı) ancak böyle yakalanır. Seri hat sahte bridge ile: hangi komutların
hangi sırayla gittiği kayıt altında.
"""

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import main

# Varsayılan parametrelerle (D_drive 52, D_wheel_rot 52, D_rod 10, ROT_PPR 3200,
# D_wheel_assist 13, ASSIST_PPR 3200, D_wheel_dc 52, RPM_MAX_NOLOAD 100):
ASSIST_10_MMS_DELAY = 1276  # 10 mm/s
ROT_100_RPM_DELAY = 975  # 100 çubuk RPM
DC_10_RPM_DUTY = 2  # 10 RPM
DC_200_RPM_DUTY = 38  # 200 RPM


class FakeBridge:
    """Donanıma dokunmayan sahte STM32Bridge. Komutları zaman damgasıyla kaydeder.
    `start_running`: step komutu gelince durum 'running:1' olur (gerçek firmware gibi);
    testler bitişi `finish_step()` ile simüle eder. `raise_on`: (cmd, dir) için
    exception; `reply_for`: (cmd) -> özel yanıt (ör. err)."""

    def __init__(self, start_running=True):
        self.sent_commands: list[dict] = []
        self.sent_at: list[float] = []
        self.status = {"running": 0, "e1": 0, "e2": 0}
        self.is_connected = True
        self.last_error = None
        self.start_running = start_running
        self.raise_on: set = set()
        self.reply_for: dict = {}  # cmd -> yanıt (yalnızca başlatma komutlarına; durdurmalara uygulanmaz)
        self.timeout_for: set = set()  # cmd -> yanıt gelmedi (timed_out)
        self.unsent_for: set = set()  # cmd -> seri porta yazılamadı (sent:false)
        self.connected_for_send = True
        self._lock = threading.Lock()

    def get_status_age(self):
        return 0.1

    def get_diagnostics(self):
        return {}

    def get_status(self):
        return dict(self.status)

    def send_command(self, command: dict, **_kw) -> dict:
        with self._lock:
            self.sent_commands.append(command)
            self.sent_at.append(time.monotonic())
        if (command.get("cmd"), command.get("dir")) in self.raise_on:
            raise TimeoutError("seri port yanit vermedi")
        if not self.connected_for_send:
            return {"sent": False, "raw_command": None, "command": command, "raw_reply": None, "reply": None, "timed_out": False}
        is_stop = command.get("cmd") == "stop" or command.get("dir") == "stop"
        if not is_stop and command.get("cmd") in self.unsent_for:
            return {"sent": False, "raw_command": None, "command": command, "raw_reply": None, "reply": None, "timed_out": False}
        if not is_stop and command.get("cmd") in self.timeout_for:
            return {"sent": True, "raw_command": "...", "command": command, "raw_reply": None, "reply": None, "timed_out": True}
        reply = {"ok": command.get("cmd")} if is_stop else self.reply_for.get(command.get("cmd"), {"ok": command.get("cmd")})
        if command.get("cmd") == "step" and self.start_running and "err" not in reply:
            self.status = {**self.status, "running": 1}
        return {
            "sent": True, "raw_command": "...", "command": command, "raw_reply": str(reply),
            "reply": reply, "timed_out": False,
        }

    def finish_step(self):
        self.status = {**self.status, "running": 0}

    def cmds(self, name):
        return [c for c in self.sent_commands if c.get("cmd") == name]

    def names(self):
        return [c["cmd"] for c in self.sent_commands]


@pytest.fixture(autouse=True)
def _reset_globals():
    main._sync_watcher_task = None
    main._sync_watcher_motors = set()
    main._main_event_loop = None
    yield
    t = main._sync_watcher_task
    if t is not None and not t.done():
        t.cancel()
    main._sync_watcher_task = None
    main._sync_watcher_motors = set()
    main._main_event_loop = None


@pytest.fixture
def api(isolated_motion_params_config, isolated_rules_config, isolated_roi_config, monkeypatch, tmp_path):
    """Gerçek main.app + TestClient (lifespan dahil), sahte bridge, izole config'ler."""
    import journal as journal_module

    real_write_entry = journal_module.write_entry
    tmp_journal_dir = tmp_path / "journal"
    monkeypatch.setattr(journal_module, "write_entry", lambda entry, journal_dir=tmp_journal_dir: real_write_entry(entry, journal_dir))
    monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.5)
    monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(main, "FEED_SYNC_WAIT_MIN_TIMEOUT_S", 5.0)
    bridge = FakeBridge()
    monkeypatch.setattr(main, "bridge", bridge)
    with TestClient(main.app) as client:
        yield client, bridge


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _feed(client, **overrides):
    body = {"dir": 0, "speed_mms": 5.0, "distance_mm": 100.0, "accel_mms2": 0.0}
    body.update(overrides)
    return client.post("/motor/feed-start", json=body)


# Beş kısa yol kombinasyonu: (istek alanları, beklenen komut adları sırayla)
COMBOS = {
    "only_nema23": ({"use_assist": False, "use_rot": False, "use_dc": False}, ["step"]),
    "nema23_assist": (
        {"use_assist": True, "use_rot": False, "use_dc": False, "assist_speed_mms": 10.0},
        ["step", "assist"],
    ),
    "nema23_rot": (
        {"use_assist": False, "use_rot": True, "use_dc": False, "rot_rpm": 100.0},
        ["step", "rot"],
    ),
    "nema23_assist_rot": (
        {"use_assist": True, "use_rot": True, "use_dc": False, "assist_speed_mms": 10.0, "rot_rpm": 100.0},
        ["step", "assist", "rot"],
    ),
    "all_with_dc": (
        {"use_assist": True, "use_rot": True, "use_dc": True, "assist_speed_mms": 10.0, "rot_rpm": 100.0, "dc_rpm": 10.0},
        ["step", "assist", "rot", "dc"],
    ),
}


# --------------------------------------------------------------------------
class TestFeedStartFiveCombinations:
    @pytest.mark.parametrize("name", list(COMBOS))
    def test_exactly_the_selected_commands_go_to_the_serial_line(self, api, name):
        client, bridge = api
        fields, expected = COMBOS[name]
        r = _feed(client, **fields)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True
        assert bridge.names() == expected
        assert body["started"] == expected
        assert body["warnings"] == []

    def test_command_contents_for_all_motors(self, api):
        client, bridge = api
        r = _feed(client, dir=0, rot_dir="ccw", dc_dir="backward", **COMBOS["all_with_dc"][0])
        assert r.status_code == 200, r.text
        assert bridge.cmds("assist") == [{"cmd": "assist", "dir": "cw", "delay": ASSIST_10_MMS_DELAY}]
        assert bridge.cmds("rot") == [{"cmd": "rot", "dir": "ccw", "delay": ROT_100_RPM_DELAY}]
        assert bridge.cmds("dc") == [{"cmd": "dc", "dir": "backward", "speed": DC_10_RPM_DUTY}]
        body = r.json()
        assert body["assist_calc"]["delay_us"] == ASSIST_10_MMS_DELAY
        assert body["rot_calc"]["delay_us"] == ROT_100_RPM_DELAY
        assert body["dc_calc"]["duty"] == DC_10_RPM_DUTY

    def test_each_motor_uses_its_own_speed_box(self, api):
        """#120 madde 4 regresyonu: eskiden tek ortak RPM kutusu NEMA17'ye de DC'nin 500'ünü gönderiyordu."""
        client, bridge = api
        r = _feed(
            client, use_assist=True, use_rot=True, use_dc=True,
            assist_speed_mms=10.0, rot_rpm=100.0, dc_rpm=200.0,
        )
        assert r.status_code == 200, r.text
        assert bridge.cmds("assist")[0]["delay"] == ASSIST_10_MMS_DELAY
        assert bridge.cmds("rot")[0]["delay"] == ROT_100_RPM_DELAY
        assert bridge.cmds("dc")[0]["speed"] == DC_200_RPM_DUTY

    def test_dc_500_rpm_does_not_leak_into_rotation_pair(self, api):
        client, bridge = api
        r = _feed(client, use_rot=True, use_dc=True, rot_rpm=10.0, dc_rpm=500.0)
        assert r.status_code == 200, r.text
        assert bridge.cmds("dc")[0]["speed"] == 96  # 500 RPM DC için geçerli
        assert bridge.cmds("rot")[0]["delay"] == 9750  # rotasyon 10 RPM, 500 DEĞİL

    def test_unselected_motor_speed_is_ignored_even_if_absurd(self, api):
        client, bridge = api
        r = _feed(client, use_assist=False, use_rot=False, use_dc=False,
                  assist_speed_mms=99999, rot_rpm=99999, dc_rpm=99999)
        assert r.status_code == 200, r.text
        assert bridge.names() == ["step"]

    def test_nema23_command_matches_existing_step_math(self, api):
        client, bridge = api
        assert _feed(client, speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, **COMBOS["only_nema23"][0]).status_code == 200
        assert bridge.sent_commands[0] == {"cmd": "step", "dir": 0, "delay": 1276, "steps": 15671, "accel": 23}


# --------------------------------------------------------------------------
class TestSimultaneousStartAndEnd:
    def test_all_motors_started_back_to_back_before_any_stop(self, api):
        client, bridge = api
        assert _feed(client, **COMBOS["all_with_dc"][0]).status_code == 200
        names = bridge.names()
        assert names[:4] == ["step", "assist", "rot", "dc"]
        # Başlangıç anında hiçbir durdurma komutu yok (hepsi çalışırken NEMA23 sürüyor)
        time.sleep(0.15)
        assert len(bridge.sent_commands) == 4
        assert not any(c.get("dir") == "stop" or c.get("cmd") == "stop" for c in bridge.sent_commands)
        # Aynı anda: komutlar arası bekleme yok (sahte hat anında yanıtlıyor)
        assert bridge.sent_at[3] - bridge.sent_at[0] < 0.25

    def test_when_nema23_finishes_every_used_motor_is_stopped(self, api):
        client, bridge = api
        assert _feed(client, **COMBOS["all_with_dc"][0]).status_code == 200
        time.sleep(0.1)
        assert len(bridge.sent_commands) == 4  # NEMA23 hâlâ çalışıyor (running:1)
        bridge.finish_step()
        stops = [{"cmd": "dc", "dir": "stop"}, {"cmd": "rot", "dir": "stop"}, {"cmd": "assist", "dir": "stop"}]
        assert _wait_for(lambda: all(s in bridge.sent_commands for s in stops))
        assert bridge.sent_commands.count({"cmd": "assist", "dir": "stop"}) == 1

    @pytest.mark.parametrize("name", ["nema23_assist", "nema23_rot", "nema23_assist_rot", "all_with_dc"])
    def test_end_stops_only_used_motors(self, api, name):
        client, bridge = api
        fields, expected = COMBOS[name]
        assert _feed(client, **fields).status_code == 200
        bridge.finish_step()
        used = set(expected) - {"step"}
        assert _wait_for(lambda: all({"cmd": m, "dir": "stop"} in bridge.sent_commands for m in used))
        time.sleep(0.15)
        for unused in {"assist", "rot", "dc"} - used:
            assert {"cmd": unused, "dir": "stop"} not in bridge.sent_commands

    def test_only_nema23_needs_no_watcher_and_sends_nothing_else(self, api):
        client, bridge = api
        assert _feed(client, **COMBOS["only_nema23"][0]).status_code == 200
        assert main._sync_watcher_task is None
        bridge.finish_step()
        time.sleep(0.2)
        assert bridge.names() == ["step"]

    def test_watcher_runs_from_threadpool_endpoint_no_event_loop_error(self, api):
        """24-09 bug sınıfı: senkron endpoint thread pool'da çalışır; watcher doğmalı."""
        client, bridge = api
        assert _feed(client, **COMBOS["nema23_assist"][0]).status_code == 200
        assert main._sync_watcher_task is not None
        bridge.finish_step()
        assert _wait_for(lambda: {"cmd": "assist", "dir": "stop"} in bridge.sent_commands)

    def test_failsafe_when_step_never_finishes(self, api, monkeypatch):
        client, bridge = api
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_MIN_TIMEOUT_S", 0.2)
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_SAFETY_MARGIN_S", 0.0)
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_SAFETY_FACTOR", 0.0)
        assert _feed(client, **COMBOS["nema23_assist_rot"][0]).status_code == 200
        assert _wait_for(lambda: {"cmd": "assist", "dir": "stop"} in bridge.sent_commands and {"cmd": "rot", "dir": "stop"} in bridge.sent_commands)


# --------------------------------------------------------------------------
class TestAssistDirectionFollowsNema23:
    @pytest.mark.parametrize("dir_, invert, expected", [(0, False, "cw"), (1, False, "ccw"), (0, True, "ccw"), (1, True, "cw")])
    def test_direction_table(self, api, dir_, invert, expected):
        client, bridge = api
        assert client.post("/motion-params", json={"assist_dir_invert": invert}).status_code == 200
        r = _feed(client, dir=dir_, **COMBOS["nema23_assist"][0])
        assert r.status_code == 200, r.text
        assert bridge.cmds("assist")[0]["dir"] == expected
        assert r.json()["assist_dir"] == expected

    def test_rotation_pair_direction_is_its_own_choice(self, api):
        client, bridge = api
        for nema_dir in (0, 1):
            bridge.sent_commands.clear()
            bridge.finish_step()
            r = _feed(client, dir=nema_dir, rot_dir="ccw", **COMBOS["nema23_assist_rot"][0])
            assert r.status_code == 200, r.text
            assert bridge.cmds("rot")[0]["dir"] == "ccw"  # NEMA23 yönünden bağımsız
            bridge.finish_step()
            time.sleep(0.05)

    def test_dc_direction_is_its_own_choice(self, api):
        client, bridge = api
        assert _feed(client, dir=1, dc_dir="forward", use_dc=True, dc_rpm=10.0).status_code == 200
        assert bridge.cmds("dc")[0]["dir"] == "forward"

    def test_independent_assist_endpoint_ignores_invert(self, api):
        client, bridge = api
        client.post("/motion-params", json={"assist_dir_invert": True})
        assert client.post("/motor/assist", json={"dir": "cw", "speed_mms": 10.0}).status_code == 200
        assert bridge.sent_commands[-1]["dir"] == "cw"

    def test_uses_assist_params_from_admin(self, api):
        client, bridge = api
        client.post("/motion-params", json={"D_wheel_assist_mm": 26.0, "ASSIST_PPR": 6400})
        assert _feed(client, **COMBOS["nema23_assist"][0]).status_code == 200
        # D 26 mm, ppr 6400, 10 mm/s: pps = 10/(pi*26)*6400 = 783.53 -> 1276
        assert bridge.cmds("assist")[0]["delay"] == ASSIST_10_MMS_DELAY


# --------------------------------------------------------------------------
class TestFeedStartValidation4xx:
    """Kanal bazında aralık dışı/eksik/bozuk girdi: 4xx, donanıma HİÇ komut gitmez, 500 asla."""

    @pytest.mark.parametrize(
        "overrides, channel_text",
        [
            ({"use_assist": True, "assist_speed_mms": 1000.0}, "Yardımcı çift"),
            ({"use_assist": True, "assist_speed_mms": 0.01}, "Yardımcı çift"),
            ({"use_assist": True, "assist_speed_mms": 0}, "Yardımcı çift"),
            ({"use_assist": True, "assist_speed_mms": -3}, "Yardımcı çift"),
            ({"use_rot": True, "rot_rpm": 5000.0}, "Rotasyon çifti"),
            ({"use_rot": True, "rot_rpm": 0.5}, "Rotasyon çifti"),
            ({"use_rot": True, "rot_rpm": 0}, "Rotasyon çifti"),
            ({"use_dc": True, "dc_rpm": 5000.0}, "DC"),
            ({"use_dc": True, "dc_rpm": 0.5}, "DC"),
            ({"use_dc": True, "dc_rpm": -1}, "DC"),
            ({"speed_mms": 5000.0}, "Step"),
            ({"speed_mms": 0.0001}, "Step"),
            ({"distance_mm": 1e9}, "Step"),
            ({"distance_mm": 1e-9}, "Step"),
        ],
    )
    def test_out_of_range_rejected_before_hardware(self, api, overrides, channel_text):
        client, bridge = api
        r = _feed(client, **overrides)
        assert r.status_code in (400, 422), r.text
        assert bridge.sent_commands == []
        if r.status_code == 400:
            assert channel_text in r.json()["detail"]

    def test_range_message_names_supported_range(self, api):
        client, _ = api
        d = _feed(client, use_assist=True, assist_speed_mms=1000.0).json()["detail"]
        assert "min ~0.213 mm/s" in d and "max ~127.6 mm/s" in d
        d = _feed(client, use_rot=True, rot_rpm=5000.0).json()["detail"]
        assert "max ~975.0 RPM" in d
        d = _feed(client, use_dc=True, dc_rpm=5000.0).json()["detail"]
        assert "max ~520.0 RPM" in d

    def test_all_channel_errors_reported_together(self, api):
        client, bridge = api
        r = _feed(client, use_assist=True, use_rot=True, use_dc=True,
                  assist_speed_mms=1000.0, rot_rpm=5000.0, dc_rpm=5000.0, speed_mms=5000.0)
        assert r.status_code == 400
        detail = r.json()["detail"]
        for token in ("Step", "Yardımcı çift", "Rotasyon çifti", "DC"):
            assert token in detail
        assert bridge.sent_commands == []

    @pytest.mark.parametrize(
        "fields, missing_token",
        [
            ({"use_assist": True}, "assist_speed_mms"),
            ({"use_rot": True}, "rot_rpm"),
            ({"use_dc": True}, "dc_rpm"),
        ],
    )
    def test_selected_motor_without_speed_is_400(self, api, fields, missing_token):
        client, bridge = api
        r = _feed(client, **fields)
        assert r.status_code == 400 and missing_token in r.json()["detail"]
        assert bridge.sent_commands == []

    def test_non_numeric_speed_is_422(self, api):
        client, bridge = api
        r = _feed(client, use_assist=True, assist_speed_mms="abc")
        assert r.status_code == 422
        assert bridge.sent_commands == []

    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
    @pytest.mark.parametrize("field", ["assist_speed_mms", "rot_rpm", "dc_rpm", "speed_mms", "distance_mm"])
    def test_nan_and_infinity_never_500(self, api, field, token):
        client, bridge = api
        body = {
            "dir": 0, "speed_mms": 5, "distance_mm": 100, "use_assist": True, "use_rot": True, "use_dc": True,
            "assist_speed_mms": 10, "rot_rpm": 100, "dc_rpm": 10,
        }
        body[field] = "__TOKEN__"
        raw = json.dumps(body).replace('"__TOKEN__"', token)
        r = client.post("/motor/feed-start", content=raw, headers={"Content-Type": "application/json"})
        assert 400 <= r.status_code < 500, (r.status_code, r.text)
        assert bridge.sent_commands == []

    @pytest.mark.parametrize("dir_", [2, -1, 5])
    def test_bad_step_dir_is_400(self, api, dir_):
        client, bridge = api
        r = _feed(client, dir=dir_)
        assert r.status_code == 400 and bridge.sent_commands == []

    @pytest.mark.parametrize(
        "overrides",
        [{"rot_dir": "forward"}, {"rot_dir": "stop"}, {"dc_dir": "stop"}, {"dc_dir": "cw"}],
    )
    def test_bad_direction_selectors_400(self, api, overrides):
        client, bridge = api
        r = _feed(client, use_rot=True, rot_rpm=10.0, **overrides)
        assert r.status_code == 400 and bridge.sent_commands == []

    def test_mixing_legacy_and_new_fields_is_400(self, api):
        client, bridge = api
        r = _feed(client, rot_motor="nema17", rpm=10.0, use_assist=True, assist_speed_mms=10.0)
        assert r.status_code == 400 and "birlikte" in r.json()["detail"]
        assert bridge.sent_commands == []

    def test_missing_required_core_fields_is_422(self, api):
        client, bridge = api
        r = client.post("/motor/feed-start", json={"dir": 0, "use_assist": False})
        assert r.status_code == 422 and bridge.sent_commands == []


# --------------------------------------------------------------------------
class TestFeedStartFailures:
    def test_step_rejected_touches_no_other_motor(self, api):
        client, bridge = api
        bridge.reply_for["step"] = {"err": "steps must be > 0"}
        body = _feed(client, **COMBOS["all_with_dc"][0]).json()
        assert body["success"] is False and body["stage"] == "step" and "reddetti" in body["reason"]
        assert bridge.names() == ["step"]
        assert main._sync_watcher_task is None

    def test_not_connected_reports_not_sent(self, api):
        client, bridge = api
        bridge.connected_for_send = False
        body = _feed(client, **COMBOS["nema23_assist"][0]).json()
        assert body["success"] is False and "bağlı değil" in body["reason"]
        assert bridge.names() == ["step"]

    def test_step_serial_exception_is_502(self, api):
        client, bridge = api
        bridge.raise_on = {("step", 0)}
        r = _feed(client, **COMBOS["nema23_assist"][0])
        assert r.status_code == 502 and "STM32 ile haberleşme hatası" in r.json()["detail"]
        assert bridge.names() == ["step"]

    def test_assist_serial_exception_stops_everything_started_and_502(self, api):
        client, bridge = api
        bridge.raise_on = {("assist", "cw")}
        r = _feed(client, **COMBOS["all_with_dc"][0])
        assert r.status_code == 502
        assert main._sync_watcher_task is None
        # step, patlayan assist, sonra 4 kanala bağımsız stop (rot son)
        assert bridge.names() == ["step", "assist", "stop", "dc", "assist", "rot"]
        assert bridge.sent_commands[-1] == {"cmd": "rot", "dir": "stop"}
        assert "[HATA] Tüm motorlar durduruldu. Sebep: Yardımcı çift (assist)" in r.json()["detail"]
        assert len(bridge.cmds("rot")) == 1 and len(bridge.cmds("dc")) == 1  # yalnızca stop'lar; başlamamıştı

    def test_dc_exception_after_assist_and_rot_started_stops_them(self, api):
        client, bridge = api
        bridge.raise_on = {("dc", "forward")}
        r = _feed(client, **COMBOS["all_with_dc"][0])
        assert r.status_code == 502
        tail = bridge.sent_commands[4:]
        assert {"cmd": "stop"} in tail  # firmware stop: NEMA23 + rot + assist
        assert {"cmd": "assist", "dir": "stop"} in tail
        assert {"cmd": "rot", "dir": "stop"} in tail
        assert {"cmd": "dc", "dir": "stop"} in tail
        assert main._sync_watcher_task is None

    def test_firmware_error_on_assist_stops_all_motors(self, api):
        """Eski davranış (uyarı + NEMA23 sürer) kaldırıldı: aux err -> TÜM motorlar durur."""
        client, bridge = api
        bridge.reply_for["assist"] = {"err": "assist timer fault"}
        r = _feed(client, **COMBOS["nema23_assist_rot"][0])
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False and body["stage"] == "assist" and body["failed_motor"] == "assist"
        assert "assist timer fault" in body["fault"]["line"]
        assert bridge.names() == ["step", "assist", "stop", "dc", "assist", "rot"]  # rot hiç başlatılmadı
        assert main._sync_watcher_task is None



# --------------------------------------------------------------------------
class TestStopAll:
    def test_sends_stop_to_every_channel(self, api):
        client, bridge = api
        r = client.post("/motor/stop-all")
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert bridge.sent_commands == [
            {"cmd": "stop"},
            {"cmd": "dc", "dir": "stop"},
            {"cmd": "assist", "dir": "stop"},
            {"cmd": "rot", "dir": "stop"},
        ]
        assert set(r.json()["results"]) == {"step", "assist", "rot", "dc"}

    def test_stops_a_running_all_motor_feed_and_cancels_watcher(self, api, monkeypatch):
        client, bridge = api
        monkeypatch.setattr(main, "FEED_SYNC_WAIT_MIN_TIMEOUT_S", 30.0)
        assert _feed(client, **COMBOS["all_with_dc"][0]).status_code == 200
        task = main._sync_watcher_task
        bridge.sent_commands.clear()
        assert client.post("/motor/stop-all").status_code == 200
        assert _wait_for(task.done)
        assert {"cmd": "stop"} in bridge.sent_commands
        assert {"cmd": "assist", "dir": "stop"} in bridge.sent_commands
        assert {"cmd": "rot", "dir": "stop"} in bridge.sent_commands
        assert {"cmd": "dc", "dir": "stop"} in bridge.sent_commands

    def test_one_channel_failing_does_not_block_the_others_and_is_never_5xx(self, api):
        client, bridge = api
        bridge.raise_on = {("assist", "stop"), ("dc", "stop")}
        r = client.post("/motor/stop-all")
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["results"]["assist"]["sent"] is False and "error" in body["results"]["assist"]
        assert {"cmd": "rot", "dir": "stop"} in bridge.sent_commands  # assist patlamasına rağmen
        assert {"cmd": "dc", "dir": "stop"} in bridge.sent_commands  # sonuncu da denendi

    def test_not_connected_is_200_with_success_false(self, api):
        client, bridge = api
        bridge.connected_for_send = False
        r = client.post("/motor/stop-all")
        assert r.status_code == 200 and r.json()["success"] is False

    def test_old_stop_endpoint_unchanged(self, api):
        client, bridge = api
        assert client.post("/motor/stop").status_code == 200
        assert bridge.sent_commands == [{"cmd": "stop"}]


# --------------------------------------------------------------------------
class TestMotorAssistEndpoint:
    def test_cw_sends_computed_delay(self, api):
        client, bridge = api
        r = client.post("/motor/assist", json={"dir": "cw", "speed_mms": 10.0})
        assert r.status_code == 200, r.text
        assert bridge.sent_commands == [{"cmd": "assist", "dir": "cw", "delay": ASSIST_10_MMS_DELAY}]
        body = r.json()
        assert body["sent"] is True and body["assist_calc"]["delay_us"] == ASSIST_10_MMS_DELAY

    def test_ccw_and_stop(self, api):
        client, bridge = api
        client.post("/motor/assist", json={"dir": "ccw", "speed_mms": 50.0})
        r = client.post("/motor/assist", json={"dir": "stop"})
        assert bridge.sent_commands == [
            {"cmd": "assist", "dir": "ccw", "delay": 255},
            {"cmd": "assist", "dir": "stop"},
        ]
        assert "assist_calc" not in r.json()

    @pytest.mark.parametrize(
        "body",
        [
            {"dir": "cw"},  # hız eksik
            {"dir": "cw", "speed_mms": 0},
            {"dir": "cw", "speed_mms": -5},
            {"dir": "cw", "speed_mms": 1000.0},
            {"dir": "ccw", "speed_mms": 0.05},
            {"dir": "sideways", "speed_mms": 10.0},
            {"dir": "forward", "speed_mms": 10.0},
            {"dir": "cw", "speed_mms": "abc"},
            {"speed_mms": 10.0},  # dir eksik
        ],
    )
    def test_invalid_requests_4xx_and_hardware_untouched(self, api, body):
        client, bridge = api
        r = client.post("/motor/assist", json=body)
        assert r.status_code in (400, 422), r.text
        assert bridge.sent_commands == []

    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
    def test_nan_infinity_never_500(self, api, token):
        client, bridge = api
        r = client.post("/motor/assist", content='{"dir":"cw","speed_mms":%s}' % token, headers={"Content-Type": "application/json"})
        assert 400 <= r.status_code < 500
        assert bridge.sent_commands == []

    def test_range_error_message(self, api):
        client, _ = api
        d = client.post("/motor/assist", json={"dir": "cw", "speed_mms": 1000.0}).json()["detail"]
        assert "Yardımcı çift" in d and "min ~0.213 mm/s" in d and "max ~127.6 mm/s" in d

    def test_serial_error_becomes_502(self, api):
        client, bridge = api
        bridge.raise_on = {("assist", "cw")}
        r = client.post("/motor/assist", json={"dir": "cw", "speed_mms": 10.0})
        assert r.status_code == 502 and "STM32 ile haberleşme hatası" in r.json()["detail"]

    def test_not_connected_returns_sent_false_not_error(self, api):
        client, bridge = api
        bridge.connected_for_send = False
        r = client.post("/motor/assist", json={"dir": "cw", "speed_mms": 10.0})
        assert r.status_code == 200 and r.json()["sent"] is False

    def test_rot_endpoint_rejects_infinity_with_4xx_too(self, api):
        client, bridge = api
        r = client.post("/motor/rot", content='{"dir":"cw","rpm":Infinity}', headers={"Content-Type": "application/json"})
        assert 400 <= r.status_code < 500 and bridge.sent_commands == []

    def test_rot_range_message_names_rotation_pair(self, api):
        client, _ = api
        d = client.post("/motor/rot", json={"dir": "cw", "rpm": 5000.0}).json()["detail"]
        assert "Rotasyon çifti" in d and "NEMA17 RPM dönüşüm hatası" in d


# --------------------------------------------------------------------------
class TestMotionParamsAndLimitsEndpoints:
    def test_get_has_assist_defaults(self, api):
        client, _ = api
        p = client.get("/motion-params").json()["params"]
        assert p["D_wheel_assist_mm"] == 13.0 and p["ASSIST_PPR"] == 3200 and p["assist_dir_invert"] is False

    def test_admin_save_persists_across_requests(self, api):
        client, _ = api
        r = client.post("/motion-params", json={"D_wheel_assist_mm": 15.0, "ASSIST_PPR": 1600, "assist_dir_invert": True})
        assert r.status_code == 200 and r.json()["success"] is True
        p = client.get("/motion-params").json()["params"]
        assert (p["D_wheel_assist_mm"], p["ASSIST_PPR"], p["assist_dir_invert"]) == (15.0, 1600, True)
        # invert tekrar kapatılabiliyor (False "boş" sayılıp atlanmıyor)
        client.post("/motion-params", json={"assist_dir_invert": False})
        assert client.get("/motion-params").json()["params"]["assist_dir_invert"] is False
        # diğer alanlar korunmuş
        assert client.get("/motion-params").json()["params"]["ASSIST_PPR"] == 1600

    @pytest.mark.parametrize("ppr", [0, 100, 1000, 12800])
    def test_invalid_assist_ppr_is_400_and_not_saved(self, api, ppr):
        client, _ = api
        r = client.post("/motion-params", json={"ASSIST_PPR": ppr})
        assert r.status_code == 400 and "200, 400, 800, 1600, 3200, 6400" in r.json()["detail"]
        assert client.get("/motion-params").json()["params"]["ASSIST_PPR"] == 3200

    @pytest.mark.parametrize("body", [{"D_wheel_assist_mm": 0}, {"D_wheel_assist_mm": -3}, {"assist_dir_invert": "maybe"}, {"ASSIST_PPR": "abc"}])
    def test_invalid_values_422(self, api, body):
        client, _ = api
        assert client.post("/motion-params", json=body).status_code == 422

    @pytest.mark.parametrize("field", ["D_wheel_assist_mm", "D_drive_mm", "D_rod_mm", "RPM_MAX_NOLOAD", "D_wheel_rot_mm", "D_wheel_dc_mm"])
    def test_infinity_diameters_rejected_4xx(self, api, field):
        client, _ = api
        r = client.post("/motion-params", content='{"%s":Infinity}' % field, headers={"Content-Type": "application/json"})
        assert 400 <= r.status_code < 500

    def test_limits_endpoint_defaults(self, api):
        client, _ = api
        lim = client.get("/motion-limits").json()["limits"]
        assert lim["assist_mms"] == {"min": 0.213, "max": 127.627}
        assert lim["rot_rpm"] == {"min": 1.626, "max": 975.0}
        assert lim["dc_rpm"] == {"min": 5.2, "max": 520.0}
        assert lim["step_mms"] == {"min": 0.107, "max": 319.068}
        assert lim["distance_mm"]["max"] == 100000

    def test_limits_follow_admin_parameters(self, api):
        client, _ = api
        before = client.get("/motion-limits").json()["limits"]["assist_mms"]
        client.post("/motion-params", json={"D_wheel_assist_mm": 26.0})
        after = client.get("/motion-limits").json()["limits"]["assist_mms"]
        assert after["max"] == pytest.approx(before["max"] * 2, rel=0.01)

    def test_limit_edges_are_accepted_by_feed_start(self, api):
        client, bridge = api
        lim = client.get("/motion-limits").json()["limits"]
        for key, field in (("assist_mms", "assist_speed_mms"), ("rot_rpm", "rot_rpm"), ("dc_rpm", "dc_rpm")):
            use = {"assist_mms": "use_assist", "rot_rpm": "use_rot", "dc_rpm": "use_dc"}[key]
            for edge in ("min", "max"):
                r = _feed(client, **{use: True, field: lim[key][edge]})
                assert r.status_code == 200, (key, edge, r.text)
                bridge.finish_step()
                main._sync_watcher_task and main._sync_watcher_task.cancel()


# --------------------------------------------------------------------------
class TestStatusStream:
    def test_assist_and_adelay_reach_the_client(self, api):
        client, bridge = api
        bridge.status = {"running": 0, "assist": 2, "adelay": 1276, "rot": 1, "rdelay": 975, "dc": 1, "dcSpeed": 2}
        with client.websocket_connect("/ws/status") as ws:
            msg = ws.receive_json()
        assert msg["status"]["assist"] == 2 and msg["status"]["adelay"] == 1276
        assert msg["status"]["rot"] == 1 and msg["status"]["dc"] == 1

    def test_motion_block_present_with_expected_keys(self, api):
        client, bridge = api
        with client.websocket_connect("/ws/status") as ws:
            msg = ws.receive_json()
        motion = msg["motion"]
        assert {"state", "diag", "reason", "running", "enc1_cps", "enc2_cps", "stall_timeout_s"} <= set(motion)

    def test_motion_lamp_goes_green_then_red_through_the_background_loop(self, api, monkeypatch):
        """Arka plan döngüsü gerçekten besleniyor: encoder akarsa yeşil, running=1 ama durağansa kırmızı."""
        client, bridge = api
        import motion_monitor

        monkeypatch.setattr(motion_monitor.motion_monitor, "stall_timeout_s", 0.3)
        monkeypatch.setattr(motion_monitor.motion_monitor, "window_s", 0.3)
        assert _feed(client, **COMBOS["only_nema23"][0]).status_code == 200  # running:1
        stop = threading.Event()

        def spin():
            n = 0
            while not stop.is_set():
                n += 5
                bridge.status = {**bridge.status, "e1": n, "e2": n}
                time.sleep(0.02)

        t = threading.Thread(target=spin, daemon=True)
        t.start()
        try:
            def state():
                with client.websocket_connect("/ws/status") as ws:
                    return ws.receive_json()["motion"]["state"]

            assert _wait_for(lambda: state() == "moving", timeout=3)
        finally:
            stop.set()
            t.join()
        assert _wait_for(lambda: state() == "stalled", timeout=3)

    def test_failed_step_command_is_visible_as_not_sent(self, api):
        client, bridge = api
        bridge.connected_for_send = False
        _feed(client, **COMBOS["only_nema23"][0])
        import motion_monitor

        # Sahte köprü bağlı görünüyor ama komutu yazamıyor (sent:false): HATA kaydı + "komut gitmedi" nedeni
        snap = motion_monitor.motion_monitor.snapshot(time.monotonic())
        assert snap["diag"] == "fault" and "bağlı değil" in snap["reason"]
        motion_monitor.motion_monitor.note_stop()
        motion_monitor.motion_monitor.note_command(False, "kontrol kartı bağlı değil", time.monotonic())
        assert motion_monitor.motion_monitor.snapshot(time.monotonic())["diag"] == "cmd_not_sent"

    def test_stop_clears_not_sent_diag(self, api):
        client, bridge = api
        bridge.connected_for_send = False
        _feed(client, **COMBOS["only_nema23"][0])
        client.post("/motor/stop-all")
        import motion_monitor

        assert motion_monitor.motion_monitor.snapshot(time.monotonic())["diag"] == "idle"


class TestValidationErrorBody:
    def test_422_body_is_json_safe_and_has_no_input_echo(self, api):
        client, _ = api
        r = client.post("/motion-params", json={"D_wheel_assist_mm": -3})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert isinstance(detail, list) and detail[0]["loc"][-1] == "D_wheel_assist_mm"
        assert all(set(e) == {"loc", "msg", "type"} for e in detail)  # girdi değeri yok

    def test_nan_in_422_does_not_become_500(self, api):
        client, _ = api
        r = client.post("/motor/rot", content='{"dir":"cw","rpm":NaN}', headers={"Content-Type": "application/json"})
        assert r.status_code == 422


class TestRealBridgeParsesAssistFields:
    def test_listen_stores_assist_and_adelay(self):
        from serial_bridge import STM32Bridge

        class OneLineSerial:
            def __init__(self):
                self._lines = [
                    '{"t":5,"e1":10,"e2":11,"um1":1,"um2":1,"remaining":0,"running":0,"dc":0,"dcSpeed":0,'
                    '"rot":1,"rdelay":975,"assist":2,"adelay":1276}'
                ]

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
        assert _wait_for(lambda: b.get_status().get("assist") == 2, timeout=1.0)
        b._running = False
        t.join(timeout=1.0)
        status = b.get_status()
        assert status["adelay"] == 1276 and status["rot"] == 1 and status["rdelay"] == 975

    def test_assist_ok_and_err_replies_are_replies_not_status(self):
        """{"ok":"assist"} / {"err":"assist timer fault"} durum satırı değil, komut yanıtıdır."""
        from serial_bridge import STM32Bridge

        lines = ['{"ok":"assist"}', '{"err":"assist timer fault"}', '{"err":"missing delay field"}', '{"err":"invalid dir"}']

        class Serial:
            def __init__(self):
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

        b = STM32Bridge()
        b._serial = Serial()
        b._running = True
        t = threading.Thread(target=b._listen, daemon=True)
        t.start()
        assert _wait_for(lambda: b._last_reply == {"err": "invalid dir"}, timeout=1.0)
        b._running = False
        t.join(timeout=1.0)
        assert b.get_status() == {}  # yanıtlar durum alanına karışmadı


# --------------------------------------------------------------------------
class TestWatcherHandoverWithAssist:
    @pytest.fixture
    def recording_watcher(self, monkeypatch):
        calls = []

        async def fake_watcher(max_wait_s, **kwargs):
            calls.append(kwargs)
            await asyncio.Event().wait()

        monkeypatch.setattr(main, "_sync_watcher", fake_watcher)
        return calls

    def _cmd(self, **kw):
        base = dict(dir=0, speed_mms=5.0, distance_mm=100.0, accel_mms2=0.0, use_assist=False, use_rot=False, use_dc=False)
        base.update(kw)
        return main.FeedStartCommand(**base)

    def _run(self, monkeypatch, *commands):
        monkeypatch.setattr(main, "bridge", FakeBridge())

        async def run():
            main._main_event_loop = asyncio.get_running_loop()
            for cmd in commands:
                main.motor_feed_start(cmd)
            await asyncio.sleep(0.02)
            if main._sync_watcher_task is not None:
                main._sync_watcher_task.cancel()

        asyncio.run(run())

    def test_assist_only_passes_stop_assist(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, self._cmd(use_assist=True, assist_speed_mms=10.0))
        assert recording_watcher == [{"stop_dc": False, "stop_rot": False, "stop_assist": True}]

    def test_all_passes_all_three(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, self._cmd(use_assist=True, use_rot=True, use_dc=True, assist_speed_mms=10.0, rot_rpm=10.0, dc_rpm=10.0))
        assert recording_watcher == [{"stop_dc": True, "stop_rot": True, "stop_assist": True}]

    def test_nema23_only_after_assist_session_inherits_assist_stop(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        """Önceki oturum yardımcı çifti başlattı, yeni oturum yalnızca NEMA23: assist kontrolsüz kalmamalı."""
        self._run(
            monkeypatch,
            self._cmd(use_assist=True, assist_speed_mms=10.0),
            self._cmd(),
        )
        assert len(recording_watcher) == 2  # iki oturum da gerçekten watcher başlattı (devir ölçülüyor)
        assert recording_watcher[0] == {"stop_dc": False, "stop_rot": False, "stop_assist": True}
        assert recording_watcher[1] == {"stop_dc": False, "stop_rot": False, "stop_assist": True}

    def test_legacy_dc_only_still_calls_watcher_without_arguments(self, isolated_motion_params_config, monkeypatch, recording_watcher):
        self._run(monkeypatch, main.FeedStartCommand(dir=1, speed_mms=5.0, distance_mm=100.0, rpm=10.0))
        assert recording_watcher == [{}]


class TestWatcherFunctionStopAssist:
    def test_stop_assist_flag(self, monkeypatch):
        bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.1, stop_dc=False, stop_rot=False, stop_assist=True))
        assert bridge.sent_commands == [{"cmd": "assist", "dir": "stop"}]

    def test_default_does_not_touch_assist(self, monkeypatch):
        bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.1))
        assert bridge.sent_commands == [{"cmd": "dc", "dir": "stop"}]

    def test_assist_stop_failure_does_not_block_others(self, monkeypatch):
        bridge = FakeBridge()
        bridge.raise_on = {("assist", "stop")}
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.1, stop_dc=True, stop_rot=True, stop_assist=True))
        assert {"cmd": "dc", "dir": "stop"} in bridge.sent_commands
        assert {"cmd": "rot", "dir": "stop"} in bridge.sent_commands


class TestLegacyFeedStartStillWorks:
    """Eski biçim (rot_motor + ortak rpm) geriye uyumlu; yeni alanlarla birleşince 400."""

    def test_default_is_dc_only(self, api):
        client, bridge = api
        r = _feed(client, rpm=10.0)
        assert r.status_code == 200, r.text
        assert bridge.names() == ["step", "dc"]
        assert r.json()["assist_calc"] is None and r.json()["assist_result"] is None

    @pytest.mark.parametrize("mode, expected", [("nema17", ["step", "rot"]), ("both", ["step", "rot", "dc"])])
    def test_modes(self, api, mode, expected):
        client, bridge = api
        extra = {"dc_speed_pct": 30} if mode == "both" else {}
        assert _feed(client, rot_motor=mode, rpm=100.0, **extra).status_code == 200
        assert bridge.names() == expected

    def test_missing_rpm_in_legacy_form_is_4xx(self, api):
        client, bridge = api
        r = _feed(client)  # use_* da yok, rpm de yok
        assert r.status_code == 422 and bridge.sent_commands == []


# ==========================================================================
#  HATA YOLU (21-10-2026, Fatih kararı): bir hata olursa TÜM motorlar durur,
#  sebep konsol satırında / uyarı kutusunda / sistem log'unda açıkça yazar.
# ==========================================================================
STOP_ALL_SEQUENCE = [
    {"cmd": "stop"},
    {"cmd": "dc", "dir": "stop"},
    {"cmd": "assist", "dir": "stop"},
    {"cmd": "rot", "dir": "stop"},
]
ALL_MOTORS = COMBOS["all_with_dc"][0]


def _stops(bridge):
    """Gönderilen durdurma komutları (başlatmalar hariç)."""
    return [c for c in bridge.sent_commands if c.get("cmd") == "stop" or c.get("dir") == "stop"]


def _all_four_stopped(bridge):
    s = _stops(bridge)
    return all(cmd in s for cmd in STOP_ALL_SEQUENCE)


def _motion(client):
    with client.websocket_connect("/ws/status") as ws:
        return ws.receive_json()["motion"]


class TestFeedStartFaultStopsEverything:
    @pytest.mark.parametrize("channel, field", [("assist", "assist"), ("rot", "rot"), ("dc", "dc")])
    @pytest.mark.parametrize("kind", ["err", "timeout", "unsent"])
    def test_aux_failure_stops_all_four_channels(self, api, channel, field, kind, caplog):
        client, bridge = api
        if kind == "err":
            bridge.reply_for[channel] = {"err": "invalid dir"}
        elif kind == "timeout":
            bridge.timeout_for.add(channel)
        else:
            bridge.unsent_for.add(channel)
        with caplog.at_level("ERROR", logger="feedvision.motor_fault"):
            r = _feed(client, **ALL_MOTORS)
        assert r.status_code == 200, r.text  # 5xx yok
        body = r.json()
        assert body["success"] is False and body["failed_motor"] == channel
        assert _all_four_stopped(bridge)
        assert main._sync_watcher_task is None
        line = body["fault"]["line"]
        assert line.startswith("[HATA] Tüm motorlar durduruldu. Sebep: ")
        assert main._MOTOR_NAMES[channel] in line
        expected_reason = {"err": "invalid dir", "timeout": "timeout", "unsent": "bağlı değil"}[kind]
        assert expected_reason in line
        # sistem log'una aynı satır
        assert any(rec.getMessage() == line for rec in caplog.records)
        # sonraki motorlar hiç başlatılmadı
        order = ["assist", "rot", "dc"]
        for later in order[order.index(channel) + 1:]:
            assert [c for c in bridge.sent_commands if c.get("cmd") == later and c.get("dir") != "stop"] == []

    def test_example_console_line_for_assist_timer_fault(self, api):
        client, bridge = api
        bridge.reply_for["assist"] = {"err": "assist timer fault"}
        line = _feed(client, **ALL_MOTORS).json()["fault"]["line"]
        assert line == "[HATA] Tüm motorlar durduruldu. Sebep: Yardımcı çift (assist): {'err': 'assist timer fault'}"

    @pytest.mark.parametrize("channel", ["assist", "rot", "dc"])
    def test_aux_exception_is_502_with_reason_and_all_stopped(self, api, channel):
        client, bridge = api
        direction = {"assist": "cw", "rot": "cw", "dc": "forward"}[channel]
        bridge.raise_on = {(channel, direction)}
        r = _feed(client, **ALL_MOTORS)
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert "[HATA] Tüm motorlar durduruldu. Sebep: " + main._MOTOR_NAMES[channel] in detail
        assert "seri port yanit vermedi" in detail
        assert _all_four_stopped(bridge) and main._sync_watcher_task is None

    @pytest.mark.parametrize("kind", ["err", "timeout", "unsent"])
    def test_step_failure_nothing_started_so_no_stop_but_fault_recorded(self, api, kind):
        client, bridge = api
        if kind == "err":
            bridge.reply_for["step"] = {"err": "steps must be > 0"}
        elif kind == "timeout":
            bridge.timeout_for.add("step")
        else:
            bridge.unsent_for.add("step")
        r = _feed(client, **ALL_MOTORS)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False and body["stage"] == "step"
        assert body["fault"]["line"].startswith("[HATA] Besleme başlatılamadı, hiçbir motor başlatılmadı. Sebep: NEMA23 step: ")
        assert bridge.names() == ["step"]  # hiçbir şey başlamadı, durdurma gerekmedi
        m = _motion(client)
        assert m["state"] == "fault" and m["fault"]["line"] == body["fault"]["line"]

    def test_step_exception_502_with_reason(self, api):
        client, bridge = api
        bridge.raise_on = {("step", 0)}
        r = _feed(client, **ALL_MOTORS)
        assert r.status_code == 502 and "Sebep: NEMA23 step: seri port hatası" in r.json()["detail"]
        assert bridge.names() == ["step"]

    def test_one_stop_failing_does_not_stop_the_others_and_is_listed(self, api):
        client, bridge = api
        bridge.reply_for["rot"] = {"err": "x"}
        bridge.raise_on = {("dc", "stop")}
        r = _feed(client, **ALL_MOTORS)
        assert r.status_code == 200
        assert all(cmd in bridge.sent_commands for cmd in STOP_ALL_SEQUENCE)  # dc stop patlasa da rot stop dahil hepsi denendi
        line = r.json()["fault"]["line"]
        assert "durdurma komutu gitmeyen kanallar: DC" in line

    def test_state_lamp_and_banner_data_persist_until_stop(self, api):
        client, bridge = api
        bridge.timeout_for.add("rot")
        line = _feed(client, **ALL_MOTORS).json()["fault"]["line"]
        m = _motion(client)
        assert m["state"] == "fault" and m["diag"] == "fault" and m["reason"] == line
        assert m["fault"]["id"] >= 1 and m["fault"]["stopped_all"] is True
        assert _motion(client)["fault"]["line"] == line  # tekrar bakınca da duruyor
        assert client.post("/motor/stop-all").status_code == 200  # operatörün Dur'u
        assert _motion(client)["fault"] is None
        assert _motion(client)["state"] != "fault"

    def test_successful_new_feed_clears_previous_fault(self, api):
        client, bridge = api
        bridge.timeout_for.add("dc")
        _feed(client, **ALL_MOTORS)
        assert _motion(client)["fault"] is not None
        bridge.timeout_for.clear()
        bridge.finish_step()
        assert _feed(client, **ALL_MOTORS).json()["success"] is True
        assert _motion(client)["fault"] is None

    def test_each_new_fault_gets_a_new_id_so_console_logs_once_per_fault(self, api):
        client, bridge = api
        bridge.timeout_for.add("assist")
        _feed(client, **ALL_MOTORS)
        first = _motion(client)["fault"]["id"]
        client.post("/motor/stop-all")
        bridge.finish_step()
        _feed(client, **ALL_MOTORS)
        assert _motion(client)["fault"]["id"] == first + 1


class TestIndependentPanelsFault:
    @pytest.mark.parametrize(
        "channel, endpoint, body",
        [
            ("assist", "/motor/assist", {"dir": "cw", "speed_mms": 10.0}),
            ("rot", "/motor/rot", {"dir": "cw", "rpm": 10.0}),
            ("dc", "/motor/dc", {"dir": "forward", "speed": 50}),
            ("step", "/motor/step", {"dir": 0, "delay": 500, "steps": 100}),
        ],
    )
    @pytest.mark.parametrize("kind", ["err", "timeout"])
    def test_err_or_timeout_stops_all_four(self, api, channel, endpoint, body, kind, caplog):
        client, bridge = api
        if kind == "err":
            bridge.reply_for[channel] = {"err": "invalid dir"}
        else:
            bridge.timeout_for.add(channel)
        with caplog.at_level("ERROR", logger="feedvision.motor_fault"):
            r = client.post(endpoint, json=body)
        assert r.status_code == 200, r.text  # 5xx yok
        fault = r.json()["fault"]
        assert fault["line"].startswith("[HATA] Tüm motorlar durduruldu. Sebep: " + main._MOTOR_NAMES[channel] + ": ")
        assert ("invalid dir" if kind == "err" else "timeout") in fault["line"]
        assert _all_four_stopped(bridge)
        assert any(rec.getMessage() == fault["line"] for rec in caplog.records)
        assert _motion(client)["state"] == "fault"

    @pytest.mark.parametrize(
        "channel, endpoint, body, raise_key",
        [
            ("assist", "/motor/assist", {"dir": "cw", "speed_mms": 10.0}, ("assist", "cw")),
            ("rot", "/motor/rot", {"dir": "cw", "rpm": 10.0}, ("rot", "cw")),
            ("dc", "/motor/dc", {"dir": "forward", "speed": 50}, ("dc", "forward")),
            ("step", "/motor/step", {"dir": 0, "delay": 500, "steps": 100}, ("step", 0)),
        ],
    )
    def test_exception_is_502_with_reason_and_stops_all(self, api, channel, endpoint, body, raise_key):
        client, bridge = api
        bridge.raise_on = {raise_key}
        r = client.post(endpoint, json=body)
        assert r.status_code == 502
        assert "Sebep: " + main._MOTOR_NAMES[channel] + ": seri port hatası" in r.json()["detail"]
        assert _all_four_stopped(bridge)

    @pytest.mark.parametrize(
        "endpoint, body",
        [
            ("/motor/assist", {"dir": "stop"}),
            ("/motor/rot", {"dir": "stop"}),
            ("/motor/dc", {"dir": "stop"}),
        ],
    )
    def test_stop_commands_never_trigger_the_fault_path(self, api, endpoint, body):
        client, bridge = api
        for name in ("assist", "rot", "dc"):
            bridge.reply_for[name] = {"err": "x"}  # durdurmalara uygulanmaz ama kural açık olsun
        r = client.post(endpoint, json=body)
        assert r.status_code == 200 and "fault" not in r.json()
        assert len(bridge.sent_commands) == 1  # tek komut, stop-all zinciri yok

    def test_stop_command_exception_is_plain_502_not_a_fault_loop(self, api):
        client, bridge = api
        bridge.raise_on = {("rot", "stop")}
        r = client.post("/motor/rot", json={"dir": "stop"})
        assert r.status_code == 502 and "[HATA]" not in r.json()["detail"]
        assert len(bridge.sent_commands) == 1

    def test_not_connected_is_not_a_fault(self, api):
        client, bridge = api
        bridge.connected_for_send = False
        r = client.post("/motor/assist", json={"dir": "cw", "speed_mms": 10.0})
        assert r.status_code == 200 and r.json()["sent"] is False and "fault" not in r.json()
        assert len(bridge.sent_commands) == 1

    def test_ok_reply_is_not_a_fault(self, api):
        client, bridge = api
        r = client.post("/motor/dc", json={"dir": "forward", "speed": 50})
        assert r.status_code == 200 and "fault" not in r.json()
        assert _motion(client)["fault"] is None


class TestBackgroundFaultSources:
    def test_firmware_fault_indicator_in_status_line_stops_all(self, api, caplog):
        client, bridge = api
        with caplog.at_level("ERROR", logger="feedvision.motor_fault"):
            bridge.status = {**bridge.status, "fault": 1}
            assert _wait_for(lambda: _all_four_stopped(bridge))
        m = _motion(client)
        assert m["state"] == "fault"
        assert "Sebep: Kontrol kartı: durum satırında hata göstergesi fault=1" in m["fault"]["line"]
        assert any("fault=1" in rec.getMessage() for rec in caplog.records)
        # aynı gösterge sürerken tekrar tekrar durdurma/log üretmez
        n = len(bridge.sent_commands)
        time.sleep(0.4)
        assert len(bridge.sent_commands) == n

    def test_status_field_zero_is_not_a_fault(self, api):
        client, bridge = api
        bridge.status = {**bridge.status, "fault": 0, "err": 0}
        time.sleep(0.4)
        assert bridge.sent_commands == [] and _motion(client)["fault"] is None

    def test_serial_loss_during_feed_stops_all_and_cancels_watcher(self, api, caplog):
        client, bridge = api
        assert _feed(client, **ALL_MOTORS).status_code == 200
        task = main._sync_watcher_task
        time.sleep(0.25)  # döngü "bağlı + besleme aktif" durumunu görsün
        bridge.sent_commands.clear()
        bridge.is_connected = False  # kablo çekildi
        assert _wait_for(lambda: _all_four_stopped(bridge))
        assert _wait_for(task.done)
        m = _motion(client)
        assert m["state"] == "fault"
        assert "Sebep: Seri bağlantı: besleme sürerken kontrol kartı bağlantısı koptu" in m["fault"]["line"]

    def test_serial_loss_when_idle_is_not_a_fault(self, api):
        client, bridge = api
        time.sleep(0.25)
        bridge.is_connected = False
        time.sleep(0.4)
        assert bridge.sent_commands == [] and _motion(client)["fault"] is None

    def test_monitor_loop_unexpected_exception_stops_all_once(self, api, monkeypatch):
        client, bridge = api
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("izleme patladı")

        monkeypatch.setattr(main.motion_monitor, "observe", boom)
        assert _wait_for(lambda: _all_four_stopped(bridge))
        time.sleep(0.4)
        assert calls["n"] > 2  # döngü yaşamaya devam ediyor
        assert len(_stops(bridge)) == 4  # ama hata yolu bir kez çalıştı (spam yok)
        m = main.motion_monitor.snapshot(time.monotonic())
        assert "Sebep: Hareket izleme döngüsü: beklenmeyen hata: izleme patladı" in m["fault"]["line"]

    def test_watcher_unexpected_exception_stops_all(self, monkeypatch):
        bridge = FakeBridge()
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.05)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)

        def broken_status():
            raise RuntimeError("durum okunamadı")

        monkeypatch.setattr(bridge, "get_status", broken_status)
        asyncio.run(main._sync_watcher(0.2, stop_dc=True, stop_rot=True, stop_assist=True))
        assert all(cmd in bridge.sent_commands for cmd in STOP_ALL_SEQUENCE)
        line = main.motion_monitor.snapshot(time.monotonic())["fault"]["line"]
        assert "Sebep: Besleme izleyicisi: beklenmeyen hata: durum okunamadı" in line

    def test_watcher_failsafe_timeout_stops_all_including_nema23(self, monkeypatch):
        bridge = FakeBridge()
        bridge.status = {"running": 1}  # hiç bitmiyor
        monkeypatch.setattr(main, "bridge", bridge)
        monkeypatch.setattr(main, "FEED_SYNC_ARM_TIMEOUT_S", 0.02)
        monkeypatch.setattr(main, "FEED_SYNC_POLL_INTERVAL_S", 0.01)
        asyncio.run(main._sync_watcher(0.05, stop_dc=True, stop_rot=False, stop_assist=True))
        assert all(cmd in bridge.sent_commands for cmd in STOP_ALL_SEQUENCE)  # NEMA23 `stop` dahil
        assert "Sebep: NEMA23 step: " in main.motion_monitor.snapshot(time.monotonic())["fault"]["line"]

    def test_fault_path_itself_never_raises(self, monkeypatch):
        bridge = FakeBridge()
        bridge.raise_on = {("stop", None), ("dc", "stop"), ("assist", "stop"), ("rot", "stop")}
        monkeypatch.setattr(main, "bridge", bridge)
        fault = main._trigger_fault("DC", "deneme")
        assert fault["line"].startswith("[HATA] Tüm motorlar durduruldu. Sebep: DC: deneme")
        assert len(bridge.sent_commands) == 4  # hepsi denendi


class TestEncoderStallIsNotAFault:
    def test_stall_lamp_red_but_no_automatic_stop(self, api, monkeypatch):
        client, bridge = api
        import motion_monitor

        monkeypatch.setattr(motion_monitor.motion_monitor, "stall_timeout_s", 0.3)
        monkeypatch.setattr(motion_monitor.motion_monitor, "window_s", 0.3)
        assert _feed(client, **COMBOS["nema23_assist"][0]).status_code == 200  # running:1, encoder sabit
        assert _wait_for(lambda: _motion(client)["state"] == "stalled", timeout=3)
        time.sleep(0.4)
        assert _stops(bridge) == []  # otomatik durdurma YOK
        assert _motion(client)["fault"] is None and _motion(client)["diag"] == "stalled"
