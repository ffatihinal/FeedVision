"""
FeedVision — yardımcı çift (assist): motion_calc.compute_assist_command,
serial_bridge.make_assist_command, kanal bazlı hız aralıkları ve motion_params
yeni alanları (D_wheel_assist_mm, ASSIST_PPR, assist_dir_invert). Saf mantık,
donanım yok; dosya-IO yapan testler izole config kullanır (21-10-2026).

Beklenen sayılar formülden elle hesaplanıp gömüldü:
  wheel_rpm = v / (pi * D) * 60 ; pps = wheel_rpm / 60 * ppr ; delay = round(1e6 / pps)
  D=13 mm, ppr=3200, v=10 mm/s -> pps 783.532 -> delay 1276
"""

import json
import math

import pytest

import motion_calc
import motion_params
import serial_bridge

D_WHEEL = 13.0
PPR = 3200


def _call(v, d=D_WHEEL, ppr=PPR):
    return motion_calc.compute_assist_command(speed_mms=v, d_wheel_assist_mm=d, ppr=ppr)


class TestKnownValues:
    def test_10_mms_defaults(self):
        r = _call(10.0)
        assert r["delay_us"] == 1276
        assert r["wheel_rpm"] == pytest.approx(14.691225516174953)
        assert r["pulses_per_s"] == pytest.approx(783.5320275293309)
        assert r["speed_pct_of_max"] == pytest.approx(7.835320275293309)

    def test_50_mms(self):
        assert _call(50.0)["delay_us"] == 255

    def test_ppr_6400_doubles_pulse_rate_halves_delay(self):
        assert _call(10.0, ppr=6400)["delay_us"] == 638

    def test_bigger_wheel_means_slower_pulses_for_same_surface_speed(self):
        # Tekerlek 2x büyük -> aynı yüzey hızı için tur/sn yarı -> gecikme 2x
        assert _call(10.0, d=26.0)["delay_us"] == pytest.approx(2 * _call(10.0)["delay_us"], abs=1)

    def test_formula_independent_check(self):
        v, d, ppr = 37.0, 13.0, 1600
        pps = (v / (math.pi * d)) * ppr  # tur/sn * darbe/tur
        assert _call(v, d, ppr)["delay_us"] == round(1_000_000 / pps)


class TestLimits:
    def test_constants_match_firmware_contract(self):
        # firmware ASSIST_MIN_DELAY_US / ASSIST_MAX_DELAY_US (docs/protocol.md)
        assert motion_calc.ASSIST_MIN_DELAY_US == 100
        assert motion_calc.ASSIST_MAX_DELAY_US == 60000

    def test_exact_boundaries_accepted(self):
        mm_per_pulse = math.pi * D_WHEEL / PPR
        assert _call(1_000_000 / 100 * mm_per_pulse)["delay_us"] == 100
        assert _call(1_000_000 / 60000 * mm_per_pulse)["delay_us"] == 60000

    def test_too_fast_rejected_not_clipped(self):
        with pytest.raises(ValueError, match="aralığın dışında"):
            _call(130.0)  # delay ~98 < 100

    def test_too_slow_rejected_not_clipped(self):
        with pytest.raises(ValueError, match="aralığın dışında"):
            _call(0.2)  # delay ~63500 > 60000

    def test_error_message_has_supported_range(self):
        with pytest.raises(ValueError) as e:
            _call(1000.0)
        assert "min ~0.213 mm/s" in str(e.value) and "max ~127.6 mm/s" in str(e.value)

    @pytest.mark.parametrize("v", [0, -1, -0.001, math.nan])
    def test_non_positive_or_nan_speed(self, v):
        with pytest.raises(ValueError, match="0'dan büyük"):
            _call(v)

    def test_infinite_speed_is_range_error_not_crash(self):
        with pytest.raises(ValueError):
            _call(math.inf)

    def test_tiny_speed_underflow_does_not_divide_by_zero(self):
        with pytest.raises(ValueError, match="aralığın dışında"):
            _call(1e-320)

    @pytest.mark.parametrize("kwargs", [{"d": 0}, {"d": -5}, {"ppr": 0}, {"ppr": -3200}])
    def test_bad_hardware_params(self, kwargs):
        with pytest.raises(ValueError):
            _call(10.0, **kwargs)

    @pytest.mark.parametrize("ppr", [200, 400, 800, 1600, 3200, 6400])
    def test_every_allowed_ppr_has_a_usable_range(self, ppr):
        lim = motion_calc.assist_speed_limits_mms(D_WHEEL, ppr)
        assert lim["min"] < lim["max"]
        # Sınırın içinde kalan uç değerler gerçekten kabul edilir
        assert _call(lim["min"], ppr=ppr)["delay_us"] <= motion_calc.ASSIST_MAX_DELAY_US
        assert _call(lim["max"], ppr=ppr)["delay_us"] >= motion_calc.ASSIST_MIN_DELAY_US


