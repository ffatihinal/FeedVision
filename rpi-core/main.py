"""
FeedVision RPi Core — AP1/AP2 kamera akış sunucusu + STM32 köprüsü

Ne yapar: FastAPI ile küçük bir web sunucusu açar, tarayıcıdan "Kamera Aç"
butonlarına basınca canlı görüntüyü MJPEG olarak akıtır; ayrıca STM32'ye
seri port üzerinden JSON komut gönderir, canlı durumu WebSocket ile akıtır.

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

import asyncio
import threading
from pathlib import Path

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from serial_bridge import bridge

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


# ==============================================================================
#  STM32 KÖPRÜSÜ — seri port bağlantısı + motor komutları
#  Protokol: /docs/protocol.md (STM32 firmware'i ile aynı JSON sözleşmesi)
# ==============================================================================


@app.get("/serial/ports")
def list_serial_ports():
    """UI'da açılır listeye doldurulacak, o an takılı seri portlar."""
    return {"ports": bridge.list_available_ports()}


@app.post("/serial/connect")
def connect_serial(port: str):
    ok = bridge.connect(port)
    return {"success": ok, "error": bridge.last_error}


@app.post("/serial/disconnect")
def disconnect_serial():
    bridge.disconnect()
    return {"success": True}


class StepCommand(BaseModel):
    dir: int  # 0 veya 1 (yön)
    delay: int  # mikrosaniye, iki darbe arası (küçük = hızlı)
    steps: int  # atılacak toplam darbe sayısı


@app.post("/motor/step")
def motor_step(c: StepCommand):
    ok = bridge.send_command({"cmd": "step", "dir": c.dir, "delay": c.delay, "steps": c.steps})
    return {"sent": ok}


@app.post("/motor/stop")
def motor_stop():
    ok = bridge.send_command({"cmd": "stop"})
    return {"sent": ok}


class DcCommand(BaseModel):
    dir: str  # "forward" / "backward" / "stop"


@app.post("/motor/dc")
def motor_dc(c: DcCommand):
    ok = bridge.send_command({"cmd": "dc", "dir": c.dir})
    return {"sent": ok}


@app.post("/motor/reset")
def motor_reset():
    ok = bridge.send_command({"cmd": "reset"})
    return {"sent": ok}


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    """Tarayıcıya STM32'nin en son durumunu saniyede ~10 kez akıtır
    (WebSocket ile — sayfa yenilenmeden canlı gösterge güncellenir)."""
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(
                {
                    "is_connected": bridge.is_connected,
                    "last_error": bridge.last_error,
                    "status": bridge.get_status(),
                }
            )
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass


@app.get("/", response_class=HTMLResponse)
def index():
    # __file__'e göre yol kur — script'in nereden calistirildigina (VS Code
    # Play, terminalden farkli bir klasorden vb.) bagli kalmasin diye.
    ui_path = Path(__file__).resolve().parent.parent / "ui" / "index.html"
    with open(ui_path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
