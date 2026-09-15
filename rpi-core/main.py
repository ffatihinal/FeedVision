"""
FeedVision RPi Core — Chamber Camera / UI Screen Camera akış sunucusu + STM32 köprüsü

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
import json
import logging
import re
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import psutil
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import calibration_store
import journal
import roi_store
import rules_store
from rule_engine import evaluate_rules
from screen_calibration import compute_warp_matrix, detect_screen_corners, warp_roi_rect
from screen_reader import read_roi
from serial_bridge import bridge
from vision import CAMERA_NUMS, STREAM_SIZE, vision

VALID_CAM_IDS = set(CAMERA_NUMS)  # {"chamber", "ui_screen"}

# Kural Motoru (madde 1 ROI kural mantığı + madde 2 güvenlik interlock +
# madde 7 aralık dışı alarm — bkz. rule_engine.py docstring'i) kaç saniyede
# bir kontrol yapacağı. 2sn seçildi: OCR+kırpma işlemi (~birkaç 10ms, bkz.
# read-test duration_ms) yanında ek yük yaratmaz, ama "nadiren" olan bir
# arızayı (proje kapsamı) makul sürede yakalar. Sahada gerekirse kısaltılır.
RULE_CHECK_INTERVAL_S = 2.0

# En son kural değerlendirmesinin sonucu — /rules/status ve /ws/status bunu
# okur. Modül seviyesinde tutuluyor (bridge/vision ile aynı desen): tek
# süreç, tek paylaşılan durum, thread/task güvenliği için ekstra kilide
# gerek yok çünkü SADECE _rule_engine_loop() yazıyor, başkaları sadece okuyor.
_current_violations: list[dict] = []
_current_skipped: list[dict] = []
_rule_engine_task: "asyncio.Task | None" = None

# Operasyonel journal (madde 6): kural motorundan (2sn) daha seyrek —
# ekrandaki değerler bu sıklıkta değişse bile her 2sn'de bir diske yazmak
# günlük dosyayı gereksiz şişirir; 10sn "ne oldu" sorusuna cevap vermek için
# yeterli çözünürlük, disk/CPU yükü ihmal edilebilir düzeyde kalır.
JOURNAL_INTERVAL_S = 10.0
_journal_task: "asyncio.Task | None" = None

# ROI drift düzeltme: bir önceki karede gerçekten bulunan bezel köşeleri,
# kamera başına bellekte tutulur. Neden gerekli: kalibrasyon anındaki
# REFERANS köşeler sabit ama bezel HER karede yeniden aranıyor — tek bir
# karede (ör. anlık parlama/glare) bulunamazsa, tamamen ROI'yi düzeltmeden
# (kalibrasyon-öncesi ham haline) dönmek yerine son bilinen iyi köşeyi
# kullanmak daha az sıçramalı/daha güvenli bir davranış. Süreç yeniden
# başlarsa (servis restart) sıfırlanır — sorun değil, bir sonraki başarılı
# karede yeniden dolar.
_last_known_corners: dict[str, "np.ndarray"] = {}


class _PollingAccessLogFilter(logging.Filter):
    """/system/temp ve /system/resources icin uvicorn erisim log satirlarini bastirir.

    Web app'teki sicaklik/CPU/RAM panelleri bu iki endpoint'e saniyede birkac
    kez fetch atiyor; bu da journalctl'e (ve oradan "Sistem Logları" pop-up'ina)
    saniyede birkac satir rutin polling gurultusu dusuruyor ve gercek
    hata/baslama-durma kayitlarini bulmayi imkansiz hale getiriyor. Baska hicbir
    endpoint'in (motor, seri, kamera, /system/logs, /system/uptime, /vision/*
    dahil) erisim logu ya da uygulama seviyesindeki hata/baslama-durma loglari
    bu filtreden etkilenmez — sadece asagidaki iki path icin uvicorn.access
    kaydini eler.
    """

    _SUPPRESSED_PATHS = ("/system/temp", "/system/resources")

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access, path'i genelde record.args icinde (request_line olarak)
        # tasir; olmadigi durumda getMessage() ile birlesmis mesaja da bakiyoruz —
        # ikisi de kapsanmazsa gurultusuz her seyi (varsayilan) gecirmeye devam eder.
        message = record.getMessage()
        if any(path in message for path in _PollingAccessLogFilter._SUPPRESSED_PATHS):
            return False
        return True


# Modul import edilir edilmez (fonksiyon govdesine gomulu degil, ust seviyede)
# calisir — bu sayede hem systemd'nin `python3 -m uvicorn main:app` cagrisinda
# hem de `python3 main.py`'nin kendi `uvicorn.run(...)` cagrisinda ayni sekilde
# devreye girer, ayrica cagri yolundan bagimsizdir.
logging.getLogger("uvicorn.access").addFilter(_PollingAccessLogFilter())

# Modul import edilir edilmez (surec baslarken) sabitlenir — restart olunca
# otomatik yenilenir, "kod guncellendi ama eski surec calismaya devam ediyordu"
# durumunu web'den fark edebilmek icin (bkz. /system/uptime).
SERVER_START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _rule_engine_task, _journal_task
    # Servis açılırken iki kamerayı da açmayı dener (biri takılı değilse
    # diğerini/motor kontrolünü engellemez), kapanırken serbest bırakır —
    # systemd restart'ta "device busy" ile kilitlenmesin diye.
    vision.start()
    psutil.cpu_percent()  # priming cagrisi: ilk cagri referans alir, anlamli deger dondurmez —
    # asagidaki /system/resources'daki interval=None cagrilari bastan itibaren dogru deger versin diye.
    _rule_engine_task = asyncio.create_task(_rule_engine_loop())
    _journal_task = asyncio.create_task(_journal_loop())
    yield
    _rule_engine_task.cancel()
    _journal_task.cancel()
    vision.stop()


app = FastAPI(title="FeedVision RPi Core", lifespan=lifespan)

# Sistemsel journal ucu icin sabitler: unit adi disaridan verilemez (guvenlik),
# istenen satir sayisina ust sinir var (asiri yuklenmeyi/CPU'yu bogmayi onlemek icin).
JOURNAL_UNIT = "feedvision"
JOURNAL_MAX_LINES = 1000


@app.get("/vision/{cam_id}/stream")
def vision_stream(cam_id: str):
    """MJPEG canlı akış. cam_id: chamber (Chamber Camera) / ui_screen (UI Screen Camera)."""
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
    """Tek kare JPEG — UI Screen Camera OCR/debug için (ileride görüntü işleme adımı)."""
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    jpg = vision.capture_jpeg(cam_id)
    if jpg is None:
        raise HTTPException(status_code=503, detail=vision.errors.get(cam_id) or "Kamera açılamadı")
    return Response(content=jpg, media_type="image/jpeg")


# "Scan Save" (madde 8): operatör bir anı (ör. şüpheli bir okuma) kalıcı
# olarak kaydetmek isterse tek tıkla — tarih-saat dosya adıyla diske yazılır.
# snapshot endpoint'inden FARKI: o tarayıcıya gösterir/indirtir, bu SUNUCUDA
# kalıcı olarak saklar (operatör dosyayı sonra Pi'den alabilsin diye).
SCANS_DIR = Path(__file__).resolve().parent / "scans"


@app.post("/vision/{cam_id}/scan-save")
def vision_scan_save(cam_id: str):
    """Şimdiki kareyi tarih-saat isimli bir JPEG olarak SCANS_DIR'e kaydeder."""
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    jpg = vision.capture_jpeg(cam_id)
    if jpg is None:
        raise HTTPException(status_code=503, detail=vision.errors.get(cam_id) or "Kamera açılamadı")
    SCANS_DIR.mkdir(parents=True, exist_ok=True)
    # Dosya adında ':' gibi karakterler olmasın diye (bazı dosya sistemleri/
    # araçlar sorun çıkarır) saat kısmı da '-' ile ayrılıyor.
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{cam_id}_{timestamp}.jpg"
    path = SCANS_DIR / filename
    with open(path, "wb") as f:
        f.write(jpg)
    return {"success": True, "filename": filename}


