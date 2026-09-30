"""
FeedVision — main.py'nin ROI drift + gerçek dörtgen (quad) entegrasyonu
testleri (Görev A, 2026-09-29; şablon-tabanlı referans + kalibrasyon
aç/kapa toggle entegrasyonu 2026-09-30 eklendi).

_adjust_rois_for_drift / _read_all_rois saf Python fonksiyonları (FastAPI'ye
bağlı değil) — TestClient/HTTP'ye gerek yok, doğrudan çağrılabilirler (bkz.
test_motor_feed_start.py'deki aynı desen). calibration_store/roi_store gerçek
disk dosyalarına dokunmasın diye isolated_* fixture'ları kullanılıyor.
"""

import cv2
import numpy as np
import pytest

import calibration_store
import main
import roi_store


def _frame_with_marker(size=200, marker_pos=(80, 60), marker_size=20, bg=128):
    """Dokulu (iç içe siyah çerçeve + beyaz merkez) bir marker çizer — sabit
    bir UI ikonunun/logonun basit bir temsili (test_screen_calibration.py'deki
    TestFindTemplateAnchor._frame_with_marker ile aynı desen, düz tek renk
    DEĞİL çünkü TM_CCOEFF_NORMED sıfır varyanslı şablonla tanımsız sonuç verir)."""
    frame = np.full((size, size, 3), bg, dtype=np.uint8)
    x, y = marker_pos
    frame[y : y + marker_size, x : x + marker_size] = (0, 0, 0)
    inner = marker_size // 4
    frame[y + inner : y + marker_size - inner, x + inner : x + marker_size - inner] = (255, 255, 255)
    return frame


def _register_template_from_frame(cam_id, frame, marker_pos=(80, 60), marker_size=20):
    """Gerçek Admin akışını taklit eder: karenin bir bölgesini kırpıp JPEG'e
    kodlayıp calibration_store.add_template ile kaydeder (main.py'nin
    /vision/{cam_id}/calibration/template endpoint'iyle AYNI veri yolu,
    burada HTTP katmanı olmadan doğrudan çağrılıyor)."""
    x, y = marker_pos
    crop = frame[y : y + marker_size, x : x + marker_size].copy()
    ok, jpg = cv2.imencode(".jpg", crop)
    assert ok
    return calibration_store.add_template(
        cam_id, anchor=[float(x), float(y)], size=[marker_size, marker_size], image_bytes=jpg.tobytes()
    )


@pytest.fixture
def _blank_frame():
    return np.zeros((100, 100, 3), dtype=np.uint8)


class TestAdjustRoisForDriftRegression:
    """Kalibrasyon yokken eski davranış (ham ROI, quad yok) korunmalı."""

    def test_no_calibration_returns_rois_unmodified(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame
    ):
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", _blank_frame, rois)
        assert adjusted == rois  # aynı liste, dokunulmamış
        assert "quad" not in adjusted[0]
        assert uncertain is False


