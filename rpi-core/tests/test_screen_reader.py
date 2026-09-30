"""
FeedVision — screen_reader.py testleri (SADECE saf goruntu islemesi, OCR
CAGRISI yapilmadan test edilebilen kisimlar).

Sentetik (numpy ile uretilen) goruntulerle calisir, gercek kameraya/Tesseract
binary'sine bagimli degil — read_text_ocr'in kendisi (Tesseract cagrisi)
kasitli olarak test edilmiyor (ortama gore var/yok), sadece "boolean" ROI'lerde
OCR'in hic CAGRILMADIGI dogrulaniyor.
"""

import cv2
import numpy as np
import pytest

import screen_reader
from screen_reader import (
    BOOLEAN_MATCH_RATIO_THRESHOLD,
    _build_ocr_config,
    _pin_current_process_to_ocr_cores,
    _restore_process_affinity,
    crop_roi,
    crop_roi_quad,
    read_boolean_state,
    read_roi,
)


def _solid_bgr_image(size: int, value: int) -> np.ndarray:
    """Tum pikselleri ayni gri tonda (value, value, value) olan kare bir goruntu —
    dusuk doygunluklu (S=0) test senaryolari icin (turkuaz DEGIL)."""
    return np.full((size, size, 3), value, dtype=np.uint8)


# BGR degeri — web "turquoise" rengi (RGB 64,224,208), ampirik olcumde
# HSV H=87 S=182 V=224 verir (bkz. screen_reader.BOOLEAN_HUE_RANGE'deki not) —
# gercek HMI'nin dolu-kare rengini temsil eden sentetik test rengi.
_TURQUOISE_BGR = (208, 224, 64)


def _solid_turquoise_image(size: int) -> np.ndarray:
    return np.full((size, size, 3), _TURQUOISE_BGR, dtype=np.uint8)