def _adjust_rois_for_drift(cam_id: str, frame: np.ndarray, rois: list[dict]) -> tuple[list[dict], bool]:
    """Kayıtlı ROI'leri, kamera kaymasını (drift) telafi edecek şekilde günceller.

    Kalibrasyon (bkz. /vision/{cam_id}/calibrate) hiç yapılmamışsa ROI'ler
    olduğu gibi (düzeltmesiz) döner — bu özellik OPSİYONEL/geriye uyumlu,
    kalibrasyon yapılmadan da eski davranış (ham ROI) çalışmaya devam eder.

    Döner: (düzeltilmiş roi listesi, uncertain) — uncertain=True ise bezel bu
    karede bulunamadı ve son bilinen köşe (ya da hiç yoksa referansın kendisi)
    kullanıldı; çağıran taraf bunu kullanıcıya/Kural Motoru'na "ROI referansı
    belirsiz" olarak iletmeli (sessizce yanlış okumak yerine açıkça bildirmek).
    """
    reference = calibration_store.get_reference(cam_id)
    if reference is None:
        return rois, False

    reference_corners = np.array(reference["corners"], dtype=np.float32)
    current_corners = detect_screen_corners(frame)
    uncertain = False

    if current_corners is None:
        current_corners = _last_known_corners.get(cam_id)
        uncertain = True
        if current_corners is None:
            # Hiç iyi kare görülmedi (servis yeni başladı) — düzeltmesiz devam,
            # ham ROI referansla aynı olduğu için bu, "kalibrasyonsuz" ile aynı.
            return rois, True
    else:
        _last_known_corners[cam_id] = current_corners

    matrix = compute_warp_matrix(reference_corners, current_corners)
    adjusted = []
    for roi_def in rois:
        x, y, w, h = warp_roi_rect((roi_def["x"], roi_def["y"], roi_def["w"], roi_def["h"]), matrix)
        adjusted.append({**roi_def, "x": x, "y": y, "w": w, "h": h})
    return adjusted, uncertain


