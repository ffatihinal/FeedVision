"""
FeedVision — Fiziksel birim <-> ham firmware komutu dönüşümleri (23-09-2026)

Ne yapar: operatörün girdiği mm/s, mm, mm/s², RPM gibi fiziksel değerleri,
STM32 firmware'inin anladığı ham `delay`(µs)/`steps`/`accel`(adım) ve DC
motor `speed`(0-100 PWM duty%) değerlerine çevirir — kullanıcı hiçbir zaman
ham birim görmez/girmez (main.py /motor/feed-start bunu kullanır).

Bilinçli olarak main.py'den (FastAPI/pydantic) ve serial_bridge.py'den
(donanım/seri port) BAĞIMSIZ, saf matematik — pytest ile hızlı ve donanımsız
test edilebilsin diye (bkz. tests/test_motion_calc.py).

Kaynak: Sistem Müh. + Elektronik spesifikasyonu (23-09-2026, sahadan gerçek
ölçüm + mikroadım DIP switch kararı).
"""

import math

# Mikroadım kararı (23-09-2026): DM556 DIP switch SW5=OFF,SW6=OFF,SW7=OFF,SW8=ON
# = 1/128 mikroadım. Motor başına 200 tam adım/tur × 128 = 25600 mikroadım/tur.
# Fatih sahada fiziksel olarak ayarlayacak — bu sabit RPi tarafında (burada)
# tanımlı, firmware kaç mikroadım geldiğini BİLMEZ (sadece darbe üretir),
# sadece RPi komut hesaplarken bu çarpanı kullanır. main.c'deki 200 adım/tur
# (mikroadımsız) varsayımıyla KARIŞTIRILMAMALI — bkz. docs/protocol.md
# "RPM'e çevirmek için mikroadım çarpanı gerekir" bölümü (o hesap ham/raw
# admin panelindeki manuel µs/adım girişleri için, mikroadım çarpanı=1
# varsayıyor; burada DIP switch fiziksel olarak 128 mikroadıma ayarlandığı
# için gerçek değer 128'dir — main.py ui/admin.html "Hareket Analizi"
# STEP_ANGLE_DEG sabiti de bu değişiklikle tutarlı hale getirildi).
STEPS_PER_REV = 25600

# firmware/Core/Src/main.c sabitleri — buradan KOPYALANDI (main.c'yi firmware
# tarafında değiştirmiyoruz, sadece RPi kendi hesaplarken aynı sınırları
# bilmesi gerekiyor). main.c değişirse burası da elle güncellenmeli.
STEP_MIN_DELAY_US = 20  # main.c STEP_MIN_DELAY_US (tepe hız, DM556 200kHz limitine göre 4x güvenlik payı)
STEP_MAX_DELAY_US = 60000  # main.c STEP_MAX_DELAY_US (timer'ın 16-bit sayıcı tavanı)
STEP_RAMP_START_DELAY_US = 2000  # main.c STEP_RAMP_START_DELAY_US (rampa başlangıç/bitiş gecikmesi)


def mm_per_step(d_drive_mm: float) -> float:
    """Bir mikroadımın karşılık geldiği doğrusal mesafe (mm) — tahrik
    tekerleğinin çapına (D_drive_mm) bağlı."""
    return math.pi * d_drive_mm / STEPS_PER_REV


