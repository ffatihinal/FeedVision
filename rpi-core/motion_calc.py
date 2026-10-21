"""
FeedVision — Fiziksel birim <-> ham firmware komutu dönüşümleri (23-09-2026)

Ne yapar: operatörün girdiği mm/s, mm, mm/s², RPM gibi fiziksel değerleri,
STM32 firmware'inin anladığı ham `delay`(µs)/`steps`/`accel`(adım), DC
motor `speed`(0-100 PWM duty%) ve NEMA17 dönüş ekseni `delay`(µs) değerlerine çevirir — kullanıcı hiçbir zaman
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
# Firmware JSON ayrıştırıcısı komut sayılarını int32 okur: "steps" bu sınırı aşarsa taşar.
STEP_MAX_STEPS = 2_000_000_000
# Tek bir besleme komutunun işletme üst sınırı (yazım hatası koruması: 1e9 mm gibi
# bir girdi sessizce kabul edilip saatlerce sürecek bir hareket başlatmasın).
STEP_MAX_DISTANCE_MM = 100_000
STEP_RAMP_START_DELAY_US = 2000  # main.c STEP_RAMP_START_DELAY_US (rampa başlangıç/bitiş gecikmesi)

# NEMA17 dönüş ekseni (TB6600) sınırları — firmware/Core/Src/main.c'deki
# ROT_MIN_DELAY_US / ROT_MAX_DELAY_US ile BİREBİR EŞİT tutulmalı (protokol
# sözleşmesi, 05-10-2026). main.c'de değişirse burası da elle güncellenmeli.
ROT_MIN_DELAY_US = 100  # main.c ROT_MIN_DELAY_US (tepe hız, iki darbe arası en kısa süre)
ROT_MAX_DELAY_US = 60000  # main.c ROT_MAX_DELAY_US (timer'ın 16-bit sayıcı tavanı)

# Yardımcı çift (2× NEMA17 paralel, 2. TB6600, LPTIM1) sınırları — firmware
# ASSIST_MIN_DELAY_US / ASSIST_MAX_DELAY_US ile BİREBİR EŞİT (docs/protocol.md
# "Yardımcı çift assist", 21-10-2026). main.c'de değişirse burası da elle güncellenmeli.
ASSIST_MIN_DELAY_US = 100  # tepe hız (10 kHz darbe)
ASSIST_MAX_DELAY_US = 60000


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

    Dönen `speed_pct_of_max` (29-09-2026 eklendi): girilen `speed_mms`'in, bu
    teker çapıyla `STEP_MIN_DELAY_US`'te ulaşılabilecek maksimum hıza (delay_us
    tepe hızda sabitlendiği için `max_speed_mms = 1e6 * mm_per_step / STEP_MIN_DELAY_US`)
    oranı, % olarak — operatörün UI'da "girdiğin hız motorun tepe hızının
    kaçta kaçı" bilgisini görebilmesi için (JS'de DUPLICATE hesaplanmasın diye
    burada hesaplanıp response'a ekleniyor).
    """
    # `not x > 0` (x <= 0 yerine): NaN'ı da yakalar; sonsuzlar aşağıda ayrıca elenir.
    if not speed_mms > 0:
        raise ValueError("Hız (mm/s) 0'dan büyük olmalı")
    if not distance_mm > 0:
        raise ValueError("Mesafe (mm) 0'dan büyük olmalı")
    if not d_drive_mm > 0:
        raise ValueError("Tahrik tekerleği çapı (mm) 0'dan büyük olmalı")
    if not math.isfinite(speed_mms) or not math.isfinite(distance_mm):
        raise ValueError("Hız ve mesafe sonlu bir sayı olmalı")

    mps = mm_per_step(d_drive_mm)

    # round() öncesi: devasa mesafe (1e308) bölümde sonsuza taşıp OverflowError vermesin
    if distance_mm > STEP_MAX_DISTANCE_MM:
        raise ValueError(f"Mesafe ({distance_mm} mm) çok büyük (en fazla {min(STEP_MAX_DISTANCE_MM, STEP_MAX_STEPS * mps):.0f} mm)")
    steps = round(distance_mm / mps)
    if steps <= 0:
        raise ValueError(
            f"Mesafe ({distance_mm} mm) bu teker çapıyla en az bir mikroadıma karşılık gelmiyor, çok küçük"
        )
    if steps > STEP_MAX_STEPS or distance_mm > STEP_MAX_DISTANCE_MM:
        max_mm = min(STEP_MAX_DISTANCE_MM, STEP_MAX_STEPS * mps)
        raise ValueError(f"Mesafe ({distance_mm} mm) çok büyük (en fazla {max_mm:.0f} mm)")

    delay_exact = 1_000_000 * mps / speed_mms
    # Aşırı küçük hızda bölüm sonsuza taşabilir (round(inf) OverflowError verirdi)
    delay_us = round(delay_exact) if math.isfinite(delay_exact) else STEP_MAX_DELAY_US + 1
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

    max_speed_mms = 1_000_000 * mps / STEP_MIN_DELAY_US
    speed_pct_of_max = speed_mms / max_speed_mms * 100

    return {
        "steps": steps,
        "delay_us": delay_us,
        "accel_steps": accel_steps,
        "mm_per_step": mps,
        "speed_pct_of_max": speed_pct_of_max,
    }


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
    if not rpm_rod > 0:
        raise ValueError("RPM 0'dan büyük olmalı")
    if d_wheel_dc_mm <= 0:
        raise ValueError("DC teker çapı (mm) 0'dan büyük olmalı")
    if d_rod_mm <= 0:
        raise ValueError("Besleme çubuğu çapı (mm) 0'dan büyük olmalı")
    if rpm_max_noload <= 0:
        raise ValueError("RPM_MAX_NOLOAD 0'dan büyük olmalı")

    duty_exact = rpm_rod * 100 * d_rod_mm / (rpm_max_noload * d_wheel_dc_mm)
    if not math.isfinite(duty_exact):
        raise ValueError(f"İstenen RPM ({rpm_rod}) sonlu bir sayı olmalı")
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