@app.post("/vision/{cam_id}/calibrate")
def vision_calibrate(cam_id: str):
    """ROI drift düzeltme referansını (bezel köşeleri) şimdiki kareden yeniden kaydeder.

    Ne zaman çağrılır: kamera ilk kurulduğunda/ROI'ler ilk çizildiğinde, ya da
    kamera fiziksel olarak yeniden konumlandırıldığında (operatör elle
    tetikler — otomatik değil, çünkü "yeni konum artık doğru" kararı insan
    kararı). Bulunamazsa eski referans DOKUNULMADAN kalır (yarım/yanlış
    referansla üzerine yazmamak için).
    """
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    jpg = vision.capture_jpeg(cam_id)
    if jpg is None:
        raise HTTPException(status_code=503, detail=vision.errors.get(cam_id) or "Kamera açılamadı")
    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=500, detail="Kare çözümlenemedi (JPEG decode hatası)")
    corners = detect_screen_corners(frame)
    if corners is None:
        raise HTTPException(
            status_code=422,
            detail="Ekran çerçevesi (bezel) bu karede bulunamadı — kamera açısını/ışığı kontrol edip tekrar deneyin.",
        )
    entry = calibration_store.save_reference(cam_id, corners.tolist())
    _last_known_corners[cam_id] = corners
    return {"success": True, "calibration": entry}


@app.get("/vision/{cam_id}/calibration")
def vision_get_calibration(cam_id: str):
    """Kamera için kayıtlı kalibrasyon referansını döner (hiç yapılmamışsa null)."""
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    return {"calibration": calibration_store.get_reference(cam_id)}


def _capture_frame(cam_id: str) -> np.ndarray | None:
    """Kameradan tek kare alip decode eder. Kamera kapaliysa/decode
    basarisizsa None doner (cagiran taraf HTTP hatasi ya da sessiz atlama
    olarak kendi baglaminda ele alir — bu fonksiyon FastAPI'ye bagli degil,
    hem endpoint hem arka plan kural dongusu tarafindan kullanilabilsin diye)."""
    jpg = vision.capture_jpeg(cam_id)
    if jpg is None:
        return None
    return cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)