class TestReadBooleanState:
    """30-09-2026 duzeltmesi: read_boolean_state artik SADECE parlakliga degil
    turkuaz/cyan renk (Hue+Saturation) kriterine bakiyor — eski "sadece
    parlaklik" yaklasiminin parlak-ama-turkuaz-degil yanlis-pozitif riskini
    (ör. beyaz/gri parlak alan) burada acikca kapatiyoruz."""

    def test_empty_image_returns_zero(self):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        state, match_ratio = read_boolean_state(empty)
        assert state == 0
        assert match_ratio == 0.0

    def test_black_image_is_zero(self):
        black = _solid_bgr_image(20, value=0)
        state, match_ratio = read_boolean_state(black)
        assert state == 0
        assert match_ratio == 0.0

    def test_solid_turquoise_is_one(self):
        # Gercek turkuaz/cyan renkte dolu bir kare -> state=1, tum pikseller
        # kriteri karsiladigi icin match_ratio tam 1.0 olmali.
        turquoise = _solid_turquoise_image(20)
        state, match_ratio = read_boolean_state(turquoise)
        assert state == 1
        assert match_ratio == pytest.approx(1.0)

    def test_bright_white_is_not_falsely_positive(self):
        # KRITIK regresyon: eski kod (sadece V/parlaklik esigi 130) beyaz
        # (V=255) icin YANLISLIKLA 1 donerdi. Yeni kod dusuk doygunluk (S=0)
        # nedeniyle bunu 0 olarak elemeli.
        white = _solid_bgr_image(20, value=255)
        state, match_ratio = read_boolean_state(white)
        assert state == 0
        assert match_ratio == 0.0

    def test_bright_gray_is_not_falsely_positive(self):
        # Ayni regresyon, daha az uc bir deger (parlak gri metin/yansima
        # senaryosunu temsil eder) ile.
        bright_gray = _solid_bgr_image(20, value=200)
        state, match_ratio = read_boolean_state(bright_gray)
        assert state == 0
        assert match_ratio == 0.0

    def test_partial_turquoise_gives_intermediate_match_ratio(self):
        # Karenin yarisi turkuaz, yarisi siyah -> match_ratio ~0.5, esigin
        # (varsayilan ~0.28) USTUNDE oldugu icin state=1 olmali.
        half = np.zeros((20, 20, 3), dtype=np.uint8)
        half[:10, :, :] = _TURQUOISE_BGR
        state, match_ratio = read_boolean_state(half)
        assert match_ratio == pytest.approx(0.5)
        assert state == 1

    def test_small_turquoise_fraction_below_threshold_is_zero(self):
        # Karenin sadece %10'u turkuaz (gurultu/kismi yansima senaryosu) —
        # match_ratio esigin ALTINDA kalmali, state=0.
        mostly_black = np.zeros((20, 20, 3), dtype=np.uint8)
        mostly_black[:2, :, :] = _TURQUOISE_BGR
        state, match_ratio = read_boolean_state(mostly_black)
        assert match_ratio == pytest.approx(0.1)
        assert match_ratio < BOOLEAN_MATCH_RATIO_THRESHOLD
        assert state == 0

    def test_custom_match_ratio_threshold_is_respected(self):
        # %10 turkuaz varsayilan esikte 0 donuyordu (yukaridaki test) — ozel
        # olarak dusuk bir esik verilirse 1 donmeli.
        mostly_black = np.zeros((20, 20, 3), dtype=np.uint8)
        mostly_black[:2, :, :] = _TURQUOISE_BGR
        state, _ratio = read_boolean_state(mostly_black, match_ratio_threshold=0.05)
        assert state == 1

    def test_custom_hue_range_can_target_a_different_color(self):
        # Guvenlik agi: hue_range/sat_min disaridan verilebiliyor -- ileride
        # farkli renkte bir gosterge (ör. kirmizi alarm karesi, BGR (0,0,200))
        # cikarsa kod DEGISMEDEN cagiran taraf yeni araligi gecebilir.
        red_bgr = (0, 0, 200)
        red_image = np.full((20, 20, 3), red_bgr, dtype=np.uint8)
        # Varsayilan turkuaz araliginda kirmizi 0 donmeli.
        assert read_boolean_state(red_image)[0] == 0
        # Kirmizinin gercek Hue'sunu (~0) hedefleyen ozel bir aralikla 1 donmeli.
        state, match_ratio = read_boolean_state(red_image, hue_range=(0, 10), sat_min=100.0)
        assert state == 1
        assert match_ratio == pytest.approx(1.0)


class TestReadRoiKindDispatch:
    def test_boolean_kind_skips_ocr_and_fills_bool_state(self):
        frame = _solid_turquoise_image(100)
        result = read_roi(frame, roi=(0, 0, 50, 50), kind="boolean")
        assert result.kind == "boolean"
        assert result.bool_state == 1
        assert result.match_ratio == pytest.approx(1.0)
        assert result.text == ""
        assert result.ocr_error is None

    def test_boolean_kind_bright_white_is_zero_not_falsely_positive(self):
        # Ayni kritik regresyon read_roi uzerinden de dogrulanir: parlak ama
        # turkuaz OLMAYAN bir ROI "dolu" sayilmamali.
        frame = _solid_bgr_image(100, value=255)
        result = read_roi(frame, roi=(0, 0, 50, 50), kind="boolean")
        assert result.bool_state == 0
        assert result.match_ratio == pytest.approx(0.0)

    def test_numeric_kind_leaves_bool_state_and_match_ratio_none(self):
        frame = _solid_turquoise_image(100)
        result = read_roi(frame, roi=(0, 0, 50, 50), kind="numeric")
        assert result.kind == "numeric"
        assert result.bool_state is None
        assert result.match_ratio is None

    def test_default_kind_is_numeric(self):
        frame = _solid_turquoise_image(100)
        result = read_roi(frame, roi=(0, 0, 50, 50))
        assert result.kind == "numeric"


