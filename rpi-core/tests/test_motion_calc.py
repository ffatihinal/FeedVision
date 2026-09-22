"""
FeedVision — motion_calc.py testleri (bilinen giriş/çıkış çiftleri + sınır
değerler). Dosya-IO yok, saf matematik — fixture gerekmiyor.

Beklenen sayılar main.c sabitleriyle (STEP_MIN_DELAY_US=20, STEP_MAX_DELAY_US=
60000, STEP_RAMP_START_DELAY_US=2000) ve STEPS_PER_REV=25600 ile elle/python
ile önceden hesaplanıp buraya sabit olarak gömüldü (23-09-2026).
"""

import math

import pytest

import motion_calc


class TestMmPerStep:
    def test_known_value_d52(self):
        # pi * 52 / 25600
        assert motion_calc.mm_per_step(52.0) == pytest.approx(0.006381360077604268)

    def test_scales_linearly_with_diameter(self):
        assert motion_calc.mm_per_step(104.0) == pytest.approx(2 * motion_calc.mm_per_step(52.0))


class TestComputeStepCommand:
    def test_known_pair_speed5_distance100_accel50(self):
        # Python ile önceden hesaplandı (D_drive=52mm): steps=15671, delay_us=1276, accel_steps=23
        result = motion_calc.compute_step_command(speed_mms=5.0, distance_mm=100.0, accel_mms2=50.0, d_drive_mm=52.0)
        assert result["steps"] == 15671
        assert result["delay_us"] == 1276
        assert result["accel_steps"] == 23

    def test_speed_below_jumpstart_has_zero_accel_steps(self):
        # v_jumpstart ~3.19 mm/s (D=52) — 1 mm/s bunun altında, accel_mms2
        # verilse bile firmware zaten rampasız/direkt başlıyor demek.
        result = motion_calc.compute_step_command(speed_mms=1.0, distance_mm=50.0, accel_mms2=50.0, d_drive_mm=52.0)
        assert result["steps"] == 7835
        assert result["delay_us"] == 6381
        assert result["accel_steps"] == 0

    def test_zero_accel_means_no_ramp(self):
        result = motion_calc.compute_step_command(speed_mms=5.0, distance_mm=100.0, accel_mms2=0.0, d_drive_mm=52.0)
        assert result["accel_steps"] == 0

    def test_negative_accel_means_no_ramp(self):
        result = motion_calc.compute_step_command(speed_mms=5.0, distance_mm=100.0, accel_mms2=-1.0, d_drive_mm=52.0)
        assert result["accel_steps"] == 0

    def test_accel_steps_clamped_to_half_of_steps(self):
        # Çok küçük bir mesafe + çok yüksek hedef ivme: accel_steps ham
        # formülle steps/2'den büyük çıkmalı, floor(steps/2)'ye kırpılmalı.
        result = motion_calc.compute_step_command(speed_mms=5.0, distance_mm=0.1, accel_mms2=0.001, d_drive_mm=52.0)
        assert result["accel_steps"] <= result["steps"] // 2

    def test_speed_at_min_boundary_is_accepted(self):
        # min_speed ~0.10635600129340446 mm/s (delay_us tam 60000'e denk gelir)
        result = motion_calc.compute_step_command(speed_mms=0.10635600129340446, distance_mm=10.0, accel_mms2=0.0, d_drive_mm=52.0)
        assert result["delay_us"] == 60000

    def test_speed_just_below_min_boundary_rejected(self):
        with pytest.raises(ValueError, match="aralığın dışında"):
            motion_calc.compute_step_command(speed_mms=0.05, distance_mm=10.0, accel_mms2=0.0, d_drive_mm=52.0)

    def test_speed_at_max_boundary_is_accepted(self):
        # max_speed ~319.0680038802134 mm/s (delay_us tam 20'ye denk gelir)
        result = motion_calc.compute_step_command(speed_mms=319.0680038802134, distance_mm=1000.0, accel_mms2=0.0, d_drive_mm=52.0)
        assert result["delay_us"] == 20

    def test_speed_above_max_boundary_rejected(self):
        with pytest.raises(ValueError, match="aralığın dışında"):
            motion_calc.compute_step_command(speed_mms=1000.0, distance_mm=10.0, accel_mms2=0.0, d_drive_mm=52.0)

    def test_zero_speed_rejected(self):
        with pytest.raises(ValueError, match="Hız"):
            motion_calc.compute_step_command(speed_mms=0.0, distance_mm=10.0, accel_mms2=0.0, d_drive_mm=52.0)

    def test_negative_speed_rejected(self):
        with pytest.raises(ValueError, match="Hız"):
            motion_calc.compute_step_command(speed_mms=-5.0, distance_mm=10.0, accel_mms2=0.0, d_drive_mm=52.0)

    def test_zero_distance_rejected(self):
        with pytest.raises(ValueError, match="Mesafe"):
            motion_calc.compute_step_command(speed_mms=5.0, distance_mm=0.0, accel_mms2=0.0, d_drive_mm=52.0)

    def test_negative_distance_rejected(self):
        with pytest.raises(ValueError, match="Mesafe"):
            motion_calc.compute_step_command(speed_mms=5.0, distance_mm=-1.0, accel_mms2=0.0, d_drive_mm=52.0)

    def test_zero_wheel_diameter_rejected(self):
        with pytest.raises(ValueError, match="çapı"):
            motion_calc.compute_step_command(speed_mms=5.0, distance_mm=10.0, accel_mms2=0.0, d_drive_mm=0.0)

    def test_tiny_distance_rejected_when_below_one_microstep(self):
        # mm_per_step(52) ~0.00638mm — 0.0001mm bunun altında, round() 0 verir.
        with pytest.raises(ValueError, match="çok küçük"):
            motion_calc.compute_step_command(speed_mms=1.0, distance_mm=0.0001, accel_mms2=0.0, d_drive_mm=52.0)

    def test_larger_wheel_diameter_shifts_speed_range(self):
        # Daha büyük teker -> aynı delay_us aralığı için daha yüksek mm/s
        # aralığı (mm_per_step büyüdüğü için) — 0.05 mm/s D=52'de reddediliyordu,
        # D=520 (10x) ile artık min_speed de 10x büyüdüğü için YİNE reddedilir
        # ama üst sınır da 10x büyüdüğünden önceden kabul edilen bir orta hız
        # (ör. 50 mm/s) artık delay_us'i çok küçültüp geçerli kalmalı.
        result = motion_calc.compute_step_command(speed_mms=50.0, distance_mm=1000.0, accel_mms2=0.0, d_drive_mm=520.0)
        assert result["delay_us"] == round(1_000_000 * motion_calc.mm_per_step(520.0) / 50.0)


