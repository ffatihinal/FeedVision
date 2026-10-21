"""
FeedVision — motion_monitor (hareket durum lambası mantığı) testleri.
Sahte saat: zaman `now` parametresiyle verilir, gerçek bekleme yok (21-10-2026).
"""

import pytest

from motion_monitor import CMD_FAILURE_TTL_S, MIN_COUNTS_FOR_MOTION, MotionMonitor


def _feed(m, t0, t1, running, e1_rate=0.0, e2_rate=0.0, dt=0.1, e1_0=0, e2_0=0, connected=True):
    """t0..t1 arası dt aralıkla durum örneği besler; encoder'lar sabit hızla (sayım/sn) artar."""
    t = t0
    while t <= t1 + 1e-9:
        status = {"running": 1 if running else 0, "e1": int(e1_0 + e1_rate * (t - t0)), "e2": int(e2_0 + e2_rate * (t - t0))}
        m.observe(status, connected, t)
        t = round(t + dt, 6)
    return t1


class TestStates:
    def test_idle_when_nothing_happens(self):
        m = MotionMonitor()
        _feed(m, 0, 3, running=False)
        s = m.snapshot(3)
        assert (s["state"], s["diag"], s["running"]) == ("idle", "idle", False)

    def test_moving_when_running_and_encoder_counts_change(self):
        m = MotionMonitor()
        _feed(m, 0, 2, running=True, e1_rate=100, e2_rate=100)
        s = m.snapshot(2)
        assert (s["state"], s["diag"]) == ("moving", "moving")
        assert s["enc1_cps"] == pytest.approx(100, rel=0.1)
        assert s["enc2_cps"] == pytest.approx(100, rel=0.1)

    def test_starting_then_stalled_when_running_but_no_motion(self):
        m = MotionMonitor(stall_timeout_s=1.5)
        _feed(m, 0, 1.0, running=True)
        assert m.snapshot(1.0)["state"] == "starting"  # süre dolmadı, henüz kırmızı değil
        _feed(m, 1.1, 1.6, running=True)
        s = m.snapshot(1.6)
        assert (s["state"], s["diag"]) == ("stalled", "stalled")

    def test_stall_countdown_starts_when_running_starts_not_at_boot(self):
        m = MotionMonitor(stall_timeout_s=1.5)
        _feed(m, 0, 10, running=False)  # uzun süre boşta
        _feed(m, 10.1, 10.5, running=True)
        assert m.snapshot(10.5)["state"] == "starting"

    def test_stall_after_motion_stops_midway(self):
        m = MotionMonitor(stall_timeout_s=1.5, window_s=1.5)
        _feed(m, 0, 2, running=True, e1_rate=200)
        assert m.snapshot(2)["state"] == "moving"
        # Encoder durdu (sayaç sabit), firmware hâlâ running=1
        last = 200 * 2
        _feed(m, 2.1, 6.0, running=True, e1_0=last, e2_0=0)
        assert m.snapshot(6.0)["state"] == "stalled"

    def test_green_with_slowest_supported_speed(self):
        # ~2.1 sayım/sn (NEMA23 en yavaş hız, 40mm teker): pencerede eşik aşılmalı
        m = MotionMonitor()
        _feed(m, 0, 3, running=True, e2_rate=2.1)
        assert m.snapshot(3)["state"] == "moving"

    def test_single_count_jitter_is_not_motion(self):
        m = MotionMonitor()
        t = 0.0
        while t < 3:
            m.observe({"running": 1, "e1": 5 + int(t * 10) % 2, "e2": 7}, True, t)
            t = round(t + 0.1, 6)
        assert MIN_COUNTS_FOR_MOTION >= 2
        assert m.snapshot(3)["state"] == "stalled"

    def test_motion_without_step_command_is_green_but_labelled(self):
        # rotasyon/yardımcı/DC çubuğu sürüyor, NEMA23 komutu yok
        m = MotionMonitor()
        _feed(m, 0, 2, running=False, e2_rate=50)
        s = m.snapshot(2)
        assert (s["state"], s["diag"]) == ("moving", "moving_no_step")

    def test_returns_to_idle_after_motion_ends(self):
        m = MotionMonitor(window_s=1.5)
        _feed(m, 0, 2, running=True, e1_rate=100)
        _feed(m, 2.1, 5, running=False, e1_0=200)
        assert m.snapshot(5)["state"] == "idle"

    def test_disconnected_is_idle_with_own_diag(self):
        m = MotionMonitor()
        _feed(m, 0, 1, running=True, e1_rate=100)
        m.observe({"running": 1, "e1": 999, "e2": 0}, False, 1.1)
        s = m.snapshot(1.1)
        assert (s["state"], s["diag"], s["running"]) == ("idle", "disconnected", False)

    def test_empty_status_treated_as_no_data(self):
        m = MotionMonitor()
        m.observe({}, True, 0.0)
        assert m.snapshot(0.0)["state"] == "idle"

    def test_missing_encoder_fields_do_not_crash(self):
        m = MotionMonitor()
        for i in range(30):
            m.observe({"running": 1}, True, i * 0.1)
        assert m.snapshot(3.0)["state"] == "stalled"  # hareket kanıtı yok -> kırmızı (komut var, kanıt yok)

    def test_garbage_encoder_values_ignored(self):
        m = MotionMonitor()
        m.observe({"running": 0, "e1": "x", "e2": None}, True, 0.0)
        assert m.snapshot(0.0)["state"] == "idle"