class TestCropRoi:
    def test_crop_within_bounds(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        cropped = crop_roi(frame, (10, 10, 20, 30))
        assert cropped.shape == (30, 20, 3)

    def test_crop_clips_to_frame_bounds(self):
        frame = np.zeros((50, 50, 3), dtype=np.uint8)
        cropped = crop_roi(frame, (40, 40, 30, 30))
        assert cropped.shape == (10, 10, 3)

    def test_no_calibration_still_uses_plain_rect_crop_regression(self):
        # Görev A regresyon: quad verilmezse (kalibrasyon yok) read_roi eski
        # crop_roi (düz dikdörtgen) davranışını korumalı.
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:40, 10:30] = (0, 200, 0)  # ROI içi yeşil boyalı
        result = read_roi(frame, roi=(10, 10, 20, 30), kind="numeric", quad=None)
        # avg_color_rgb yaklaşık (0, 200, 0) olmalı — crop_roi ile aynı bölge kırpıldı.
        r, g, b = result.avg_color_rgb
        assert r == pytest.approx(0, abs=2)
        assert g == pytest.approx(200, abs=2)
        assert b == pytest.approx(0, abs=2)


class TestCropRoiQuad:
    def test_axis_aligned_quad_matches_plain_crop_size(self):
        # Eksene hizali (döndürülmemiş) bir "dörtgen" verilirse, crop_roi_quad
        # crop_roi ile aynı boyutta/içerikte bir görüntü üretmeli.
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:40, 10:30] = (0, 200, 0)
        quad = np.array([[10, 10], [30, 10], [30, 40], [10, 40]], dtype=np.float32)
        cropped = crop_roi_quad(frame, quad)
        assert cropped.shape[:2] == (30, 20)
        # atol biraz gevşek: warpPerspective kenar piksellerinde interpolasyon
        # nedeniyle en dış satır/sütunda hafif karışım oluşabilir — asıl
        # doğrulanan şey boyut ve baskın renk, alt-piksel kenar hassasiyeti değil.
        mean_color = cropped.reshape(-1, 3).mean(axis=0)
        np.testing.assert_allclose(mean_color, [0, 200, 0], atol=20)

    def test_skewed_quad_is_flattened_and_contains_painted_region(self):
        # Eğik (paralelkenar) bir dörtgen tanımlanır, içinde belirli bir renk
        # boyanmış olsun — düzleştirilmiş çıktı bu rengi baskın olarak içermeli.
        frame = np.full((200, 200, 3), 255, dtype=np.uint8)
        skewed_region = np.array([[50, 50], [130, 60], [110, 140], [30, 130]], dtype=np.int32)
        cv2.fillConvexPoly(frame, skewed_region, (0, 0, 200))  # kırmızı (BGR)
        quad = np.array([[50, 50], [130, 60], [110, 140], [30, 130]], dtype=np.float32)
        cropped = crop_roi_quad(frame, quad)
        assert cropped.size > 0
        mean_color = cropped.reshape(-1, 3).mean(axis=0)  # BGR
        # Çoğunlukla boyalı bölgeden geldiği için kırmızı (B kanalı) baskın olmalı.
        assert mean_color[2] > mean_color[0]
        assert mean_color[2] > 100

    def test_output_size_override_is_respected(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        quad = np.array([[10, 10], [30, 10], [30, 40], [10, 40]], dtype=np.float32)
        cropped = crop_roi_quad(frame, quad, output_size=(64, 128))
        assert cropped.shape[:2] == (128, 64)


class TestReadRoiQuadPath:
    def test_quad_provided_uses_perspective_crop_not_plain_bbox(self):
        # Aynı boyuttaki bbox'ın DIŞINDA kalan bir alanı kapsayan eğik quad
        # verilirse, read_roi'nin quad yolunu kullandığı (roi bbox'ını değil)
        # renk sonucundan doğrulanabilir.
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[60:90, 60:90] = (0, 0, 250)  # sağ-alt köşede kırmızı bölge
        quad = np.array([[60, 60], [90, 60], [90, 90], [60, 90]], dtype=np.float32)
        # roi (bbox) kasıtlı olarak farklı/boş bir bölgeyi işaret ediyor — quad
        # verildiğinde bunun YOK sayıldığını doğrular.
        result = read_roi(frame, roi=(0, 0, 5, 5), kind="numeric", quad=quad)
        r, g, b = result.avg_color_rgb
        # Kenar interpolasyonu nedeniyle biraz gevşek tolerans (bkz. yukarıdaki
        # TestCropRoiQuad notu) — asıl doğrulanan şey roi bbox'ının DEĞİL,
        # quad'ın kullanıldığı (kırmızı bölgenin baskın çıkması).
        assert r > 200
        assert r > g and r > b


class TestBuildOcrConfig:
    """_build_ocr_config saf/yan-etkisiz bir string kurucu — Tesseract'a hiç
    girmeden test edilebilir (30-09-2026, saha OCR intermittent-fail düzeltmesi)."""

    def test_default_uses_psm_7_no_whitelist(self):
        assert _build_ocr_config(None) == "--psm 7"

    def test_empty_string_whitelist_treated_as_no_whitelist(self):
        # "" da None gibi davranmalı (falsy) — çağıran taraf yanlışlıkla boş
        # string geçirirse Tesseract'a anlamsız bir "-c ...=" eklenmemeli.
        assert _build_ocr_config("") == "--psm 7"

    def test_whitelist_appended_as_tesseract_char_whitelist_option(self):
        config = _build_ocr_config("0123456789.-")
        assert config == "--psm 7 -c tessedit_char_whitelist=0123456789.-"


class TestOcrAffinityPinning:
    """CPU affinity pin/restore — Pi 5'te (Linux) Tesseract subprocess'lerini
    {2,3} çekirdeklerine sabitler, kamera+web sunucusunun kullandığı 0-1'i
    serbest bırakır (30-09-2026, saha FPS düşüşü bulgusu). Mac'te (geliştirme
    ortamı) os.sched_setaffinity yok -> gerçek platformda (bu makinede) no-op
    olduğu, SİMÜLE edilmiş Linux davranışının da doğru çalıştığı ayrı ayrı
    doğrulanıyor."""

    def test_noop_on_platform_without_sched_setaffinity(self):
        # Bu test makinesi (Mac) zaten os.sched_setaffinity'ye sahip değil —
        # gerçek/doğal no-op davranışını doğrular (hasattr kontrolü).
        if hasattr(screen_reader.os, "sched_setaffinity"):
            pytest.skip("bu platformda sched_setaffinity mevcut, no-op yolu test edilemez")
        previous = _pin_current_process_to_ocr_cores()
        assert previous is None
        _restore_process_affinity(previous)  # None ile çağrılırsa hata vermemeli

    def test_pin_sets_affinity_and_returns_previous_on_simulated_linux(self, monkeypatch):
        state = {"affinity": {0, 1, 2, 3}}
        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", lambda pid, cores: state.__setitem__("affinity", set(cores)), raising=False)
        monkeypatch.setattr(screen_reader.os, "sched_getaffinity", lambda pid: set(state["affinity"]), raising=False)

        previous = _pin_current_process_to_ocr_cores()

        assert previous == frozenset({0, 1, 2, 3})
        assert state["affinity"] == {2, 3}

    def test_restore_puts_previous_affinity_back(self, monkeypatch):
        state = {"affinity": {2, 3}}
        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", lambda pid, cores: state.__setitem__("affinity", set(cores)), raising=False)

        _restore_process_affinity(frozenset({0, 1, 2, 3}))

        assert state["affinity"] == {0, 1, 2, 3}

    def test_restore_noop_when_previous_is_none(self, monkeypatch):
        calls = []
        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", lambda pid, cores: calls.append(cores), raising=False)

        _restore_process_affinity(None)

        assert calls == []  # sabitleme hiç uygulanmadıysa geri yükleme de yapılmamalı

    def test_pin_skips_when_target_cores_unavailable(self, monkeypatch):
        # Örn. 2 çekirdekli bir cihaz/cgroup kısıtı — {2,3} zaten kullanılabilir
        # değilse sabitlemenin anlamı yok, mevcut kümeye dokunulmamalı.
        monkeypatch.setattr(screen_reader.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
        calls = []
        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", lambda pid, cores: calls.append(cores), raising=False)

        previous = _pin_current_process_to_ocr_cores()

        assert previous is None
        assert calls == []

    def test_pin_swallows_oserror_and_returns_none(self, monkeypatch):
        monkeypatch.setattr(screen_reader.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3}, raising=False)

        def _raise(pid, cores):
            raise OSError("izin yok")

        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", _raise, raising=False)

        assert _pin_current_process_to_ocr_cores() is None


class TestReadTextOcrUsesConfigAndAffinity:
    """read_text_ocr'ın Tesseract'a doğru config'i geçirdiğini VE affinity
    pin/restore çağrılarını (başarı + exception yolunda) doğru sırayla
    yaptığını, gerçek Tesseract binary'sine bağımlı olmadan (pytesseract
    mock'lanarak) doğrular."""

    def _gray_image(self):
        return np.full((10, 10, 3), 200, dtype=np.uint8)

    def test_passes_psm_and_whitelist_config_to_pytesseract(self, monkeypatch):
        captured = {}

        class _FakePytesseract:
            @staticmethod
            def image_to_string(image, config=""):
                captured["config"] = config
                return "19"

        monkeypatch.setattr(screen_reader, "PYTESSERACT_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "pytesseract", _FakePytesseract)

        text, err = screen_reader.read_text_ocr(self._gray_image(), char_whitelist="0123456789.-")

        assert text == "19"
        assert err is None
        assert captured["config"] == "--psm 7 -c tessedit_char_whitelist=0123456789.-"

    def test_affinity_is_pinned_during_call_and_restored_after(self, monkeypatch):
        affinity_during_call = {}

        class _FakePytesseract:
            @staticmethod
            def image_to_string(image, config=""):
                affinity_during_call["value"] = screen_reader.os.sched_getaffinity(0)
                return "42"

        monkeypatch.setattr(screen_reader, "PYTESSERACT_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "pytesseract", _FakePytesseract)
        state = {"affinity": {0, 1, 2, 3}}
        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", lambda pid, cores: state.__setitem__("affinity", set(cores)), raising=False)
        monkeypatch.setattr(screen_reader.os, "sched_getaffinity", lambda pid: set(state["affinity"]), raising=False)

        screen_reader.read_text_ocr(self._gray_image())

        assert affinity_during_call["value"] == {2, 3}  # Tesseract çağrısı SIRASINDA {2,3}'e sabitlenmiş olmalı
        assert state["affinity"] == {0, 1, 2, 3}  # çağrı bitince eski hale dönmüş olmalı

    def test_affinity_restored_even_if_pytesseract_raises(self, monkeypatch):
        class _FakePytesseract:
            @staticmethod
            def image_to_string(image, config=""):
                raise RuntimeError("tesseract binary bulunamadı")

        monkeypatch.setattr(screen_reader, "PYTESSERACT_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "pytesseract", _FakePytesseract)
        state = {"affinity": {0, 1, 2, 3}}
        monkeypatch.setattr(screen_reader.os, "sched_setaffinity", lambda pid, cores: state.__setitem__("affinity", set(cores)), raising=False)
        monkeypatch.setattr(screen_reader.os, "sched_getaffinity", lambda pid: set(state["affinity"]), raising=False)

        text, err = screen_reader.read_text_ocr(self._gray_image())

        assert text == ""
        assert "tesseract binary bulunamadı" in err
        assert state["affinity"] == {0, 1, 2, 3}  # exception olsa da affinity geri yüklenmiş olmalı


class _FakeTesserocrPSM:
    SINGLE_LINE = 7


class _FakeTesserocrAPI:
    """tesserocr.PyTessBaseAPI'nin sahte/stub bir sürümü — gerçek Tesseract
    motoruna dokunmadan (bu makinede tesserocr kurulu olmayabilir) PSM/
    whitelist/görüntü çağrılarının doğru sırayla/parametreyle yapıldığını
    doğrulamak için kullanılır."""

    def __init__(self):
        self.psm_calls: list[int] = []
        self.whitelist_calls: list[tuple[str, str]] = []
        self.image_calls: list[object] = []
        self.text = "19"

    def SetPageSegMode(self, psm):
        self.psm_calls.append(psm)

    def SetVariable(self, name, value):
        self.whitelist_calls.append((name, value))

    def SetImage(self, image):
        self.image_calls.append(image)

    def GetUTF8Text(self):
        return self.text


class _FakeTesserocrModule:
    """tesserocr modülünün sahte sürümü — PyTessBaseAPI() her çağrıldığında
    yeni bir _FakeTesserocrAPI üretir, ama hepsini created_apis'te tutar
    (böylece "motor bir kere açıldı mı" testleri yapılabilir)."""

    PSM = _FakeTesserocrPSM

    def __init__(self, api_factory=_FakeTesserocrAPI):
        self.created_apis: list[_FakeTesserocrAPI] = []
        self._api_factory = api_factory

    def PyTessBaseAPI(self):
        api = self._api_factory()
        self.created_apis.append(api)
        return api


class TestReadTextOcrPytesseractPathRegressionWhenTesserocrUnavailable:
    """TESSEROCR_AVAILABLE=False durumunda (tesserocr kurulu değil/Mac'te
    derlenemedi senaryosu) pytesseract yolunun HİÇ değişmediğini kilitler —
    30-09-2026 tesserocr eklenmesi sonrası regresyon kilidi."""

    def test_pytesseract_path_used_unchanged_with_config_and_result(self, monkeypatch):
        captured = {}

        class _FakePytesseract:
            @staticmethod
            def image_to_string(image, config=""):
                captured["config"] = config
                return "19"

        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", False)
        monkeypatch.setattr(screen_reader, "PYTESSERACT_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "pytesseract", _FakePytesseract)

        text, err = screen_reader.read_text_ocr(
            np.full((10, 10, 3), 200, dtype=np.uint8), char_whitelist="0123456789.-"
        )

        assert text == "19"
        assert err is None
        assert captured["config"] == "--psm 7 -c tessedit_char_whitelist=0123456789.-"

    def test_neither_backend_available_returns_same_error_as_before(self, monkeypatch):
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", False)
        monkeypatch.setattr(screen_reader, "PYTESSERACT_AVAILABLE", False)

        text, err = screen_reader.read_text_ocr(np.full((10, 10, 3), 200, dtype=np.uint8))

        assert text == ""
        assert err == "pytesseract Python paketi kurulu degil (pip install pytesseract)"


class TestReadTextOcrTesserocrPath:
    """TESSEROCR_AVAILABLE=True simülasyonu (sahte tesserocr modülü ile) —
    gerçek tesserocr bu ortamda kurulu olmasa da PSM/whitelist kararlarının
    doğru API çağrılarına dönüştüğünü doğrular."""

    def setup_method(self):
        # Modül-seviyesinde saklanan lazy-init singleton'ı her testten önce
        # sıfırla — testler birbirinin API örneğini miras almasın.
        screen_reader._tesserocr_api = None

    def teardown_method(self):
        screen_reader._tesserocr_api = None

    def test_sets_psm_and_whitelist_then_reads_image(self, monkeypatch):
        fake_module = _FakeTesserocrModule()
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)

        text, err = screen_reader.read_text_ocr(
            np.full((10, 10, 3), 200, dtype=np.uint8), char_whitelist="0123456789.-"
        )

        assert err is None
        assert text == "19"
        assert len(fake_module.created_apis) == 1
        api = fake_module.created_apis[0]
        assert api.psm_calls == [_FakeTesserocrPSM.SINGLE_LINE]
        assert api.whitelist_calls == [("tessedit_char_whitelist", "0123456789.-")]
        assert len(api.image_calls) == 1

    def test_api_instance_created_once_and_reused_across_calls(self, monkeypatch):
        fake_module = _FakeTesserocrModule()
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)
        image = np.full((10, 10, 3), 200, dtype=np.uint8)

        screen_reader.read_text_ocr(image)
        screen_reader.read_text_ocr(image)
        screen_reader.read_text_ocr(image)

        # Motor (PyTessBaseAPI) sadece BİR KERE açılmış olmalı — asıl amaç
        # pytesseract'ın her çağrıda yeni subprocess açmasının aksine burada
        # tek bir örneğin tekrar kullanılması.
        assert len(fake_module.created_apis) == 1

    def test_empty_whitelist_clears_previous_call_whitelist(self, monkeypatch):
        fake_module = _FakeTesserocrModule()
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)
        image = np.full((10, 10, 3), 200, dtype=np.uint8)

        screen_reader.read_text_ocr(image, char_whitelist="0123456789")
        screen_reader.read_text_ocr(image, char_whitelist=None)

        api = fake_module.created_apis[0]
        assert api.whitelist_calls[-1] == ("tessedit_char_whitelist", "")

    def test_exception_from_tesserocr_returns_same_error_format_as_pytesseract(self, monkeypatch):
        class _RaisingAPI(_FakeTesserocrAPI):
            def GetUTF8Text(self):
                raise RuntimeError("motor baslatilamadi")

        fake_module = _FakeTesserocrModule(api_factory=_RaisingAPI)
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)

        text, err = screen_reader.read_text_ocr(np.full((10, 10, 3), 200, dtype=np.uint8))

        assert text == ""
        assert "motor baslatilamadi" in err

    def test_affinity_pinned_during_call_and_restored_after_tesserocr_path(self, monkeypatch):
        fake_module = _FakeTesserocrModule()
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)
        state = {"affinity": {0, 1, 2, 3}}
        monkeypatch.setattr(
            screen_reader.os,
            "sched_setaffinity",
            lambda pid, cores: state.__setitem__("affinity", set(cores)),
            raising=False,
        )
        monkeypatch.setattr(
            screen_reader.os, "sched_getaffinity", lambda pid: set(state["affinity"]), raising=False
        )

        screen_reader.read_text_ocr(np.full((10, 10, 3), 200, dtype=np.uint8))

        assert state["affinity"] == {0, 1, 2, 3}  # çağrı bitince eski hale dönmüş olmalı

    def test_tesserocr_preferred_over_pytesseract_when_both_available(self, monkeypatch):
        fake_module = _FakeTesserocrModule()
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)
        monkeypatch.setattr(screen_reader, "PYTESSERACT_AVAILABLE", True)

        class _ShouldNotBeCalled:
            @staticmethod
            def image_to_string(image, config=""):
                raise AssertionError("pytesseract çağrılmamalı — tesserocr öncelikli olmalı")

        monkeypatch.setattr(screen_reader, "pytesseract", _ShouldNotBeCalled)

        text, err = screen_reader.read_text_ocr(np.full((10, 10, 3), 200, dtype=np.uint8))

        assert err is None
        assert text == "19"

    def test_empty_image_returns_before_touching_tesserocr(self, monkeypatch):
        fake_module = _FakeTesserocrModule()
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)

        text, err = screen_reader.read_text_ocr(np.zeros((0, 0, 3), dtype=np.uint8))

        assert text == ""
        assert err is None
        assert fake_module.created_apis == []  # boş görüntüde motora hiç girilmemeli