def compute_rot_command(rpm_rod: float, d_wheel_rot_mm: float, d_rod_mm: float, ppr: float) -> dict:
    """İstenen besleme çubuğu RPM'i -> NEMA17 dönüş ekseni darbe gecikmesi
    (µs, firmware `{"cmd":"rot","delay":...}`). Sürtünme tahriki, DC ile aynı
    fizik (RPM_rod = RPM_tekerlek × D_tekerlek / D_çubuk):

        wheel_rpm    = rpm_rod × D_rod / D_wheel_rot
        pulses_per_s = wheel_rpm / 60 × ppr
        delay_us     = round(1e6 / pulses_per_s)

    `ppr` = TB6600 darbe/tur (DIP switch ile aynı, bkz. motion_params.ROT_PPR).

    `delay_us` [ROT_MIN_DELAY_US, ROT_MAX_DELAY_US] dışına çıkarsa SESSİZCE
    KIRPILMAZ (compute_step_command/compute_dc_duty ile aynı gerekçe) —
    desteklenen min/max çubuk RPM'i ile açıklayıcı ValueError.

    Dönen `speed_pct_of_max`: istenen darbe hızının ROT_MIN_DELAY_US'teki
    (tepe) darbe hızına oranı, % (compute_step_command ile aynı mantık).
    """
    # `not x > 0` (x <= 0 yerine): NaN'ı da yakalar.
    if not rpm_rod > 0:
        raise ValueError("RPM 0'dan büyük olmalı")
    if not d_wheel_rot_mm > 0:
        raise ValueError("NEMA17 teker çapı (mm) 0'dan büyük olmalı")
    if not d_rod_mm > 0:
        raise ValueError("Besleme çubuğu çapı (mm) 0'dan büyük olmalı")
    if not ppr > 0:
        raise ValueError("ROT_PPR (darbe/tur) 0'dan büyük olmalı")

    wheel_rpm = rpm_rod * d_rod_mm / d_wheel_rot_mm
    pulses_per_s = wheel_rpm / 60 * ppr

    # pulses_per_s alt taşmayla 0'a düşerse (aşırı küçük RPM) bölme hatası
    # yerine aralık dışı hatasına düşsün.
    delay_exact = 1_000_000 / pulses_per_s if pulses_per_s > 0 else math.inf
    delay_us = round(delay_exact) if math.isfinite(delay_exact) else None

    if delay_us is None or delay_us < ROT_MIN_DELAY_US or delay_us > ROT_MAX_DELAY_US:
        # delay -> pulses_per_s -> çubuk RPM tersi: rpm_rod = pps × 60 / ppr × D_wheel / D_rod
        rod_rpm_per_pps = 60 / ppr * d_wheel_rot_mm / d_rod_mm
        min_rpm = (1_000_000 / ROT_MAX_DELAY_US) * rod_rpm_per_pps
        max_rpm = (1_000_000 / ROT_MIN_DELAY_US) * rod_rpm_per_pps
        raise ValueError(
            f"İstenen RPM ({rpm_rod}) bu ayarlarla desteklenen aralığın dışında "
            f"(min ~{min_rpm:.3f} RPM, max ~{max_rpm:.1f} RPM)"
        )

    max_pulses_per_s = 1_000_000 / ROT_MIN_DELAY_US
    speed_pct_of_max = pulses_per_s / max_pulses_per_s * 100

    return {
        "delay_us": delay_us,
        "wheel_rpm": wheel_rpm,
        "pulses_per_s": pulses_per_s,
        "speed_pct_of_max": speed_pct_of_max,
    }


