"""
FeedVision — AP1/AP2 kamera yonetimi (picamera2/libcamera tabanli)

Ne yapar: Pi Camera'larin iki CSI portunu (cam1 = AP1 chamber izleme,
cam2 = AP2 UA cihazi ekran okuma) picamera2 ile acar/kapatir; MJPEG
canli akis ve tek kare (snapshot) uretir. Motor/seri koduna (main.py'deki
STM32 koprusu, serial_bridge.py) dokunmaz, main.py'nin router'ina
ayrica eklenir.

Neden picamera2: RPi 5 + Pi Camera CSI modulleri libcamera gerektiriyor;
eski OpenCV/V4L2 (cv2.VideoCapture) yaklasimi Pi'de calismiyordu
(donanim `rpicam-hello` ile ayrica dogrulandi). Detay:
Azobex_WP1/kamera_entegrasyon_onerisi.md.
"""

import threading
import time

import cv2

try:
    from picamera2 import Picamera2

    PICAMERA2_AVAILABLE = True
except ImportError:
    # Gelistirme makinesi (Mac) picamera2'yi kuramaz — libcamera'ya bagli.
    # Modul yine de import edilebilsin diye burada yutuyoruz; gercek acma
    # denemesi start()'ta yapilir ve hata mesaji olarak saklanir.
    Picamera2 = None
    PICAMERA2_AVAILABLE = False

# cam_id (HTTP'ye/UI'a donuk isim) -> picamera2 camera_num (donanim CSI indeksi)
CAMERA_NUMS = {"cam1": 0, "cam2": 1}  # cam1 = AP1 chamber, cam2 = AP2 ekran

STREAM_SIZE = (1280, 720)  # 720p hedefi — TV/tablet icin yeterli, Pi 5 CPU'yu bogmaz
STREAM_FPS = 12.0  # 10-15fps hedef araligi (kamera_entegrasyon_onerisi.md Bolum 1)


class VisionManager:
    """Iki kamerayi da acik/kapali tutan tekil nesne — main.py'de app.state.vision olarak saklanir.

    FastAPI lifespan icinde start()/stop() cagrilir ki systemd restart'ta
    kameralar duzgun serbest kalsin (aksi halde "device busy" ile bir
    sonraki baslatma kilitlenebilir).
    """

    def __init__(self) -> None:
        self._cameras: dict[str, "Picamera2"] = {}
        self._locks: dict[str, threading.Lock] = {cam_id: threading.Lock() for cam_id in CAMERA_NUMS}
        # Kamera acilamadiysa (takili degil, izin yok, picamera2 kurulu degil)
        # neden burada saklanir — endpoint bunu HTTP hata govdesinde dondurur.
        self.errors: dict[str, str | None] = {cam_id: None for cam_id in CAMERA_NUMS}

    def start(self) -> None:
        """Iki kamerayi da acmayi dener. Biri takili degilse/hata verirse
        digerini ve servisin geri kalanini (motor kontrolu) engellemez."""
        if not PICAMERA2_AVAILABLE:
            for cam_id in CAMERA_NUMS:
                self.errors[cam_id] = "picamera2 kurulu degil (bu makine Raspberry Pi degil mi?)"
            return
        for cam_id, cam_num in CAMERA_NUMS.items():
            try:
                picam2 = Picamera2(camera_num=cam_num)
                config = picam2.create_video_configuration(main={"size": STREAM_SIZE, "format": "RGB888"})
                picam2.configure(config)
                picam2.start()
                self._cameras[cam_id] = picam2
                self.errors[cam_id] = None
            except Exception as exc:  # noqa: BLE001 — donanim/izin hatasi tipi onceden bilinmiyor
                self.errors[cam_id] = str(exc)

    def stop(self) -> None:
        """Servis kapanirken cagrilir — kameralari serbest birakir."""
        for picam2 in self._cameras.values():
            try:
                picam2.stop()
                picam2.close()
            except Exception:  # noqa: BLE001 — kapanista hata olsa da devam et
                pass
        self._cameras.clear()

    def get(self, cam_id: str):
        return self._cameras.get(cam_id)

    def capture_jpeg(self, cam_id: str) -> bytes | None:
        """Tek kare yakalayip JPEG'e kodlar. Kamera acik degilse None doner."""
        picam2 = self.get(cam_id)
        if picam2 is None:
            return None
        lock = self._locks[cam_id]
        with lock:
            frame = picam2.capture_array()
        ok, jpg = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        return jpg.tobytes()

    def mjpeg_generator(self, cam_id: str, fps: float = STREAM_FPS):
        """Surekli kare okuyup MJPEG (art arda JPEG, multipart) formatinda akitir.

        Kamera koparsa/hata verirse jenerator sessizce biter — StreamingResponse
        baglantiyi kapatir, tarayicidaki <img> "error" olayini tetikler.
        """
        interval = 1.0 / fps if fps > 0 else 0.0
        while True:
            jpg_bytes = self.capture_jpeg(cam_id)
            if jpg_bytes is None:
                break
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg_bytes + b"\r\n")
            if interval:
                time.sleep(interval)


# Modul seviyesinde tekil nesne — main.py bunu import edip lifespan'da
# start()/stop() cagirir, endpoint'ler dogrudan bu nesneyi kullanir.
vision = VisionManager()