class TestComputeDcDuty:
    def test_known_pair_defaults(self):
        # D_wheel_dc=52, D_rod=10, RPM_MAX_NOLOAD=100 -> duty(10)=1.923 -> round 2
        result = motion_calc.compute_dc_duty(rpm_rod=10.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)
        assert result["duty"] == 2
        assert result["duty_exact"] == pytest.approx(1.9230769230769231)

    def test_known_pair_duty_exactly_10(self):
        result = motion_calc.compute_dc_duty(rpm_rod=52.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)
        assert result["duty"] == 10

    def test_max_reachable_rpm_gives_duty_100(self):
        result = motion_calc.compute_dc_duty(rpm_rod=520.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)
        assert result["duty"] == 100

    def test_rpm_above_max_reachable_rejected(self):
        # duty(525)=100.96 -> round 101 (521'de round 100'e iniyor, sınırda
        # kalıyor — gerçek "reddedilir" davranışını görmek için biraz daha yüksek).
        with pytest.raises(ValueError, match="aralığın dışında"):
            motion_calc.compute_dc_duty(rpm_rod=525.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)

    def test_rpm_giving_duty_below_one_rejected(self):
        # duty(1)=0.192 -> round 0 -> reddedilmeli (<1)
        with pytest.raises(ValueError, match="minimumun altında"):
            motion_calc.compute_dc_duty(rpm_rod=1.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)

    def test_zero_rpm_rejected(self):
        with pytest.raises(ValueError, match="RPM"):
            motion_calc.compute_dc_duty(rpm_rod=0.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)

    def test_negative_rpm_rejected(self):
        with pytest.raises(ValueError, match="RPM"):
            motion_calc.compute_dc_duty(rpm_rod=-10.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)

    def test_zero_wheel_diameter_rejected(self):
        with pytest.raises(ValueError, match="DC teker çapı"):
            motion_calc.compute_dc_duty(rpm_rod=10.0, d_wheel_dc_mm=0.0, d_rod_mm=10.0, rpm_max_noload=100.0)

    def test_zero_rod_diameter_rejected(self):
        with pytest.raises(ValueError, match="çubuğu çapı"):
            motion_calc.compute_dc_duty(rpm_rod=10.0, d_wheel_dc_mm=52.0, d_rod_mm=0.0, rpm_max_noload=100.0)

    def test_zero_rpm_max_noload_rejected(self):
        with pytest.raises(ValueError, match="RPM_MAX_NOLOAD"):
            motion_calc.compute_dc_duty(rpm_rod=10.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=0.0)

    def test_larger_rpm_max_noload_lowers_required_duty(self):
        lo = motion_calc.compute_dc_duty(rpm_rod=50.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=100.0)
        hi = motion_calc.compute_dc_duty(rpm_rod=50.0, d_wheel_dc_mm=52.0, d_rod_mm=10.0, rpm_max_noload=200.0)
        assert hi["duty_exact"] < lo["duty_exact"]


class TestConstantsConsistency:
    def test_v_jumpstart_within_faith_target_range(self):
        # Fatih'in hedef aralığı 0.1-5mm/s; v_jumpstart (D=52) bu aralığın
        # İÇİNDE olmalı (~3.19 mm/s) — spesifikasyonun beklediği tutarlılık.
        mps = motion_calc.mm_per_step(52.0)
        v_jumpstart = mps * 1_000_000 / motion_calc.STEP_RAMP_START_DELAY_US
        assert 0.1 < v_jumpstart < 5.0
        assert v_jumpstart == pytest.approx(3.190680038802134)

    def test_min_speed_matches_saha_notu(self):
        # Saha notu: "gerçekte ulaşılabilir hız aralığı ~0.106 – çok yüksek"
        mps = motion_calc.mm_per_step(52.0)
        min_speed = 1_000_000 * mps / motion_calc.STEP_MAX_DELAY_US
        assert min_speed == pytest.approx(0.10635600129340446)

    def test_steps_per_rev_matches_microstep_decision(self):
        # 200 tam adım/tur x 128 mikroadım (DIP SW5=OFF,SW6=OFF,SW7=OFF,SW8=ON) = 25600
        assert motion_calc.STEPS_PER_REV == 200 * 128

    def test_mm_per_step_matches_manual_formula(self):
        assert motion_calc.mm_per_step(52.0) == pytest.approx(math.pi * 52.0 / 25600)