def _read_all_rois(cam_id: str, frame: np.ndarray) -> tuple[list[dict], bool]:
    """Bir kamera icin kayitli TUM ROI'leri (kalibrasyona gore kaymayi
    telafi ederek) okur. Hem /vision/{cam_id}/read-test endpoint'i hem
    Kural Motoru dongusu tarafindan kullanilan ORTAK yol — iki yerde ayni
    mantigin tekrarlanip zamanla birbirinden sapmasini onler.

    Doner: (okuma sonuc listesi [{"name","roi","text","ocr_error",
    "avg_color_hsv","avg_color_rgb"}, ...], roi_reference_uncertain)
    """
    rois = roi_store.get_rois(cam_id)
    if not rois:
        return [], False
    adjusted_rois, uncertain = _adjust_rois_for_drift(cam_id, frame, rois)
    results = []
    for roi_def in adjusted_rois:
        roi_tuple = (roi_def["x"], roi_def["y"], roi_def["w"], roi_def["h"])
        result = read_roi(frame, roi_tuple)
        results.append(
            {
                "name": roi_def["name"],
                "roi": list(result.roi),
                "text": result.text,
                "ocr_error": result.ocr_error,
                "avg_color_hsv": list(result.avg_color_hsv),
                "avg_color_rgb": list(result.avg_color_rgb),
            }
        )
    return results, uncertain


@app.get("/vision/{cam_id}/read-test")
def vision_read_test(cam_id: str):
    """UI Screen Camera ekran-okuma — tek kare al, o kamera icin KAYITLI TUM ROI'leri
    (varsa kalibrasyona göre kaymayi telafi ederek) sirayla kirpar, her biri
    icin hem OCR (Tesseract) hem ortalama renk (HSV) sonucu doner.

    Kayitli ROI yoksa (henuz /vision/{cam_id}/rois ile hic cizilmemis)
    hata degil, bos sonuc listesi doner — bu normal/beklenen bir durum.
    """
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    if not roi_store.get_rois(cam_id):
        return {"results": [], "roi_reference_uncertain": False}
    # Sure olcumu burada basliyor (kare alinmadan hemen once) — "saniyede kac
    # kez bu islemi yapabiliriz" sorusuna cevap vermek icin; JSON serialize/
    # network gonderimi kasitli olarak disarida birakildi (bizim kontrolumuzde
    # degil, olcmenin anlami yok).
    start = time.perf_counter()
    frame = _capture_frame(cam_id)
    if frame is None:
        raise HTTPException(status_code=503, detail=vision.errors.get(cam_id) or "Kamera açılamadı / kare çözümlenemedi")
    results, uncertain = _read_all_rois(cam_id, frame)
    duration_ms = (time.perf_counter() - start) * 1000
    return {"results": results, "duration_ms": duration_ms, "roi_reference_uncertain": uncertain}


# ==============================================================================
#  ROI YONETIMI — kamera basina adlandirilmis, kalici ROI listesi
#  (operatorun canvas uzerinde elle cizdigi dikdortgenler; bkz. roi_store.py)
# ==============================================================================


class RoiDef(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class RoiListPayload(BaseModel):
    rois: list[RoiDef]


@app.get("/vision/{cam_id}/rois")
def get_rois(cam_id: str):
    """Kamera icin kayitli ROI listesini doner (hic cizilmemisse bos liste)."""
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    return {"rois": roi_store.get_rois(cam_id)}


@app.post("/vision/{cam_id}/rois")
def set_rois(cam_id: str, payload: RoiListPayload):
    """Kamera icin TUM ROI listesini degistirir (replace-all).

    Dogrulama: koordinatlar zaten Pydantic ile pozitif/sifirdan buyuk
    zorunlu kilinir; burada ayrica her ROI'nin STREAM_SIZE (kameranin
    gercek kare boyutu) sinirlari icinde kaldigi kontrol edilir — asiri
    buyuk/kare disina tasan bir ROI 400 ile reddedilir.
    """
    if cam_id not in VALID_CAM_IDS:
        raise HTTPException(status_code=404, detail=f"Bilinmeyen kamera: {cam_id}")
    frame_w, frame_h = STREAM_SIZE
    for roi in payload.rois:
        if roi.x + roi.w > frame_w or roi.y + roi.h > frame_h:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"ROI '{roi.name}' kare sınırlarını aşıyor "
                    f"(kare: {frame_w}x{frame_h}, ROI: x={roi.x}, y={roi.y}, w={roi.w}, h={roi.h})"
                ),
            )
    rois_as_dicts = [r.model_dump() for r in payload.rois]
    roi_store.save_rois(cam_id, rois_as_dicts)
    return {"success": True, "rois": roi_store.get_rois(cam_id)}


# ==============================================================================
#  KURAL MOTORU — madde 1 (ROI kural mantığı) + madde 2 (güvenlik interlock)
#  + madde 7 (aralık dışı alarm), TEK motor olarak (bkz. rule_engine.py).
#  Kurallar periyodik arka plan görevinde (_rule_engine_loop) değerlendirilir;
#  ihlal olunca motor durdurulur + alarm durumu bellekte tutulup UI'a
#  (/rules/status, /ws/status) yansıtılır.
# ==============================================================================