def compute_assist_command(speed_mms: float, d_wheel_assist_mm: float, ppr: float) -> dict:
    """İstenen çubuk yüzey hızı (mm/s) -> yardımcı çift darbe gecikmesi (µs,
    firmware `{"cmd":"assist","delay":...}`). `compute_rot_command`'ın aynası;
    fark: giriş çubuk RPM'i değil, çubuğun yüzey hızı (mm/s). Sürtünme
    tahriki, tekerlek çevresi = çubuk yüzey yolu:

        wheel_rpm    = v / (pi × D_wheel_assist) × 60
        pulses_per_s = wheel_rpm / 60 × ppr
        delay_us     = round(1e6 / pulses_per_s)

    `ppr` = 2. TB6600 darbe/tur (DIP switch ile aynı, bkz. motion_params.ASSIST_PPR).
    Hız oranı/patinaj payı YOK: her motor kendi kutusundaki hızla gider.

    `delay_us` [ASSIST_MIN_DELAY_US, ASSIST_MAX_DELAY_US] dışına çıkarsa
    SESSİZCE KIRPILMAZ (firmware kırpar, ama burada operatöre net bir hata
    verilir): desteklenen min/max mm/s ile açıklayıcı ValueError.

    Dönen `speed_pct_of_max`: istenen darbe hızının ASSIST_MIN_DELAY_US'teki
    (tepe) darbe hızına oranı, % (compute_rot_command ile aynı mantık).
    """
    if not speed_mms > 0:
        raise ValueError("Hız (mm/s) 0'dan büyük olmalı")
    if not d_wheel_assist_mm > 0:
        raise ValueError("Yardımcı çift teker çapı (mm) 0'dan büyük olmalı")
    if not ppr > 0:
        raise ValueError("ASSIST_PPR (darbe/tur) 0'dan büyük olmalı")

    wheel_rpm = speed_mms / (math.pi * d_wheel_assist_mm) * 60
    pulses_per_s = wheel_rpm / 60 * ppr

    # pulses_per_s alt taşmayla 0'a düşerse (aşırı küçük hız) bölme hatası
    # yerine aralık dışı hatasına düşsün.
    delay_exact = 1_000_000 / pulses_per_s if pulses_per_s > 0 else math.inf
    delay_us = round(delay_exact) if math.isfinite(delay_exact) else None

    if delay_us is None or delay_us < ASSIST_MIN_DELAY_US or delay_us > ASSIST_MAX_DELAY_US:
        min_mms, max_mms = _assist_speed_bounds_mms(d_wheel_assist_mm, ppr)
        raise ValueError(
            f"İstenen hız ({speed_mms} mm/s) bu ayarlarla desteklenen aralığın dışında "
            f"(min ~{min_mms:.3f} mm/s, max ~{max_mms:.1f} mm/s)"
        )

    max_pulses_per_s = 1_000_000 / ASSIST_MIN_DELAY_US
    speed_pct_of_max = pulses_per_s / max_pulses_per_s * 100

    return {
        "delay_us": delay_us,
        "wheel_rpm": wheel_rpm,
        "pulses_per_s": pulses_per_s,
        "speed_pct_of_max": speed_pct_of_max,
    }