class TestReadTextOcrTesserocrThreadSafety:
    """_tesserocr_lock'un gercekten koruma sagladigini dogrular (30-09-2026,
    tester FAIL bulgusu): main.py'de vision_read_test/_rule_engine_loop/
    _journal_loop AYRI thread'lerden ayni paylasilan _tesserocr_api'yi
    cagirabiliyordu -- kilitsiz durumda SetVariable/SetImage/GetUTF8Text
    ic ice girip (interleave) bir ROI'nin whitelist/goruntusu digerininkiyle
    KARISABILIRDI. Sahte tesserocr modulunde GetUTF8Text'e kucuk bir gecikme
    konup iki thread ayni anda read_text_ocr() cagirdiginda sonuclarin
    KARISMADIGI (her thread kendi whitelist'ine karsilik gelen metni aldigi)
    dogrulanir."""

    def setup_method(self):
        screen_reader._tesserocr_api = None

    def teardown_method(self):
        screen_reader._tesserocr_api = None

    def test_concurrent_calls_do_not_interleave_results(self, monkeypatch):
        import threading
        import time

        class _SlowInterleavingAPI:
            """GetUTF8Text'te kasitli gecikme -- kilit olmasaydi iki thread'in
            SetVariable/SetImage cagrilari bu gecikme sirasinda ic ice girip
            digerinin whitelist'ini/goruntusunu 'calardi'."""

            def __init__(self):
                self._whitelist = None

            def SetPageSegMode(self, psm):
                pass

            def SetVariable(self, name, value):
                self._whitelist = value

            def SetImage(self, image):
                pass

            def GetUTF8Text(self):
                time.sleep(0.05)  # kilit yoksa diger thread bu sirada whitelist'i degistirebilir
                return f"text-for-{self._whitelist}"

        fake_module = _FakeTesserocrModule(api_factory=_SlowInterleavingAPI)
        monkeypatch.setattr(screen_reader, "TESSEROCR_AVAILABLE", True)
        monkeypatch.setattr(screen_reader, "tesserocr", fake_module)
        image = np.full((10, 10, 3), 200, dtype=np.uint8)

        results: dict[str, tuple[str, str | None]] = {}

        def _call(thread_name, whitelist):
            results[thread_name] = screen_reader.read_text_ocr(image, char_whitelist=whitelist)

        t1 = threading.Thread(target=_call, args=("t1", "AAAA"))
        t2 = threading.Thread(target=_call, args=("t2", "BBBB"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Kilit calisiyorsa her thread KENDI whitelist'ine karsilik gelen
        # metni almis olmali -- interleave olsaydi biri digerinin whitelist'ini
        # gorup "text-for-BBBB"/"text-for-AAAA" karisikligi yasardi.
        assert results["t1"] == ("text-for-AAAA", None)
        assert results["t2"] == ("text-for-BBBB", None)
        # Motor tek kilit altinda lazy-init edildigi icin yine tek ornek olmali.
        assert len(fake_module.created_apis) == 1


class TestReadRoiForwardsOcrWhitelist:
    def test_read_roi_passes_ocr_whitelist_to_read_text_ocr(self, monkeypatch):
        captured = {}

        def _fake_read_text_ocr(image, char_whitelist=None):
            captured["char_whitelist"] = char_whitelist
            return "19", None

        monkeypatch.setattr(screen_reader, "read_text_ocr", _fake_read_text_ocr)
        frame = np.full((100, 100, 3), 200, dtype=np.uint8)

        result = read_roi(frame, roi=(0, 0, 50, 50), kind="numeric", ocr_whitelist="0123456789.-")

        assert result.text == "19"
        assert captured["char_whitelist"] == "0123456789.-"