class RuleDef(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    source: str  # "roi" | "stm32"
    cam_id: str | None = None  # source="roi" ise zorunlu
    roi_name: str | None = None  # source="roi" ise zorunlu
    field: str | None = None  # source="stm32" ise zorunlu
    min: float | None = None
    max: float | None = None
    stop_motor: bool = False
    enabled: bool = True


class RuleListPayload(BaseModel):
    rules: list[RuleDef]


@app.get("/rules")
def get_rules():
    """Kayıtlı TÜM kuralları döner (hiç tanımlanmamışsa boş liste)."""
    return {"rules": rules_store.get_rules()}


@app.post("/rules")
def set_rules(payload: RuleListPayload):
    """TÜM kural listesini değiştirir (replace-all, ROI yönetimiyle aynı desen).

    Basit doğrulama: source="roi" için cam_id+roi_name, source="stm32" için
    field zorunlu — eksikse kural sessizce yanlış çalışmak yerine 400 ile
    reddedilir (ör. hangi ROI/alan izleneceği belirsiz bir kural, motor
    durdurma kararını asla veremeyecek bir kural demektir, kaydedilmemeli).
    """
    for rule in payload.rules:
        if rule.source == "roi" and not (rule.cam_id and rule.roi_name):
            raise HTTPException(status_code=400, detail=f"Kural '{rule.name}': source=roi için cam_id+roi_name zorunlu")
        if rule.source == "stm32" and not rule.field:
            raise HTTPException(status_code=400, detail=f"Kural '{rule.name}': source=stm32 için field zorunlu")
        if rule.source not in ("roi", "stm32"):
            raise HTTPException(status_code=400, detail=f"Kural '{rule.name}': bilinmeyen source '{rule.source}'")
    rules_as_dicts = [r.model_dump() for r in payload.rules]
    rules_store.save_rules(rules_as_dicts)
    return {"success": True, "rules": rules_store.get_rules()}


@app.get("/rules/status")
def get_rules_status():
    """En son kural değerlendirmesinin sonucu — UI'ın alarm banner'ı ve
    "şu kural şu an okunamıyor" listesi bunu periyodik olarak çeker."""
    return {"violations": _current_violations, "skipped": _current_skipped}


def _collect_roi_readings(cam_ids: set[str]) -> dict[tuple[str, str], dict]:
    """Verilen kameralardan kayıtlı TÜM ROI'leri okuyup, kural motorunun
    beklediği {(cam_id, roi_name): {"text":..., "avg_color_hsv":...}}
    formatına çevirir. Kamera açılamazsa o kamera sessizce atlanır (bağlı
    kural değerlendirilemez -> skipped listesine düşer, motoru durdurmaz —
    bkz. rule_engine.py'deki "okunamayan değer ihlal sayılmaz" prensibi)."""
    readings: dict[tuple[str, str], dict] = {}
    for cam_id in cam_ids:
        frame = _capture_frame(cam_id)
        if frame is None:
            continue
        results, _uncertain = _read_all_rois(cam_id, frame)
        for r in results:
            readings[(cam_id, r["name"])] = r
    return readings


async def _rule_engine_loop():
    """Arka planda sürekli çalışır: RULE_CHECK_INTERVAL_S'te bir kayıtlı
    kuralları değerlendirir, ihlal varsa motoru durdurur + alarm durumunu
    günceller. lifespan() içinde başlatılır/iptal edilir (bkz. yukarısı)."""
    global _current_violations, _current_skipped
    while True:
        try:
            rules = rules_store.get_rules()
            if rules:
                cam_ids_needed = {r["cam_id"] for r in rules if r.get("source") == "roi" and r.get("cam_id")}
                roi_readings = _collect_roi_readings(cam_ids_needed) if cam_ids_needed else {}
                stm32_status = bridge.get_status()
                violations, skipped = evaluate_rules(rules, roi_readings, stm32_status)
                _current_violations = [v.__dict__ for v in violations]
                _current_skipped = [s.__dict__ for s in skipped]
                # Herhangi bir ihlal stop_motor=true ise motoru durdur. Her
                # döngüde tekrar gönderiliyor (ihlal sürdüğü sürece) — "bir
                # kere durdur, unut" değil, interlock ihlal bitene kadar
                # ısrarla durdurmalı (ör. UI'dan yanlışlıkla tekrar
                # başlatılırsa bir sonraki döngüde yine durdurulur).
                if any(v.stop_motor for v in violations):
                    bridge.send_command({"cmd": "stop"})
            else:
                _current_violations = []
                _current_skipped = []
        except Exception:  # noqa: BLE001 — arka plan görevi hicbir hatada tamamen olmemeli
            logging.getLogger("feedvision.rules").exception("Kural Motoru dongusunde beklenmeyen hata")
        await asyncio.sleep(RULE_CHECK_INTERVAL_S)


async def _journal_loop():
    """Arka planda sürekli çalışır: JOURNAL_INTERVAL_S'te bir, o an ekrandan
    takip edilen TÜM değerleri (kural motorunun baktığı belirli ROI'lerle
    sınırlı değil — kayıtlı her iki kameranın da tüm ROI'leri + STM32
    durumu) günün journal dosyasına ekler (bkz. journal.py). lifespan()
    içinde başlatılır/iptal edilir."""
    while True:
        try:
            roi_readings: dict = {}
            for cam_id in VALID_CAM_IDS:
                if not roi_store.get_rois(cam_id):
                    continue
                frame = _capture_frame(cam_id)
                if frame is None:
                    continue
                results, uncertain = _read_all_rois(cam_id, frame)
                roi_readings[cam_id] = {"results": results, "roi_reference_uncertain": uncertain}
            journal.write_entry(
                {
                    "operator_username": _current_operator["username"],
                    "stm32_status": bridge.get_status(),
                    "stm32_connected": bridge.is_connected,
                    "roi_readings": roi_readings,
                    "rule_violations": _current_violations,
                }
            )
        except Exception:  # noqa: BLE001 — arka plan görevi hicbir hatada tamamen olmemeli
            logging.getLogger("feedvision.journal").exception("Journal dongusunde beklenmeyen hata")
        await asyncio.sleep(JOURNAL_INTERVAL_S)


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
                    # Kural Motoru'nun en son değerlendirmesi — UI polling'e
                    # gerek kalmadan alarm banner'ını canlı güncelleyebilsin
                    # diye zaten var olan bu akışa iğnelendi (ayrı bir
                    # WebSocket açmaya gerek yok).
                    "rule_violations": _current_violations,
                    "rule_skipped": _current_skipped,
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
#  OPERASYONEL JOURNAL (madde 6) — yukaridaki sistemsel journal'dan AYRI:
#  bu, servis basladi/durdu degil, EKRANDAN TAKIP EDILEN DEGERLERIN periyodik
#  kaydi (bkz. journal.py + yukaridaki _journal_loop). Gun basina bir
#  .jsonl dosyasi.
# ==============================================================================

OPERATIONAL_JOURNAL_MAX_LINES = 2000


@app.get("/journal/today")
def journal_today(lines: int = 200):
    """Bugunun operasyonel journal dosyasindan son N satiri (parse edilmis
    JSON listesi olarak) doner. Henuz hic yazilmadiysa (servis yeni acildi,
    ilk JOURNAL_INTERVAL_S dolmadi) bos liste doner — hata degil."""
    if lines < 1:
        raise HTTPException(status_code=400, detail="lines 1 veya daha buyuk olmali")
    lines = min(lines, OPERATIONAL_JOURNAL_MAX_LINES)

    path = journal._file_path_for_today()
    if not path.exists():
        return {"date": path.stem, "entries": []}

    with open(path, encoding="utf-8") as f:
        all_lines = f.readlines()
    tail = all_lines[-lines:]
    entries = []
    for raw_line in tail:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entries.append(json.loads(raw_line))
        except json.JSONDecodeError:
            continue  # yarim yazilmis son satir olabilir (crash aninda) — sessizce atla
    return {"date": path.stem, "entries": entries}


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


@app.get("/system/resources")
def system_resources():
    """CPU ve RAM kullanimini `{"cpu_percent": 12.3, "ram_percent": 41.7}` olarak doner.

    psutil hem Mac hem Pi'de calisir (vcgencmd gibi Pi'ye ozel degil).
    interval=None non-blocking'tir (lifespan'daki priming cagrisina bagli) —
    yine de beklenmedik bir hata cikarsa sunucu cokmesin diye 503 + gercek
    hata metni donulur (yukaridaki /system/temp pattern'iyle ayni felsefe).
    """
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
        ram_percent = psutil.virtual_memory().percent
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"psutil hata verdi: {exc}")

    return {"cpu_percent": cpu_percent, "ram_percent": ram_percent}


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


