"""
FeedVision — VisionManager.set_exposure() testleri.

Picamera2 gercek donanima (libcamera) bagli oldugu icin gercek kamera
acilamiyor (Mac/CI'da PICAMERA2_AVAILABLE=False) — bu yuzden gercek
picam2 nesnesi yerine sadece set_controls() cagrisini kaydeden SAHTE
(fake) bir nesne VisionManager._cameras'a dogrudan enjekte ediliyor.
Bu, set_exposure()'in dogru controls sozlugunu urettigini test eder,
picamera2/libcamera'nin kendisini degil (o zaten kutuphanenin sorumlulugu).
"""

from vision import VisionManager


class FakePicam2:
    def __init__(self):
        self.controls_calls: list[dict] = []
        self.raise_on_call = False

    def set_controls(self, controls: dict) -> None:
        if self.raise_on_call:
            raise RuntimeError("sahte donanim hatasi")
        self.controls_calls.append(controls)


class TestSetExposure:
    def test_camera_not_open_returns_false_with_message(self):
        manager = VisionManager()
        ok, error = manager.set_exposure("ui_screen", auto=True)
        assert ok is False
        assert error is not None

    def test_auto_true_enables_ae(self):
        manager = VisionManager()
        fake = FakePicam2()
        manager._cameras["ui_screen"] = fake
        ok, error = manager.set_exposure("ui_screen", auto=True)
        assert ok is True
        assert error is None
        assert fake.controls_calls == [{"AeEnable": True}]

    def test_manual_sets_exposure_time_and_gain(self):
        manager = VisionManager()
        fake = FakePicam2()
        manager._cameras["ui_screen"] = fake
        ok, error = manager.set_exposure("ui_screen", auto=False, exposure_time=20000, gain=2.5)
        assert ok is True
        assert fake.controls_calls == [{"AeEnable": False, "ExposureTime": 20000, "AnalogueGain": 2.5}]

    def test_manual_without_values_only_disables_ae(self):
        manager = VisionManager()
        fake = FakePicam2()
        manager._cameras["ui_screen"] = fake
        manager.set_exposure("ui_screen", auto=False)
        assert fake.controls_calls == [{"AeEnable": False}]

    def test_hardware_exception_returns_false_with_message(self):
        manager = VisionManager()
        fake = FakePicam2()
        fake.raise_on_call = True
        manager._cameras["ui_screen"] = fake
        ok, error = manager.set_exposure("ui_screen", auto=True)
        assert ok is False
        assert "sahte donanim hatasi" in error