class TestCommandFailureDiag:
    def test_failed_command_shows_not_sent_with_reason(self):
        m = MotionMonitor()
        _feed(m, 0, 1, running=False)
        m.note_command(False, "kontrol kartı komuta yanıt vermedi", 1.0)
        s = m.snapshot(1.5)
        assert (s["state"], s["diag"]) == ("idle", "cmd_not_sent")
        assert s["reason"] == "kontrol kartı komuta yanıt vermedi"

    def test_successful_command_clears_failure(self):
        m = MotionMonitor()
        _feed(m, 0, 1, running=False)
        m.note_command(False, "x", 1.0)
        m.note_command(True, None, 2.0)
        assert m.snapshot(2.0)["diag"] == "idle"

    def test_stop_clears_failure(self):
        m = MotionMonitor()
        _feed(m, 0, 1, running=False)
        m.note_command(False, "x", 1.0)
        m.note_stop()
        assert m.snapshot(1.1)["diag"] == "idle"

    def test_failure_expires(self):
        m = MotionMonitor()
        _feed(m, 0, 1, running=False)
        m.note_command(False, "x", 1.0)
        assert m.snapshot(1.0 + CMD_FAILURE_TTL_S + 1)["diag"] == "idle"

    def test_reason_never_leaks_into_other_diags(self):
        m = MotionMonitor()
        m.note_command(False, "x", 0.0)
        _feed(m, 0.1, 2, running=True, e1_rate=100)
        s = m.snapshot(2)
        assert s["diag"] == "moving" and s["reason"] is None


class TestReset:
    def test_counter_reset_does_not_fake_motion(self):
        m = MotionMonitor()
        _feed(m, 0, 2, running=False, e1_0=5000)  # sabit
        m.reset_samples()  # reset komutu: sayaç 5000 -> 0
        _feed(m, 2.1, 4, running=False, e1_0=0)
        assert m.snapshot(4)["state"] == "idle"

    def test_without_reset_a_counter_jump_looks_like_motion(self):
        # Belgeleme testi: reset_samples'ın neden gerekli olduğunu gösterir
        m = MotionMonitor()
        _feed(m, 0, 1, running=False, e1_0=5000)
        _feed(m, 1.1, 1.5, running=False, e1_0=0)
        assert m.snapshot(1.5)["diag"] == "moving_no_step"