class TestAdjustRoisForDriftWithCalibration:
    """Kalibrasyon VARSA her roi_def'e bounding box'ın YANINDA gerçek quad eklenmeli."""

    def test_calibrated_but_corners_not_found_falls_back_uncertain(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        calibration_store.save_reference(
            "ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]]
        )
        monkeypatch.setattr(main, "detect_screen_corners", lambda frame: None)
        main._last_known_corners.pop("ui_screen", None)
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", _blank_frame, rois)
        # Hiç iyi kare görülmedi -> düzeltmesiz devam, ama uncertain=True.
        assert adjusted == rois
        assert uncertain is True

    def test_calibrated_and_corners_found_adds_quad_alongside_bbox(
        self, isolated_calibration_config, isolated_roi_config, _blank_frame, monkeypatch
    ):
        ref_corners = [[0, 0], [100, 0], [100, 100], [0, 100]]
        calibration_store.save_reference("ui_screen", ref_corners)
        # Şimdiki köşeler referansa göre hafif trapez (perspektif kayması).
        current_corners = np.array(
            [[0, 0], [100, 10], [95, 100], [0, 100]], dtype=np.float32
        )
        monkeypatch.setattr(main, "detect_screen_corners", lambda frame: current_corners)
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", _blank_frame, rois)
        assert uncertain is False
        roi_def = adjusted[0]
        assert "quad" in roi_def
        quad = np.array(roi_def["quad"], dtype=np.float32)
        assert quad.shape == (4, 2)
        # bbox (x,y,w,h), quad'ın min/max'ıyla tutarlı olmalı (aynı geometri)
        # — dikdörtgenlik bozulması zaten test_screen_calibration.py'deki
        # TestWarpRoiQuad.test_perspective_skew_... içinde ayrıca doğrulanıyor,
        # burada asıl kontrol main.py'nin quad'ı bbox'ın YANINDA taşıdığı.
        assert roi_def["x"] == pytest.approx(float(np.min(quad[:, 0])), abs=1)
        assert roi_def["y"] == pytest.approx(float(np.min(quad[:, 1])), abs=1)


class TestReadAllRoisUsesQuad:
    def test_result_includes_roi_quad_when_calibrated(
        self, isolated_calibration_config, isolated_roi_config, monkeypatch
    ):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:30, 10:30] = (0, 150, 0)
        roi_store.save_rois(
            "ui_screen", [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        )
        calibration_store.save_reference(
            "ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]]
        )
        # Kimlik dönüşüm (şimdiki = referans) — bbox/quad aynı bölgeyi işaret etsin.
        identity_corners = np.array(
            [[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32
        )
        monkeypatch.setattr(main, "detect_screen_corners", lambda f: identity_corners)
        results, uncertain = main._read_all_rois("ui_screen", frame)
        assert uncertain is False
        assert len(results) == 1
        assert results[0]["roi_quad"] is not None
        assert len(results[0]["roi_quad"]) == 4

    def test_result_roi_quad_is_none_without_calibration(
        self, isolated_calibration_config, isolated_roi_config
    ):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        roi_store.save_rois(
            "ui_screen", [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        )
        results, uncertain = main._read_all_rois("ui_screen", frame)
        assert uncertain is False
        assert results[0]["roi_quad"] is None


# ==============================================================================
#  2026-09-30, Görev A — şablon-tabanlı referans BİRİNCİL kaynak olarak
#  entegre edildi (bezel köşe tespiti yerine, ör. dişli ikonu). Şablon hiç
#  kaydedilmemişse eski bezel yöntemine geriye uyumlu düşülüyor (yukarıdaki
#  testler zaten bunu kanıtlıyor — hiçbirinde şablon kaydedilmedi).
# ==============================================================================


class TestAdjustRoisForDriftTemplatePriority:
    """Kayıtlı referans şablon(lar) varsa BİRİNCİL kaynak bunlar olmalı —
    bezel köşe tespiti (detect_screen_corners) hiç çağrılmamalı, aynı anda
    bir bezel referansı da kayıtlı olsa bile."""

    def test_template_present_skips_bezel_detection_entirely(
        self, isolated_calibration_config, isolated_roi_config, monkeypatch
    ):
        main._last_known_anchors.pop("ui_screen", None)
        frame = _frame_with_marker(marker_pos=(80, 60))
        _register_template_from_frame("ui_screen", frame, marker_pos=(80, 60))
        # Bezel referansı da kayıtlı — şablon varken buna HİÇ bakılmamalı.
        calibration_store.save_reference("ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]])

        def _boom(_frame):
            raise AssertionError("detect_screen_corners çağrılmamalıydı (şablon varken bezele düşülmemeli)")

        monkeypatch.setattr(main, "detect_screen_corners", _boom)

        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", frame, rois)
        assert uncertain is False
        assert "quad" in adjusted[0]
        # Şablon tam olarak aynı yerde bulunduğu için (kare değişmedi) ROI
        # neredeyse hiç kaymamalı (kimlik dönüşüme yakın).
        assert adjusted[0]["x"] == pytest.approx(10, abs=1)
        assert adjusted[0]["y"] == pytest.approx(10, abs=1)


class TestAdjustRoisForDriftFallsBackToBezelWithoutTemplates:
    """Şablon hiç kaydedilmemişse eski bezel-köşe yöntemine GERİYE UYUMLU
    olarak düşülmeli — regresyon yok."""

    def test_no_templates_uses_bezel_path(self, isolated_calibration_config, isolated_roi_config, monkeypatch):
        main._last_known_corners.pop("ui_screen", None)
        calibration_store.save_reference("ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]])
        current_corners = np.array([[5, 5], [105, 5], [105, 105], [5, 105]], dtype=np.float32)
        monkeypatch.setattr(main, "detect_screen_corners", lambda frame: current_corners)

        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        adjusted, uncertain = main._adjust_rois_for_drift(
            "ui_screen", np.zeros((100, 100, 3), dtype=np.uint8), rois
        )
        assert uncertain is False
        assert adjusted[0]["x"] == pytest.approx(15, abs=1)
        assert adjusted[0]["y"] == pytest.approx(15, abs=1)


class TestCalibrationToggle:
    """Görev B — kalibrasyon açık/kapalı toggle'ı."""

    def test_disabled_skips_bezel_and_template_lookup_entirely(
        self, isolated_calibration_config, isolated_roi_config, monkeypatch
    ):
        # Hem bezel referansı hem şablon kayıtlı olsun — toggle kapalıyken
        # İKİSİNE DE bakılmamalı (CPU tasarrufu iddiasının kanıtı).
        calibration_store.save_reference("ui_screen", [[0, 0], [100, 0], [100, 100], [0, 100]])
        calibration_store.add_template("ui_screen", anchor=[1.0, 1.0], size=[5, 5], image_bytes=b"\xff\xd8\xff")
        calibration_store.set_enabled("ui_screen", False)

        def _boom(*_args, **_kwargs):
            raise AssertionError("kalibrasyon kapalıyken hiçbir referans araması yapılmamalı")

        monkeypatch.setattr(main, "detect_screen_corners", _boom)
        monkeypatch.setattr(main, "find_template_anchor", _boom)

        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", frame, rois)
        assert adjusted == rois
        assert "quad" not in adjusted[0]
        assert uncertain is False

    def test_enabled_true_runs_template_path_normally(
        self, isolated_calibration_config, isolated_roi_config, monkeypatch
    ):
        main._last_known_anchors.pop("ui_screen", None)
        calibration_store.set_enabled("ui_screen", True)
        # Gerçek imdecode edilebilir küçük bir JPEG lazım (find_template_anchor
        # monkeypatch'lenecek olsa da, ondan ÖNCEKİ cv2.imdecode adımı gerçek
        # bayt bekliyor — geçersiz bayt template_img=None üretip find_template_anchor'a
        # hiç ulaşmadan atlar, bu da testi anlamsızlaştırır).
        tiny = np.zeros((5, 5, 3), dtype=np.uint8)
        ok, jpg = cv2.imencode(".jpg", tiny)
        assert ok
        calibration_store.add_template("ui_screen", anchor=[0.0, 0.0], size=[5, 5], image_bytes=jpg.tobytes())
        monkeypatch.setattr(
            main, "find_template_anchor", lambda frame, template, min_confidence=0.6: (10.0, 5.0)
        )

        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        adjusted, uncertain = main._adjust_rois_for_drift("ui_screen", frame, rois)
        assert uncertain is False
        # anchor=(0,0) -> bulunan=(10,5) => öteleme (+10,+5)
        assert adjusted[0]["x"] == pytest.approx(20, abs=1)
        assert adjusted[0]["y"] == pytest.approx(15, abs=1)


class TestAdjustRoisForDriftJitterRegression:
    """ÇOK ÖNEMLİ — Fatih'in saha şikayetinin kanıtla kapatılması: 'kamera
    hiç hareket etmese bile ROI'ler kayıyor, boyutları küçülüp büyüyor'.

    Şablon eşleştirme yöntemi aynı kare için DETERMİNİSTİK olmalı: aynı
    kareyi art arda 2 kere _adjust_rois_for_drift'e verince dönen ROI
    koordinatları BİREBİR AYNI çıkmalı. İkinci test (küçük sensör gürültüsü)
    ek kanıt: fiziksel olarak hiç oynamamış bir sahnenin bile kareden kareye
    taşıdığı ufak piksel gürültüsü altında ROI 1px toleransın içinde kalmalı
    — şikayetin muhtemel gerçek kaynağı (eski yöntemin bu gürültüye aşırı
    duyarlı olması) artık üretmiyor."""

    def test_identical_frame_given_twice_produces_byte_identical_roi_coords(
        self, isolated_calibration_config, isolated_roi_config
    ):
        main._last_known_anchors.pop("ui_screen", None)
        frame = _frame_with_marker(marker_pos=(80, 60))
        _register_template_from_frame("ui_screen", frame, marker_pos=(80, 60))
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]

        adjusted_1, uncertain_1 = main._adjust_rois_for_drift("ui_screen", frame.copy(), rois)
        adjusted_2, uncertain_2 = main._adjust_rois_for_drift("ui_screen", frame.copy(), rois)

        assert uncertain_1 is False
        assert uncertain_2 is False
        assert adjusted_1[0]["x"] == adjusted_2[0]["x"]
        assert adjusted_1[0]["y"] == adjusted_2[0]["y"]
        assert adjusted_1[0]["w"] == adjusted_2[0]["w"]
        assert adjusted_1[0]["h"] == adjusted_2[0]["h"]
        np.testing.assert_array_equal(np.array(adjusted_1[0]["quad"]), np.array(adjusted_2[0]["quad"]))

    def test_tiny_sensor_noise_between_frames_keeps_roi_within_one_pixel(
        self, isolated_calibration_config, isolated_roi_config
    ):
        main._last_known_anchors.pop("ui_screen", None)
        base_frame = _frame_with_marker(marker_pos=(80, 60))
        _register_template_from_frame("ui_screen", base_frame, marker_pos=(80, 60))
        rois = [{"name": "a", "x": 10, "y": 10, "w": 20, "h": 20, "kind": "numeric"}]

        rng = np.random.default_rng(7)
        noisy_frame = base_frame.astype(np.int16) + rng.integers(-3, 4, size=base_frame.shape)
        noisy_frame = np.clip(noisy_frame, 0, 255).astype(np.uint8)

        adjusted_clean, uncertain_clean = main._adjust_rois_for_drift("ui_screen", base_frame.copy(), rois)
        adjusted_noisy, uncertain_noisy = main._adjust_rois_for_drift("ui_screen", noisy_frame, rois)

        assert uncertain_clean is False
        assert uncertain_noisy is False
        assert adjusted_noisy[0]["x"] == pytest.approx(adjusted_clean[0]["x"], abs=1)
        assert adjusted_noisy[0]["y"] == pytest.approx(adjusted_clean[0]["y"], abs=1)
        assert adjusted_noisy[0]["w"] == pytest.approx(adjusted_clean[0]["w"], abs=1)
        assert adjusted_noisy[0]["h"] == pytest.approx(adjusted_clean[0]["h"], abs=1)