class TestChannelLimitsAreAccepted:
    """limits fonksiyonlarının verdiği uç değerler, ilgili compute_* tarafından
    KABUL edilmeli (arayüz min/max kutusuna bunları koyuyor)."""

    def test_step(self):
        lim = motion_calc.step_speed_limits_mms(52.0)
        for v in (lim["min"], lim["max"]):
            motion_calc.compute_step_command(speed_mms=v, distance_mm=10, accel_mms2=0, d_drive_mm=52.0)
        with pytest.raises(ValueError):
            motion_calc.compute_step_command(speed_mms=lim["max"] * 1.1, distance_mm=10, accel_mms2=0, d_drive_mm=52.0)

    def test_rot(self):
        lim = motion_calc.rot_rpm_limits(52.0, 10.0, 3200)
        for v in (lim["min"], lim["max"]):
            motion_calc.compute_rot_command(v, 52.0, 10.0, 3200)

    def test_dc(self):
        lim = motion_calc.dc_rpm_limits(52.0, 10.0, 100.0)
        for v in (lim["min"], lim["max"]):
            motion_calc.compute_dc_duty(v, 52.0, 10.0, 100.0)
        with pytest.raises(ValueError):
            motion_calc.compute_dc_duty(lim["max"] * 1.05, 52.0, 10.0, 100.0)

    def test_distance_bounds(self):
        lim = motion_calc.step_distance_limits_mm(52.0)
        motion_calc.compute_step_command(5.0, lim["min"], 0, 52.0)
        motion_calc.compute_step_command(5.0, lim["max"], 0, 52.0)
        with pytest.raises(ValueError, match="çok büyük"):
            motion_calc.compute_step_command(5.0, lim["max"] * 2, 0, 52.0)


class TestNoCrashOnWeirdNumbers:
    """Sunucu hiçbir sayısal girdide ValueError dışı istisna (=500) üretmemeli."""

    @pytest.mark.parametrize("v", [math.inf, -math.inf, math.nan, 1e308, 1e-308])
    def test_dc(self, v):
        try:
            motion_calc.compute_dc_duty(v, 52.0, 10.0, 100.0)
        except ValueError:
            pass

    @pytest.mark.parametrize("v", [math.inf, math.nan, 1e308, 1e-308])
    def test_step_distance(self, v):
        try:
            motion_calc.compute_step_command(5.0, v, 0, 52.0)
        except ValueError:
            pass

    @pytest.mark.parametrize("v", [math.inf, math.nan, 1e308, 1e-308])
    def test_step_speed(self, v):
        try:
            motion_calc.compute_step_command(v, 10.0, 0, 52.0)
        except ValueError:
            pass

    @pytest.mark.parametrize("v", [math.inf, math.nan, 1e308, 1e-308])
    def test_rot_and_assist(self, v):
        for fn in (lambda: motion_calc.compute_rot_command(v, 52.0, 10.0, 3200), lambda: _call(v)):
            try:
                fn()
            except ValueError:
                pass