def compute_step_command(speed_mms: float, distance_mm: float, accel_mms2: float, d_drive_mm: float) -> dict:
    """mm/s + mm + mm/s² -> ham step komutu ({"steps","delay_us","accel_steps"}).

    `speed_mms`/`distance_mm` > 0 zorunlu (çağıran taraf — main.py Pydantic
    Field(gt=0) — zaten garanti eder, burada da ValueError ile ikinci bir
    savunma hattı var çünkü bu fonksiyon main.py'den bağımsız da çağrılabilir/
    test edilebilir).

    `delay_us` hesaplanan aralığın [STEP_MIN_DELAY_US, STEP_MAX_DELAY_US]
    dışına çıkması SESSİZCE KIRPILMAZ (firmware'in kendi kırpma davranışının
    aksine) — operatöre net bir ValueError ile "bu teker çapıyla desteklenen
    hız aralığı şu" bilgisi verilir (Fatih'in açık talebi: firmware'in
    sessizce kırpmasına güvenme).

    `accel_mms2 <= 0` -> rampasız (accel_steps=0) kabul edilir; bu spesifikasyonda
    açıkça tarif edilmemiş bir sınır durum ama mevcut ham `/motor/step`
    endpoint'indeki "accel=0 => rampasız" kuralıyla tutarlı bir varsayılan
    (operatör kasıtlı olarak rampasız hareket isteyebilir).
    """
    if speed_mms <= 0:
        raise ValueError("Hız (mm/s) 0'dan büyük olmalı")
    if distance_mm <= 0:
        raise ValueError("Mesafe (mm) 0'dan büyük olmalı")
    if d_drive_mm <= 0:
        raise ValueError("Tahrik tekerleği çapı (mm) 0'dan büyük olmalı")

    mps = mm_per_step(d_drive_mm)

    steps = round(distance_mm / mps)
    if steps <= 0:
        raise ValueError(
            f"Mesafe ({distance_mm} mm) bu teker çapıyla en az bir mikroadıma karşılık gelmiyor, çok küçük"
        )

    delay_us = round(1_000_000 * mps / speed_mms)
    if delay_us < STEP_MIN_DELAY_US or delay_us > STEP_MAX_DELAY_US:
        min_speed = 1_000_000 * mps / STEP_MAX_DELAY_US
        max_speed = 1_000_000 * mps / STEP_MIN_DELAY_US
        raise ValueError(
            f"Hız ({speed_mms} mm/s) bu teker çapıyla desteklenen aralığın dışında "
            f"(min ~{min_speed:.3f} mm/s, max ~{max_speed:.1f} mm/s)"
        )

    if accel_mms2 <= 0:
        accel_steps = 0
    else:
        v_jumpstart = mps * 1_000_000 / STEP_RAMP_START_DELAY_US
        if speed_mms <= v_jumpstart:
            # Hedef hız zaten rampa başlangıcından yavaş/eşit — firmware'de
            # de fiilen accel_steps=0 gibi davranır (main.c step_start()
            # yorumu: "komut edilen hız zaten rampa başlangıcından yavaşsa,
            # rampa yapılacak bir şey yok").
            accel_steps = 0
        else:
            v_start = min(v_jumpstart, speed_mms)
            accel_steps = round((speed_mms**2 - v_start**2) / (2 * accel_mms2 * mps))
            accel_steps = max(0, min(accel_steps, steps // 2))

    return {"steps": steps, "delay_us": delay_us, "accel_steps": accel_steps, "mm_per_step": mps}


def compute_dc_duty(rpm_rod: float, d_wheel_dc_mm: float, d_rod_mm: float, rpm_max_noload: float) -> dict:
    """İstenen besleme çubuğu RPM'i -> gerekli DC motor PWM duty% (Senaryo B,
    sürtünme tekerlek — Fatih onaylı):

        RPM_rod = RPM_motor_shaft × (D_wheel_dc / D_rod)
        RPM_motor_shaft ≈ RPM_MAX_NOLOAD × (duty/100)   (doğrusal yaklaşım, placeholder)

    Tersinden: duty = rpm_rod × 100 × D_rod / (RPM_MAX_NOLOAD × D_wheel_dc)

    Hesaplanan duty [1, 100] dışına çıkarsa (ör. istenen RPM bu donanımla
    hiç ulaşılamayacak kadar yüksek/düşük) ValueError — motor/dc endpoint'inin
    kendi sessiz `max(0, min(100, speed))` kırpmasına burada GÜVENMİYORUZ,
    aynı gerekçeyle step tarafında da server-side reddediliyor (bkz.
    compute_step_command docstring'i).
    """
    if rpm_rod <= 0:
        raise ValueError("RPM 0'dan büyük olmalı")
    if d_wheel_dc_mm <= 0:
        raise ValueError("DC teker çapı (mm) 0'dan büyük olmalı")
    if d_rod_mm <= 0:
        raise ValueError("Besleme çubuğu çapı (mm) 0'dan büyük olmalı")
    if rpm_max_noload <= 0:
        raise ValueError("RPM_MAX_NOLOAD 0'dan büyük olmalı")

    duty_exact = rpm_rod * 100 * d_rod_mm / (rpm_max_noload * d_wheel_dc_mm)
    duty = round(duty_exact)

    if duty < 1:
        raise ValueError(
            f"İstenen RPM ({rpm_rod}) bu ayarlarla ulaşılabilecek minimumun altında "
            f"(gerekli duty %{duty_exact:.2f}, en az %1 gerekir)"
        )
    if duty > 100:
        max_rpm = rpm_max_noload * d_wheel_dc_mm / d_rod_mm
        raise ValueError(
            f"İstenen RPM ({rpm_rod}) bu ayarlarla desteklenen aralığın dışında (max ~{max_rpm:.1f} RPM @ duty %100)"
        )

    return {"duty": duty, "duty_exact": duty_exact}
