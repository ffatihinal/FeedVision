"""
FeedVision — motion_calc.compute_rot_command testleri (NEMA17 dönüş ekseni,
TB6600). Saf matematik, dosya-IO yok.

Beklenen sayılar formülden (rpm_rod * D_rod / D_wheel_rot / 60 * ppr -> darbe/s
-> 1e6/darbe/s) elle hesaplanıp sabit olarak gömüldü (05-10-2026): varsayılan
D_wheel_rot=52, D_rod=10, ppr=3200.
"""

import math
import random

import pytest

import motion_calc

D_WHEEL = 52.0
D_ROD = 10.0
PPR = 3200


def _call(rpm, d_wheel=D_WHEEL, d_rod=D_ROD, ppr=PPR):
    return motion_calc.compute_rot_command(rpm_rod=rpm, d_wheel_rot_mm=d_wheel, d_rod_mm=d_rod, ppr=ppr)


class TestKnownValues:
    def test_rpm_100_defaults(self):
        # wheel_rpm = 100*10/52 = 19.2308 ; pps = 19.2308/60*3200 = 1025.641 ; delay = 975
        r = _call(100.0)
        assert r["delay_us"] == 975
        assert r["wheel_rpm"] == pytest.approx(19.230769230769234)
        assert r["pulses_per_s"] == pytest.approx(1025.6410256410256)
        assert r["speed_pct_of_max"] == pytest.approx(10.256410256410257)

    def test_rpm_10_defaults(self):
        # pps = 102.564 -> delay 9750
        assert _call(10.0)["delay_us"] == 9750

    def test_ppr_6400_doubles_pulse_rate(self):
        # Aynı RPM için darbe sayısı 2x -> gecikme yarıya iner (975 -> 487.5 -> 488 civarı)
        r = _call(100.0, ppr=6400)
        assert r["pulses_per_s"] == pytest.approx(2051.2820512820513)
        assert r["delay_us"] == round(1_000_000 / 2051.2820512820513)

    def test_wheel_to_rod_ratio_matters(self):
        # Tekerlek 2x büyük -> aynı çubuk RPM'i için tekerlek 2x yavaş, gecikme 2x
        small = _call(100.0, d_wheel=52.0)["delay_us"]
        big = _call(100.0, d_wheel=104.0)["delay_us"]
        assert big == pytest.approx(2 * small, abs=1)

    def test_equal_diameters_wheel_rpm_equals_rod_rpm(self):
        assert _call(60.0, d_wheel=10.0, d_rod=10.0)["wheel_rpm"] == pytest.approx(60.0)

    def test_speed_pct_matches_delay_ratio(self):
        r = _call(100.0)
        assert r["speed_pct_of_max"] == pytest.approx(motion_calc.ROT_MIN_DELAY_US / (1_000_000 / r["pulses_per_s"]) * 100)


class TestLimits:
    def test_constants_match_protocol_contract(self):
        # Firmware (main.c) ile aynı olmalı — protokol sözleşmesi 05-10-2026
        assert motion_calc.ROT_MIN_DELAY_US == 100
        assert motion_calc.ROT_MAX_DELAY_US == 60000

    def test_max_rpm_boundary_accepted(self):
        # delay=100 <-> pps=10000 <-> wheel_rpm=187.5 <-> rod_rpm=975
        r = _call(975.0)
        assert r["delay_us"] == 100
        assert r["speed_pct_of_max"] == pytest.approx(100.0)

    def test_above_max_rpm_rejected_with_range_message(self):
        with pytest.raises(ValueError, match="aralığın dışında") as exc:
            _call(1000.0)
        assert "max ~975.0 RPM" in str(exc.value)
        assert "min ~1.625 RPM" in str(exc.value)

    def test_min_rpm_boundary_accepted(self):
        # delay=60000 <-> pps=16.667 <-> rod_rpm=1.625
        assert _call(1.625)["delay_us"] == 60000

    def test_below_min_rpm_rejected_with_range_message(self):
        with pytest.raises(ValueError, match="aralığın dışında") as exc:
            _call(1.0)
        assert "min ~1.625 RPM" in str(exc.value)

    def test_range_message_follows_ppr(self):
        # ppr 2x -> desteklenen RPM aralığı yarıya iner
        with pytest.raises(ValueError) as exc:
            _call(1000.0, ppr=6400)
        assert "max ~487.5 RPM" in str(exc.value)

    def test_extreme_values_become_value_error_not_other_exception(self):
        for rpm in (1e-320, 5e-324, 1e300, float("inf")):
            with pytest.raises(ValueError, match="aralığın dışında"):
                _call(rpm)


class TestInputValidation:
    @pytest.mark.parametrize("rpm", [0.0, -1.0, float("nan")])
    def test_bad_rpm(self, rpm):
        with pytest.raises(ValueError, match="RPM"):
            _call(rpm)

    @pytest.mark.parametrize("d_wheel", [0.0, -5.0])
    def test_bad_wheel_diameter(self, d_wheel):
        with pytest.raises(ValueError, match="NEMA17 teker çapı"):
            _call(10.0, d_wheel=d_wheel)

    @pytest.mark.parametrize("d_rod", [0.0, -5.0])
    def test_bad_rod_diameter(self, d_rod):
        with pytest.raises(ValueError, match="çubuğu çapı"):
            _call(10.0, d_rod=d_rod)

    @pytest.mark.parametrize("ppr", [0, -3200])
    def test_bad_ppr(self, ppr):
        with pytest.raises(ValueError, match="ROT_PPR"):
            _call(10.0, ppr=ppr)


class TestFuzz:
    def test_result_is_valid_or_clean_value_error(self):
        rng = random.Random(20261005)
        accepted = rejected = 0
        for _ in range(3000):
            rpm = 10 ** rng.uniform(-3, 4)
            d_wheel = rng.uniform(5, 200)
            d_rod = rng.uniform(2, 50)
            ppr = rng.choice([200, 400, 800, 1600, 3200, 6400])
            # Bağımsız referans: formül + delay sınırı
            pps = rpm * d_rod / d_wheel / 60 * ppr
            expected_delay = round(1_000_000 / pps)
            in_range = motion_calc.ROT_MIN_DELAY_US <= expected_delay <= motion_calc.ROT_MAX_DELAY_US
            if in_range:
                r = _call(rpm, d_wheel, d_rod, ppr)
                accepted += 1
                assert r["delay_us"] == expected_delay
                assert motion_calc.ROT_MIN_DELAY_US <= r["delay_us"] <= motion_calc.ROT_MAX_DELAY_US
                assert math.isclose(r["pulses_per_s"], pps, rel_tol=1e-9)
                assert 0 < r["speed_pct_of_max"] <= 100 * (1 + 1e-2)
            else:
                rejected += 1
                with pytest.raises(ValueError, match="aralığın dışında"):
                    _call(rpm, d_wheel, d_rod, ppr)
        # Fuzz her iki dalı da gerçekten sınamış olmalı
        assert accepted > 200 and rejected > 200

    def test_delay_monotonic_decreasing_in_rpm(self):
        delays = [_call(rpm)["delay_us"] for rpm in (2, 5, 10, 50, 100, 500, 900)]
        assert delays == sorted(delays, reverse=True)
