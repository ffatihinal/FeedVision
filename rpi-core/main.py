"""
FeedVision RPi Core — AP1/AP2 kamera akış sunucusu (MVP3 iskeleti)

Ne yapar: FastAPI ile küçük bir web sunucusu açar, tarayıcıdan "Kamera Aç"
butonlarına basınca canlı görüntüyü MJPEG olarak akıtır.

Nasıl çalıştırılır:
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt
    ./.venv/bin/python3 main.py
    # tarayıcıda: http://localhost:8000

SAHADA DEĞİŞECEK NOKTA (Pi Camera v2 için):
    CAMERA_INDEXES sözlüğü şu an OpenCV'nin genel VideoCapture arayüzünü
    kullanıyor (Mac'te test amaçlı yerleşik kamerayla denendi). Raspberry Pi
    5 + Pi Camera v2 (CSI) kombinasyonunda bu satır muhtemelen çalışmaz —
    Pi Camera'lar `picamera2` kütüphanesiyle (libcamera tabanlı) açılır.
    Sahada ilk kurulumda: `pip install picamera2` + bu dosyadaki
    `get_capture()` fonksiyonunu picamera2 API'sine göre güncelle. Bu dosya
    şimdilik "iskelet + akış mantığı doğru çalışıyor" seviyesindedir.
"""

import threading

import cv2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="FeedVision RPi Core")

# kamera id -> OpenCV cihaz indeksi. Sahada gerçek CSI kameraların index'i
# (veya picamera2'ye geçilirse kamera nesnesi) burada güncellenecek.
CAMERA_INDEXES = {1: 0, 2: 1}

_caps: dict[int, cv2.VideoCapture] = {}
_locks: dict[int, threading.Lock] = {}


def get_capture(cam_id: int):
    """Kamerayı ilk istekte açar, sonraki isteklerde aynı bağlantıyı kullanır."""
    if cam_id not in _caps:
        idx = CAMERA_INDEXES.get(cam_id)
        if idx is None:
            return None
        cap = cv2.VideoCapture(idx)
        _caps[cam_id] = cap
        _locks[cam_id] = threading.Lock()
    return _caps[cam_id]


def mjpeg_generator(cam_id: int):
    """Kameradan sürekli kare okuyup MJPEG formatında (art arda JPEG) akıtır."""
    cap = get_capture(cam_id)
    if cap is None or not cap.isOpened():
        return
    while True:
        with _locks[cam_id]:
            ok, frame = cap.read()
        if not ok:
            break
        ok, jpg = cv2.imencode(".jpg", frame)
        if not ok:
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpg.tobytes() + b"\r\n"
        )


@app.get("/camera/{cam_id}/stream")
def camera_stream(cam_id: int):
    return StreamingResponse(
        mjpeg_generator(cam_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/", response_class=HTMLResponse)
def index():
    with open("../ui/index.html", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
