"""
FeedVision RPi Core — AP1/AP2 kamera akış sunucusu + STM32 köprüsü

Ne yapar: FastAPI ile küçük bir web sunucusu açar, tarayıcıdan "Kamera Aç"
butonlarına basınca canlı görüntüyü MJPEG olarak akıtır (picamera2/libcamera
üzerinden, bkz. vision.py); ayrıca STM32'ye seri port üzerinden JSON komut
gönderir, canlı durumu WebSocket ile akıtır.

Nasıl çalıştırılır:
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt
    ./.venv/bin/python3 main.py
    # tarayıcıda: http://localhost:8000

Kamera notu: Pi Camera (CSI) kameraları `picamera2` (libcamera tabanlı)
gerektirir — bu Raspberry Pi OS dışında kurulamaz, Mac/Windows'ta
`vision.py` picamera2'siz de import edilir ama kamera endpoint'leri
503 döner (bkz. vision.py PICAMERA2_AVAILABLE).
"""

import asyncio
import re
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from serial_bridge import bridge
from vision import CAMERA_NUMS, vision

VALID_CAM_IDS = set(CAMERA_NUMS)  # {"cam1", "cam2"}

# Modul import edilir edilmez (surec baslarken) sabitlenir — restart olunca
# otomatik yenilenir, "kod guncellendi ama eski surec calismaya devam ediyordu"
# durumunu web'den fark edebilmek icin (bkz. /system/uptime).
SERVER_START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Servis açılırken iki kamerayı da açmayı dener (biri takılı değilse
    # diğerini/motor kontrolünü engellemez), kapanırken serbest bırakır —
    # systemd restart'ta "device busy" ile kilitlenmesin diye.
    vision.start()
    yield
    vision.stop()


app = FastAPI(title="FeedVision RPi Core", lifespan=lifespan)

# Sistemsel journal ucu icin sabitler: unit adi disaridan verilemez (guvenlik),
# istenen satir sayisina ust sinir var (asiri yuklenmeyi/CPU'yu bogmayi onlemek icin).
JOURNAL_UNIT = "feedvision"
JOURNAL_MAX_LINES = 1000


@app.get("/vision/{cam_id}/stream")
def vision_stream(cam_id: str):
    """MJPEG canlı akış. cam_id: cam1 (AP1 — chamber) / cam2 (AP2 — UA ekranı)."""
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    if vision.get(cam_id) is None:
        raise HTTPException(status_code=503, detail=vision.errors.get(cam_id) or "Kamera açılamadı")
    return StreamingResponse(
        vision.mjpeg_generator(cam_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/vision/{cam_id}/snapshot")
def vision_snapshot(cam_id: str):
    """Tek kare JPEG — AP2 OCR/debug için (ileride görüntü işleme adımı)."""
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    jpg = vision.capture_jpeg(cam_id)
    if jpg is None:
        raise HTTPException(status_code=503, detail=vision.errors.get(cam_id) or "Kamera açılamadı")
    return Response(content=jpg, media_type="image/jpeg")


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
    delay: int = Field(gt=0)  # mikrosaniye, hedef/sabit hız — iki darbe arası (küçük = hızlı) — 0/negatif firmware'i anlamsız hızlandırır
    steps: int = Field(gt=0)  # atılacak toplam darbe sayısı — firmware zaten <=0'ı reddediyor, burada da erken kes
    accel: int = Field(default=0, ge=0)  # kaç adımda hızlanıp/yavaşlanılacağı (0 = rampasız, sabit hız — eski davranış)


@app.post("/motor/step")
def motor_step(c: StepCommand):
    # send_command artık {"sent","raw_command","command","raw_reply","reply","timed_out"}
    # döndürüyor — UI hem gönderdiğimiz ham komutu hem STM32'nin ok/err yanıtını gösterebilsin diye.
    return bridge.send_command({"cmd": "step", "dir": c.dir, "delay": c.delay, "steps": c.steps, "accel": c.accel})


@app.post("/motor/stop")
def motor_stop():
    return bridge.send_command({"cmd": "stop"})


class DcCommand(BaseModel):
    dir: str  # "forward" / "backward" / "stop"


@app.post("/motor/dc")
def motor_dc(c: DcCommand):
    return bridge.send_command({"cmd": "dc", "dir": c.dir})


@app.post("/motor/reset")
def motor_reset():
    return bridge.send_command({"cmd": "reset"})


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


# ==============================================================================
#  SISTEMSEL JOURNAL — servisin systemd/journalctl loglarini web'den gormek icin
#  (operasyonel journal — motor komutlari/besleme miktari — AYRI ve KAPSAM DISI,
#  burada SADECE servis basladi/durdu/hata/crash gibi sistem loglari var.)
# ==============================================================================


@app.get("/system/logs", response_class=PlainTextResponse)
def system_logs(lines: int = 200):
    """Son N satir journald kaydini duz metin olarak dondurur.

    Guvenlik: unit adi sabit ("feedvision") — disaridan baska bir unit
    istenemez. `lines` JOURNAL_MAX_LINES ile sinirlanir, sunucu asiri
    yuklenmesin diye. Calismasi icin kullanicinin `systemd-journal`
    grubunda olmasi gerekir (bkz. setup_pi.sh) — aksi halde journalctl
    izin hatasi verir, bu da HTTPException 500 olarak donulur.
    """
    if lines < 1:
        raise HTTPException(status_code=400, detail="lines 1 veya daha buyuk olmali")
    lines = min(lines, JOURNAL_MAX_LINES)

    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", JOURNAL_UNIT,
                "-n", str(lines),
                "--no-pager",
                "-o", "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="journalctl bulunamadi (Linux/systemd disinda mi calisiyor?)")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="journalctl zaman asimina ugradi")

    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"journalctl hata verdi: {result.stderr.strip()}")

    return result.stdout


# ==============================================================================
#  SOC SICAKLIGI — `vcgencmd measure_temp` ile Pi'nin SoC sicakligini web'e
#  tasir (daha once elle `watch -n 0.5 vcgencmd measure_temp` ile bakiliyordu).
# ==============================================================================

VCGENCMD_TIMEOUT_S = 5
TEMP_RE = re.compile(r"temp=([\d.]+)")


@app.get("/system/temp")
def system_temp():
    """SoC sicakligini `{"temp_c": 42.8}` olarak doner.

    Mac/Windows gelistirme ortaminda `vcgencmd` bulunmaz — sunucu cokmesin
    diye 503 + gercek hata sebebi `detail` alaninda donulur (vision.py'deki
    errors pattern'iyle ayni felsefe, uydurma mesaj yok).
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True,
            text=True,
            timeout=VCGENCMD_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="vcgencmd bulunamadi (Raspberry Pi disinda mi calisiyor?)")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=503, detail="vcgencmd zaman asimina ugradi")

    if result.returncode != 0:
        raise HTTPException(status_code=503, detail=f"vcgencmd hata verdi: {result.stderr.strip()}")

    match = TEMP_RE.search(result.stdout)
    if not match:
        raise HTTPException(status_code=503, detail=f"vcgencmd ciktisi ayristirilamadi: {result.stdout.strip()!r}")

    return {"temp_c": float(match.group(1))}


@app.get("/system/uptime")
def system_uptime():
    """Su anki sürecin ne zaman başladığını ve ne kadar süredir çalıştığını döner.

    Restart farkedilebilsin diye: süreç her yeniden başladığında SERVER_START_TIME
    de otomatik yenilenir (yukarıda modül seviyesinde tanımlı) — dış komuta bağımlı
    değil, hata senaryosu yok.
    """
    return {
        "started_at": datetime.fromtimestamp(SERVER_START_TIME).astimezone().isoformat(),
        "uptime_seconds": time.time() - SERVER_START_TIME,
    }


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