def _assist_speed_bounds_mms(d_wheel_assist_mm: float, ppr: float) -> tuple[float, float]:
    """Yardımcı çiftin desteklenen (min, max) çubuk yüzey hızı, mm/s —
    delay_us = ASSIST_MAX_DELAY_US / ASSIST_MIN_DELAY_US uçlarına karşılık gelir."""
    mm_per_pulse = math.pi * d_wheel_assist_mm / ppr
    return (
        (1_000_000 / ASSIST_MAX_DELAY_US) * mm_per_pulse,
        (1_000_000 / ASSIST_MIN_DELAY_US) * mm_per_pulse,
    )


# --------------------------------------------------------------------------
#  KANAL BAZLI İZİNLİ HIZ ARALIKLARI (21-10-2026) — arayüzün hız kutularına
#  min/max koymak ve anlamlı hata göstermek için. Sunucu aynı sınırları
#  compute_*_command içinde zaten uyguluyor (4xx); bu fonksiyonlar yalnızca
#  aynı sınırları DÜZ SAYI olarak dışarı verir (JS'de formül kopyalanmasın diye).
#  Alt sınır yukarı, üst sınır aşağı yuvarlanır: gösterilen uç değerler
#  kesinlikle kabul edilir.
# --------------------------------------------------------------------------

def _round_range(lo: float, hi: float, digits: int = 3) -> dict:
    factor = 10**digits
    return {"min": math.ceil(lo * factor) / factor, "max": math.floor(hi * factor) / factor}


def step_speed_limits_mms(d_drive_mm: float) -> dict:
    """NEMA23 desteklenen hız aralığı (mm/s) — delay STEP_MAX/STEP_MIN uçları."""
    mps = mm_per_step(d_drive_mm)
    return _round_range(1_000_000 * mps / STEP_MAX_DELAY_US, 1_000_000 * mps / STEP_MIN_DELAY_US)


def step_distance_limits_mm(d_drive_mm: float) -> dict:
    """NEMA23 desteklenen mesafe aralığı (mm): en az 1 mikroadım, en çok
    işletme sınırı (STEP_MAX_DISTANCE_MM; firmware adım sayacı sınırından küçükse)."""
    mps = mm_per_step(d_drive_mm)
    return _round_range(mps, min(STEP_MAX_DISTANCE_MM, STEP_MAX_STEPS * mps))


def rot_rpm_limits(d_wheel_rot_mm: float, d_rod_mm: float, ppr: float) -> dict:
    """Rotasyon çiftinin desteklenen çubuk RPM aralığı."""
    rod_rpm_per_pps = 60 / ppr * d_wheel_rot_mm / d_rod_mm
    return _round_range(
        (1_000_000 / ROT_MAX_DELAY_US) * rod_rpm_per_pps,
        (1_000_000 / ROT_MIN_DELAY_US) * rod_rpm_per_pps,
    )


def assist_speed_limits_mms(d_wheel_assist_mm: float, ppr: float) -> dict:
    """Yardımcı çiftin desteklenen çubuk yüzey hızı aralığı (mm/s)."""
    return _round_range(*_assist_speed_bounds_mms(d_wheel_assist_mm, ppr))


def dc_rpm_limits(d_wheel_dc_mm: float, d_rod_mm: float, rpm_max_noload: float) -> dict:
    """DC motorun desteklenen çubuk RPM aralığı: duty %1 .. %100 (bkz. compute_dc_duty)."""
    rpm_per_duty_pct = rpm_max_noload * d_wheel_dc_mm / (100 * d_rod_mm)
    return _round_range(1 * rpm_per_duty_pct, 100 * rpm_per_duty_pct)