def _read_ui_file(filename: str) -> str:
    """ui/ klasöründen bir HTML dosyasını okur. __file__'e göre yol kurar —
    script'in nereden çalıştırıldığına (VS Code Play, terminalden farklı
    bir klasörden vb.) bağlı kalmasın diye. Landing/operator/admin/wall
    sayfalarının hepsi bu ortak yardımcıyı kullanır."""
    ui_path = Path(__file__).resolve().parent.parent / "ui" / filename
    with open(ui_path, encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def landing():
    """FeedVision landing page — "Operatör" / "Admin" seçim ekranı.

    15-09-2026 karar: Admin/Operatör ayrımı GÜVENLİK değil KULLANIM
    KOLAYLIĞI amaçlı (aynı yetki seviyesindeki insanlar kullanıyor) — bu
    yüzden admin tarafında şifre YOK. Operatör'e giden kullanıcı adı
    sorusu da gerçek bir login değil, sadece "kim kullandı" bilgisini
    operasyonel journal'a düşmek için (bkz. /operator/session).
    """
    return _read_ui_file("landing.html")


# Şu an operasyon başında olan kişinin adı — gerçek bir auth/oturum SİSTEMİ
# DEĞİL (madde 1'deki karar: şifre yok, sadece "kim kullandı" bilgisi).
# Operatör ekranı açılıp isim girildiğinde /operator/session ile güncellenir;
# journal döngüsü (bkz. _journal_loop) bunu her kayda ekler. Tek kiosk
# istasyonu senaryosuna uygun tek/paylaşılan bir global — birden fazla
# operatörün aynı anda ayrı oturumu yok, en son giren "şu an kullanan" sayılır.
_current_operator: dict = {"username": None, "since": None}


class OperatorSessionPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@app.post("/operator/session")
def set_operator_session(payload: OperatorSessionPayload):
    """Operatör ekranı açılırken girilen kullanıcı adını kaydeder — DOĞRULAMA
    YOK (parola değil), sadece journal kayıtlarına düşecek bir etiket."""
    _current_operator["username"] = payload.username.strip()
    _current_operator["since"] = time.time()
    return {"success": True, "operator": dict(_current_operator)}


@app.get("/operator/session")
def get_operator_session():
    """Şu an kayıtlı operatör adını döner (hiç girilmediyse username=None)."""
    return {"operator": dict(_current_operator)}


@app.get("/operator", response_class=HTMLResponse)
def operator_page():
    """Operatör ekranı — sade, teknik detaysız (bkz. UI/UX planı: DURDUR,
    canlı encoder mm, kamera görüntüleri, alarm banner, Scan Save)."""
    return _read_ui_file("operator.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    """Admin ekranı — tam erişim (ROI/kalibrasyon, kural tanımlama, ham
    motor komutları, sistem logları/journal, hareket analizi). Şifre YOK
    (15-09-2026 kararı — bkz. landing() docstring'i)."""
    return _read_ui_file("admin.html")


@app.get("/shared.js")
def shared_js():
    """Operatör + Admin sayfalarının paylaştığı JS (bkz. ui/shared.js docstring'i).
    Tek dosya, statik dosya sunucusu (StaticFiles) kurmaya gerek olmayacak
    kadar küçük bir yüzey — diğer sayfa route'larıyla aynı basit desen."""
    return Response(content=_read_ui_file("shared.js"), media_type="application/javascript")


@app.get("/wall", response_class=HTMLResponse)
def wall():
    """Canlı yayın duvarı: 2x2 grid (Chamber Camera, UI Screen Camera, FeedVision UI, custom alan) —
    TV/telefon gibi izleme amaçlı bağımsız sayfa, ana kontrol UI'sinden ayrı
    (bkz. ui/wall.html üstündeki mimari not). Mevcut hiçbir endpoint'in
    davranışı değişmiyor, sadece statik HTML servis eden yeni bir uç."""
    return _read_ui_file("wall.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