# --------------------------------------------------------------------------
class TestMakeAssistCommand:
    def test_cw_ccw(self):
        assert serial_bridge.make_assist_command("cw", 2000) == {"cmd": "assist", "dir": "cw", "delay": 2000}
        assert serial_bridge.make_assist_command("ccw", 100) == {"cmd": "assist", "dir": "ccw", "delay": 100}

    def test_stop_needs_no_delay(self):
        assert serial_bridge.make_assist_command("stop") == {"cmd": "assist", "dir": "stop"}

    @pytest.mark.parametrize("delay", [99, 60001, 0, -5])
    def test_delay_out_of_range_rejected_not_clipped(self, delay):
        with pytest.raises(ValueError, match="aralık dışı"):
            serial_bridge.make_assist_command("cw", delay)

    @pytest.mark.parametrize("delay", [None, 1000.5, "1000", True])
    def test_delay_must_be_int(self, delay):
        with pytest.raises(ValueError, match="tam sayı"):
            serial_bridge.make_assist_command("cw", delay)

    @pytest.mark.parametrize("direction", ["forward", "CW", "", None])
    def test_bad_direction(self, direction):
        with pytest.raises(ValueError, match="geçersiz"):
            serial_bridge.make_assist_command(direction, 1000)

    def test_boundaries_accepted(self):
        assert serial_bridge.make_assist_command("cw", 100)["delay"] == 100
        assert serial_bridge.make_assist_command("cw", 60000)["delay"] == 60000


# --------------------------------------------------------------------------
class TestMotionParamsAssistFields:
    def test_defaults(self, isolated_motion_params_config):
        p = motion_params.get_params()
        assert p["D_wheel_assist_mm"] == 13.0
        assert p["ASSIST_PPR"] == 3200 and isinstance(p["ASSIST_PPR"], int)
        assert p["assist_dir_invert"] is False

    def test_old_file_without_new_fields_gets_defaults(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text(json.dumps({"D_drive_mm": 50.0}), encoding="utf-8")
        p = motion_params.get_params()
        assert p["D_drive_mm"] == 50.0
        assert p["D_wheel_assist_mm"] == 13.0 and p["ASSIST_PPR"] == 3200 and p["assist_dir_invert"] is False

    def test_save_roundtrip(self, isolated_motion_params_config):
        motion_params.save_params({"D_wheel_assist_mm": 15.5, "ASSIST_PPR": 6400, "assist_dir_invert": True})
        p = motion_params.get_params()
        assert (p["D_wheel_assist_mm"], p["ASSIST_PPR"], p["assist_dir_invert"]) == (15.5, 6400, True)
        saved = json.loads(isolated_motion_params_config.read_text(encoding="utf-8"))
        assert saved["assist_dir_invert"] is True  # JSON'da gerçek bool

    def test_partial_update_keeps_other_assist_fields(self, isolated_motion_params_config):
        motion_params.save_params({"D_wheel_assist_mm": 15.5, "ASSIST_PPR": 800})
        motion_params.save_params({"assist_dir_invert": True})
        p = motion_params.get_params()
        assert (p["D_wheel_assist_mm"], p["ASSIST_PPR"], p["assist_dir_invert"]) == (15.5, 800, True)

    @pytest.mark.parametrize("ppr", [0, 100, 1000, 12800, 3200.5, "3200", None, True])
    def test_invalid_ppr_ignored(self, isolated_motion_params_config, ppr):
        motion_params.save_params({"ASSIST_PPR": 800})
        motion_params.save_params({"ASSIST_PPR": ppr})
        assert motion_params.get_params()["ASSIST_PPR"] == 800

    @pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, "13", True, None])
    def test_invalid_diameter_ignored(self, isolated_motion_params_config, value):
        motion_params.save_params({"D_wheel_assist_mm": 20.0})
        motion_params.save_params({"D_wheel_assist_mm": value})
        assert motion_params.get_params()["D_wheel_assist_mm"] == 20.0

    @pytest.mark.parametrize("value", [1, 0, "true", "yes", None, 1.0])
    def test_invert_accepts_only_real_bool(self, isolated_motion_params_config, value):
        motion_params.save_params({"assist_dir_invert": True})
        motion_params.save_params({"assist_dir_invert": value})
        assert motion_params.get_params()["assist_dir_invert"] is True

    def test_invalid_values_in_file_fall_back_to_defaults(self, isolated_motion_params_config):
        isolated_motion_params_config.write_text(
            json.dumps({"ASSIST_PPR": 1234, "D_wheel_assist_mm": -2, "assist_dir_invert": "evet"}), encoding="utf-8"
        )
        p = motion_params.get_params()
        assert p["ASSIST_PPR"] == 3200 and p["D_wheel_assist_mm"] == 13.0 and p["assist_dir_invert"] is False

    def test_allowed_assist_ppr_table(self):
        assert motion_params.ALLOWED_ASSIST_PPR == (200, 400, 800, 1600, 3200, 6400)
