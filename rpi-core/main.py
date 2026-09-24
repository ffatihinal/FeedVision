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
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import alarm_sounds
import calibration_store
import journal
import motion_calc
import motion_params
import roi_store
import rules_store
import vision_raw_log
from feed_totalizer import totalizer as feed_totalizer
from rule_engine import evaluate_rules
from screen_calibration import compute_warp_matrix, detect_screen_corners, warp_roi_rect
from screen_reader import read_roi
from serial_bridge import bridge
from vision import CAMERA_NUMS, STREAM_SIZE, vision

VALID_CAM_IDS = set(CAMERA_NUMS)  # {"chamber", "ui_screen"}

# Kontrol Kriterleri (madde 1 ROI kriter mantığı + madde 2 güvenlik interlock +
# madde 7 aralık dışı alarm — bkz. rule_engine.py docstring'i) kaç saniyede
# bir kontrol yapacağı. 2sn seçildi: OCR+kırpma işlemi (~birkaç 10ms, bkz.
# read-test duration_ms) yanında ek yük yaratmaz, ama "nadiren" olan bir
# arızayı (proje kapsamı) makul sürede yakalar. Sahada gerekirse kısaltılır.
RULE_CHECK_INTERVAL_S = 2.0

# En son Kontrol Kriterleri değerlendirmesinin sonucu — /rules/status ve /ws/status bunu
# okur. Modül seviyesinde tutuluyor (bridge/vision ile aynı desen): tek
# süreç, tek paylaşılan durum, thread/task güvenliği için ekstra kilide
# gerek yok çünkü SADECE _rule_engine_loop() yazıyor, başkaları sadece okuyor.
_current_violations: list[dict] = []
_current_skipped: list[dict] = []
_rule_engine_task: "asyncio.Task | None" = None

# Operasyonel journal (madde 6): Kontrol Kriterleri'nden (2sn) daha seyrek —
# ekrandaki değerler bu sıklıkta değişse bile her 2sn'de bir diske yazmak
# günlük dosyayı gereksiz şişirir; 10sn "ne oldu" sorusuna cevap vermek için
# yeterli çözünürlük, disk/CPU yükü ihmal edilebilir düzeyde kalır.
JOURNAL_INTERVAL_S = 10.0
_journal_task: "asyncio.Task | None" = None

# Görüntü İşleme Ham Veri Kaydı (15-09-2026): journal.py'den AYRI, sadece
# ROI-kaynaklı Kontrol Kriterlerinin ham okumalarını insan-gözüyle-okunur
# sabit-genişlikli bir .txt'ye yazar (bkz. vision_raw_log.py). Periyodu
# Admin'den ayarlanabilir (GET/POST /vision-raw-log/config) — varsayılan
# vision_raw_log.DEFAULT_INTERVAL_S.
_vision_raw_log_task: "asyncio.Task | None" = None

# Senkron start/stop (23-09-2026, madde GÖREV 3) — /motor/feed-start ile
# başlatılan bir besleme oturumuna ÖZEL, step motorun bitişini izleyip DC
# motoru otomatik durduran arka plan task'ı. _rule_engine_task ile AYNI
# iptal deseni: yeni bir /motor/feed-start çağrısı öncekini cancel() eder
# (bkz. motor_feed_start()). Bilerek edge-triggered (running: 0->1, sonra
# 1->0 GEÇİŞİ izlenir, düz seviye kontrolü DEĞİL) — admin panelindeki
# bağımsız DC testini etkilemesin diye SADECE bu endpoint'in kendi
# oturumuna özel bir task, DC'nin genel durumuna dayanmıyor.
_sync_watcher_task: "asyncio.Task | None" = None

# 24-09-2026 saha bugı: /motor/feed-start SENKRON bir endpoint (def, async def
# değil) — FastAPI/Starlette onu bir THREAD POOL işçi thread'inde çalıştırır
# (anyio.to_thread.run_sync), o thread'in kendi çalışan bir asyncio event
# loop'u YOKTUR. Eskiden orada doğrudan asyncio.create_task(...) çağrılıyordu,
# bu da "RuntimeError: no running event loop" ile patlıyordu (step+DC zaten
# gönderilmiş oluyordu ama _sync_watcher hiç doğmuyordu — step bitince DC hiç
# durmuyordu). Çözüm: GERÇEK çalışan event loop'un referansını lifespan()
# içinde (o KESİNLİKLE loop thread'inde çalışır) burada saklıyoruz,
# motor_feed_start bunu asyncio.run_coroutine_threadsafe ile kullanır (bkz.
# aşağısı) — farklı bir thread'den loop'a görev iletmenin doğru/güvenli yolu.
_main_event_loop: "asyncio.AbstractEventLoop | None" = None

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
    global _rule_engine_task, _journal_task, _vision_raw_log_task, _main_event_loop
    # /motor/feed-start (senkron endpoint, threadpool'da çalışır) bu ÇALIŞAN
    # loop referansına ihtiyaç duyar (bkz. _main_event_loop tanımı yukarıda) —
    # lifespan() kesinlikle loop'un kendi thread'inde çalıştığı için burada
    # yakalamak güvenli.
    _main_event_loop = asyncio.get_running_loop()
    # Servis açılırken iki kamerayı da açmayı dener (biri takılı değilse
    # diğerini/motor kontrolünü engellemez), kapanırken serbest bırakır —
    # systemd restart'ta "device busy" ile kilitlenmesin diye.
    vision.start()
    psutil.cpu_percent()  # priming cagrisi: ilk cagri referans alir, anlamli deger dondurmez —
    # asagidaki /system/resources'daki interval=None cagrilari bastan itibaren dogru deger versin diye.
    _rule_engine_task = asyncio.create_task(_rule_engine_loop())
    _journal_task = asyncio.create_task(_journal_loop())
    _vision_raw_log_task = asyncio.create_task(_vision_raw_log_loop())
    yield
    _rule_engine_task.cancel()
    _journal_task.cancel()
    _vision_raw_log_task.cancel()
    if _sync_watcher_task is not None:
        _sync_watcher_task.cancel()
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
    kullanıldı; çağıran taraf bunu kullanıcıya/Kontrol Kriterleri'ne "ROI referansı
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
    hem endpoint hem arka plan Kontrol Kriterleri dongusu tarafindan kullanilabilsin diye)."""
    jpg = vision.capture_jpeg(cam_id)
    if jpg is None:
        return None
    return cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)


def _read_all_rois(cam_id: str, frame: np.ndarray) -> tuple[list[dict], bool]:
    """Bir kamera icin kayitli TUM ROI'leri (kalibrasyona gore kaymayi
    telafi ederek) okur. Hem /vision/{cam_id}/read-test endpoint'i hem
    Kontrol Kriterleri dongusu tarafindan kullanilan ORTAK yol — iki yerde ayni
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
#  KONTROL KRİTERLERİ — madde 1 (ROI kriter mantığı) + madde 2 (güvenlik
#  interlock) + madde 7 (aralık dışı alarm), TEK motor olarak (bkz.
#  rule_engine.py). Kriterler periyodik arka plan görevinde
#  (_rule_engine_loop) değerlendirilir; ihlal olunca motor durdurulur +
#  alarm durumu bellekte tutulup UI'a (/rules/status, /ws/status) yansıtılır.
#  (İç endpoint/değişken adları "rules" kaldı — kullanıcıya görünen tüm
#  metin/yorumlarda terim "Kontrol Kriterleri", 15-09-2026 Fatih kararı.)
# ==============================================================================


class RuleDef(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    source: str  # "roi" | "stm"
    cam_id: str | None = None  # source="roi" ise zorunlu
    roi_name: str | None = None  # source="roi" ise zorunlu
    timeout_s: float | None = None  # source="stm" ise zorunlu — bağlantı kaç sn sessiz kalınca ihlal
    min: float | None = None
    max: float | None = None
    stop_motor: bool = False
    enabled: bool = True
    alarm_sound: str | None = None  # ui/alarm_sounds/ icindeki bir dosya adi (ör. "kritik.mp3"),
    # None ise UI'daki Web Audio ton sistemi (fallback) calar (bkz. shared.js).


class RuleListPayload(BaseModel):
    rules: list[RuleDef]


@app.get("/rules")
def get_rules():
    """Kayıtlı TÜM Kontrol Kriterlerini döner (hiç tanımlanmamışsa boş liste)."""
    return {"rules": rules_store.get_rules()}


@app.post("/rules")
def set_rules(payload: RuleListPayload):
    """TÜM Kontrol Kriterleri listesini değiştirir (replace-all, ROI yönetimiyle aynı desen).

    Basit doğrulama: source="roi" için cam_id+roi_name, source="stm" için
    timeout_s zorunlu — eksikse kriter sessizce yanlış çalışmak yerine 400
    ile reddedilir (ör. hangi ROI/alan izleneceği belirsiz bir kriter, motor
    durdurma kararını asla veremeyecek bir kriter demektir, kaydedilmemeli).
    """
    for rule in payload.rules:
        if rule.source == "roi" and not (rule.cam_id and rule.roi_name):
            raise HTTPException(status_code=400, detail=f"Kriter '{rule.name}': source=roi için cam_id+roi_name zorunlu")
        if rule.source == "stm" and rule.timeout_s is None:
            raise HTTPException(status_code=400, detail=f"Kriter '{rule.name}': source=stm için timeout_s zorunlu")
        if rule.source not in ("roi", "stm"):
            raise HTTPException(status_code=400, detail=f"Kriter '{rule.name}': bilinmeyen source '{rule.source}'")
    rules_as_dicts = [r.model_dump() for r in payload.rules]
    rules_store.save_rules(rules_as_dicts)
    return {"success": True, "rules": rules_store.get_rules()}


@app.get("/rules/status")
def get_rules_status():
    """En son Kontrol Kriterleri değerlendirmesinin sonucu — UI'ın alarm
    banner'ı ve "şu kriter şu an okunamıyor" listesi bunu periyodik olarak çeker."""
    return {"violations": _current_violations, "skipped": _current_skipped}


def _collect_roi_readings(cam_ids: set[str]) -> dict[tuple[str, str], dict]:
    """Verilen kameralardan kayıtlı TÜM ROI'leri okuyup, Kontrol
    Kriterleri'nin beklediği {(cam_id, roi_name): {"text":..., "avg_color_hsv":...}}
    formatına çevirir. Kamera açılamazsa o kamera sessizce atlanır (bağlı
    kriter değerlendirilemez -> skipped listesine düşer, motoru durdurmaz —
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
    Kontrol Kriterlerini değerlendirir, ihlal varsa motoru durdurur + alarm
    durumunu günceller. AYRICA (15-09-2026) günlük toplam besleme miktarını
    (feed_totalizer) günceller — STM32 durumunu ZATEN her turda okuyan bu
    döngüye eklendi, ayrı bir döngü açıp kod/örnekleme tekrarı yaratmamak
    için (Fatih talimatı: 'TEK BİR yerde hesapla'). lifespan() içinde
    başlatılır/iptal edilir (bkz. yukarısı)."""
    global _current_violations, _current_skipped
    while True:
        try:
            stm32_status = bridge.get_status()
            feed_totalizer.update(stm32_status)

            rules = rules_store.get_rules()
            if rules:
                cam_ids_needed = {r["cam_id"] for r in rules if r.get("source") == "roi" and r.get("cam_id")}
                roi_readings = _collect_roi_readings(cam_ids_needed) if cam_ids_needed else {}
                stm32_meta = {"is_connected": bridge.is_connected, "status_age_s": bridge.get_status_age()}
                violations, skipped = evaluate_rules(rules, roi_readings, stm32_status, stm32_meta)
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
            logging.getLogger("feedvision.rules").exception("Kontrol Kriterleri dongusunde beklenmeyen hata")
        await asyncio.sleep(RULE_CHECK_INTERVAL_S)


async def _journal_loop():
    """Arka planda sürekli çalışır: JOURNAL_INTERVAL_S'te bir, o an ekrandan
    takip edilen TÜM değerleri (Kontrol Kriterleri'nin baktığı belirli ROI'lerle
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


async def _vision_raw_log_loop():
    """Arka planda sürekli çalışır: vision_raw_log.get_interval_s()'te bir
    (Admin'den ayarlanabilir), SADECE ROI-kaynaklı Kontrol Kriterlerinin
    şu anki HAM okumalarını (parse edilmemiş OCR metni) sabit-genişlikli
    günün .txt dosyasına ekler (bkz. vision_raw_log.py). lifespan()
    içinde başlatılır/iptal edilir."""
    while True:
        interval = vision_raw_log.get_interval_s()
        try:
            roi_rules = [
                r for r in rules_store.get_rules()
                if r.get("source") == "roi" and r.get("enabled", True) and r.get("cam_id") and r.get("roi_name")
            ]
            if roi_rules:
                cam_ids_needed = {r["cam_id"] for r in roi_rules}
                roi_readings = _collect_roi_readings(cam_ids_needed)
                # Sütun adları "{cam_id}/{roi_name}" — ayni roi_name farkli
                # kameralarda kullanilsa bile karismasin diye. Deterministik
                # sira icin alfabetik.
                columns = sorted({f"{r['cam_id']}/{r['roi_name']}" for r in roi_rules})
                values: dict[str, str | None] = {}
                for r in roi_rules:
                    col = f"{r['cam_id']}/{r['roi_name']}"
                    reading = roi_readings.get((r["cam_id"], r["roi_name"]))
                    values[col] = reading.get("text") if reading else None
                vision_raw_log.logger.write_entry(columns, values)
        except Exception:  # noqa: BLE001 — arka plan görevi hicbir hatada tamamen olmemeli
            logging.getLogger("feedvision.vision_raw_log").exception("Goruntu isleme ham veri dongusunde beklenmeyen hata")
        await asyncio.sleep(interval)


# ==============================================================================
#  HAREKET PARAMETRELERİ (23-09-2026) — step/DC motor mm-RPM<->ham komut
#  dönüşümünde kullanılan donanım ölçüleri (bkz. motion_params.py + motion_calc.py).
#  Admin panelinden düzenlenir, aşağıdaki /motor/feed-start GERÇEK komut
#  hesaplamasında kullanır.
# ==============================================================================


class MotionParamsPayload(BaseModel):
    D_drive_mm: float | None = Field(default=None, gt=0)
    D_wheel_dc_mm: float | None = Field(default=None, gt=0)
    D_rod_mm: float | None = Field(default=None, gt=0)
    RPM_MAX_NOLOAD: float | None = Field(default=None, gt=0)


@app.get("/motion-params")
def get_motion_params():
    """Şu an geçerli hareket parametrelerini döner (hiç kaydedilmemişse
    motion_params.DEFAULTS)."""
    return {"params": motion_params.get_params()}


@app.post("/motion-params")
def set_motion_params(payload: MotionParamsPayload):
    """Verilen alanları günceller (kısmi — boş bırakılan alan eski değerinde
    kalır, bkz. motion_params.save_params)."""
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    saved = motion_params.save_params(fields)
    return {"success": True, "params": saved}


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
    # Kozmetik (23-09-2026, GÖREV 3 madde 6): DUR'un davranışı DEĞİŞMİYOR —
    # step'i hâlâ doğrudan/koşulsuz durduruyor, hiçbir watcher/session
    # state'ine BAĞIMLI değil (güvenlik gereği). Sadece varsa aktif bir
    # feed-start watcher'ını burada da cancel() ediyoruz ki DUR'dan sonra
    # watcher'ın kendi fail-safe timeout'u boşuna bekleyip gereksiz bir
    # uyarı log'u düşmesin — cancel() edilmese de DUR'un kendisi zaten
    # motoru durdurmuş olur, bu satır sadece log gürültüsünü önler.
    if _sync_watcher_task is not None and not _sync_watcher_task.done():
        _sync_watcher_task.cancel()
    return bridge.send_command({"cmd": "stop"})


class DcCommand(BaseModel):
    dir: str  # "forward" / "backward" / "stop"
    speed: int = 100  # 0-100 arası PWM hız yüzdesi


@app.post("/motor/dc")
def motor_dc(c: DcCommand):
    speed = max(0, min(100, c.speed))
    return bridge.send_command({"cmd": "dc", "dir": c.dir, "speed": speed})


@app.post("/motor/reset")
def motor_reset():
    return bridge.send_command({"cmd": "reset"})


# ==============================================================================
#  SENKRON BESLEME BAŞLAT (23-09-2026, GÖREV 3) — step (mm/s,mm,mm/s²) + DC
#  (RPM) parametrelerini TEK istekte alır, mm->ham dönüşümü yapar (bkz.
#  motion_calc.py), step'i başlatır, DC'yi başlatır, arka planda step'in
#  bitişini izleyip DC'yi otomatik durduran bir watcher spawn eder.
# ==============================================================================

# Faz 1 (ARM): step komutu gönderildikten sonra firmware'in "running:1"e
# geçtiğini kısa sürede görmemiz beklenir (komut zaten senkron send_command
# ile "ok" aldıktan sonra çağrılıyor) — 1sn makul bir üst sınır. Kısa/hızlı
# bir hareket bu 1sn'lik pencere içinde tamamen başlayıp bitebilir, bu
# durumda running:1 hiç GÖRÜLMEYEBİLİR (100ms'lik poll aralığının arasından
# kaçar) — 24-09-2026 sahada gözlemlendi. Bu yüzden ARM gözlenemezse artık
# watcher PES ETMİYOR (eski davranış: DC'ye hiç dokunmadan return — DC
# sonsuza dek dönmeye devam ediyordu), WAIT fazına her durumda geçiyor.
FEED_SYNC_ARM_TIMEOUT_S = 1.0
FEED_SYNC_POLL_INTERVAL_S = 0.1

# Faz 2 (BEKLE) fail-safe üst sınırı: gerçek bitiş "running:1->0" geçişiyle
# ANINDA yakalanır, bu süre sadece "bağlantı koptu/kart resetlendi" gibi bir
# durumda sonsuza dek beklememek için bir GÜVENLİK AĞI. Teorik süreye (mesafe/
# hız) bolca pay bırakan bir çarpan + sabit ek — rampa/step kaybı gibi
# sebeplerle gerçek süre teorikten biraz uzun sürebilir, bu ağ çok sık
# tetiklenmemeli. Sahada gerekirse ince ayar (main.c STEP_RAMP_START_DELAY_US
# gibi diğer "sahada ayarlanır" sabitlerle aynı ruhta).
FEED_SYNC_WAIT_SAFETY_FACTOR = 3.0
FEED_SYNC_WAIT_SAFETY_MARGIN_S = 5.0
FEED_SYNC_WAIT_MIN_TIMEOUT_S = 10.0

_sync_watcher_logger = logging.getLogger("feedvision.sync_watcher")


class FeedStartCommand(BaseModel):
    dir: int  # step motor yönü — 0 veya 1
    speed_mms: float = Field(gt=0)
    distance_mm: float = Field(gt=0)
    accel_mms2: float = Field(default=0, ge=0)
    dc_dir: str = "forward"  # "forward" / "backward" — "stop" burada anlamsız, ayrıca reddedilir
    rpm: float = Field(gt=0)


async def _sync_watcher(max_wait_s: float):
    """Step motorun running:0->1 (ARM) sonra running:1->0 (BİTİŞ) geçişini
    izler, bitişte DC motoru durdurur. SADECE /motor/feed-start'ın kendi
    oturumuna özel — bridge.get_status() genel/paylaşılan bir okuma olsa da,
    bu task'ın kendisi sadece bu fonksiyon çalışırken var oluyor ve YALNIZCA
    motor_feed_start() tarafından spawn ediliyor; admin panelindeki bağımsız
    "DC İleri/Geri/Dur" butonları bu task'tan habersiz, onu tetiklemez/
    etkilemez. Yeni bir /motor/feed-start çağrısı önceki task'ı cancel()
    eder (bkz. motor_feed_start) — CancelledError burada YUTULMUYOR, DC
    komutu göndermeden sessizce sonlanıyor (yeni çağrı zaten kendi step+dc
    komutlarını gönderiyor, üzerine binmesin diye).

    ÖNEMLİ (24-09-2026 saha düzeltmesi): ARM fazında running:1 hiç
    gözlenemese bile fonksiyon DC'ye dokunmadan return ETMEZ — WAIT fazına
    (fail-safe max_wait_s deadline'ıyla) HER DURUMDA geçilir, bridge'in
    "dc stop" komutu bu fonksiyonun her çalışmasında en geç ARM_TIMEOUT +
    max_wait_s içinde kesin gönderilir. Eskiden ARM kaçırılırsa (kısa/hızlı
    bir hareket 100ms'lik poll aralığının arasından tamamen kaçabiliyor)
    DC sonsuza dek dönmeye devam ediyordu — bu garanti bunu ortadan kaldırır."""
    try:
        armed = False
        deadline = time.monotonic() + FEED_SYNC_ARM_TIMEOUT_S
        while time.monotonic() < deadline:
            if bridge.get_status().get("running") == 1:
                armed = True
                break
            await asyncio.sleep(FEED_SYNC_POLL_INTERVAL_S)
        if not armed:
            _sync_watcher_logger.warning(
                "feed-start: step motor %.1f sn icinde 'running' olarak gozlenmedi "
                "(kisa/hizli bir hareket ARM penceresini kacirmis olabilir), "
                "yine de BEKLE fazina geciliyor",
                FEED_SYNC_ARM_TIMEOUT_S,
            )

        finished = False
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            if bridge.get_status().get("running") == 0:
                finished = True
                break
            await asyncio.sleep(FEED_SYNC_POLL_INTERVAL_S)

        if not finished:
            _sync_watcher_logger.warning(
                "feed-start: step motor %.1f sn icinde bitmedi (baglanti kopmus olabilir), "
                "fail-safe DC durdurma gonderiliyor",
                max_wait_s,
            )

        bridge.send_command({"cmd": "dc", "dir": "stop"})
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — arka plan görevi hicbir hatada tamamen olmemeli
        _sync_watcher_logger.exception("feed-start senkron izleyicisinde beklenmeyen hata")


@app.post("/motor/feed-start")
def motor_feed_start(c: FeedStartCommand):
    """Step (mm/s,mm,mm/s²) + DC (RPM) parametrelerini TEK istekte alıp
    fiziksel birimden ham komuta çevirir (bkz. motion_calc.py), step'i
    başlatır; step "ok" DEĞİLSE DC'ye hiç dokunmadan hata döner (Fatih'in
    açık talebi — DC'nin step'siz/anlamsız dönmeye başlaması istenmiyor).

    Dönüşüm hataları (ör. desteklenen hız/RPM aralığı dışı) HER İKİSİ DE
    donanıma HİÇBİR komut gönderilmeden önce kontrol edilir — spesifikasyon
    sırayı "step gönder, ok ise DC'yi çevir" olarak tarif etse de, RPM
    dönüşümü geçersizse motorun zaten harekete geçmiş olması (sonra da
    hiçbir zaman DC ile senkronlanmayacak yarım bir hareket) istenmeyen bir
    yan etki — bu yüzden ikisi de ÖNCE hesaplanıp doğrulanıyor, donanıma
    hiçbir şey gönderilmeden 400 ile reddedilebiliyor.
    """
    global _sync_watcher_task

    if c.dc_dir not in ("forward", "backward"):
        raise HTTPException(
            status_code=400,
            detail=f"dc_dir 'forward' veya 'backward' olmalı (senkron başlatmada '{c.dc_dir}' anlamsız)",
        )

    params = motion_params.get_params()
    try:
        step_calc = motion_calc.compute_step_command(
            speed_mms=c.speed_mms, distance_mm=c.distance_mm, accel_mms2=c.accel_mms2, d_drive_mm=params["D_drive_mm"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Step dönüşüm hatası: {e}")

    try:
        dc_calc = motion_calc.compute_dc_duty(
            rpm_rod=c.rpm,
            d_wheel_dc_mm=params["D_wheel_dc_mm"],
            d_rod_mm=params["D_rod_mm"],
            rpm_max_noload=params["RPM_MAX_NOLOAD"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"DC RPM dönüşüm hatası: {e}")

    # bridge.send_command seri port seviyesinde beklenmedik bir şeyle
    # (timeout, bağlantı kopması, vb.) karşılaşırsa exception fırlatabilir —
    # bu, motion_calc'ın ValueError'ları gibi kontrollü değil, çıplak 500
    # olarak dışarı sızar ve frontend'in "Reddedildi: ${detail}" gösterimi
    # devreye girmez (24-09-2026 sahada gözlemlendi). Diğer ValueError
    # yakalama deseniyle tutarlı olacak şekilde 502 + açıklamalı detail'e
    # çevriliyor.
    try:
        step_result = bridge.send_command(
            {
                "cmd": "step",
                "dir": c.dir,
                "delay": step_calc["delay_us"],
                "steps": step_calc["steps"],
                "accel": step_calc["accel_steps"],
            }
        )
    except Exception as e:  # noqa: BLE001 — donanım/seri port hatası, HTTPException'a çevriliyor
        raise HTTPException(status_code=502, detail=f"STM32 ile haberleşme hatası: {e}")

    step_reply = step_result.get("reply") or {}
    step_ok = bool(step_result.get("sent")) and not step_result.get("timed_out") and "err" not in step_reply
    if not step_ok:
        return {
            "success": False,
            "stage": "step",
            "step_calc": step_calc,
            "step_result": step_result,
        }

    try:
        dc_result = bridge.send_command({"cmd": "dc", "dir": c.dc_dir, "speed": dc_calc["duty"]})
    except Exception as e:  # noqa: BLE001 — donanım/seri port hatası, HTTPException'a çevriliyor
        raise HTTPException(status_code=502, detail=f"STM32 ile haberleşme hatası: {e}")

    # Yarış durumu (GÖREV 3 madde 5): yeni bir feed-start önceki watcher'ı
    # cancel() eder — _rule_engine_task.cancel() ile AYNI desen. cancel()/
    # done() hem asyncio.Task hem concurrent.futures.Future'da aynı isimle
    # var — aşağıdaki run_coroutine_threadsafe'e geçişten ETKİLENMEZ.
    if _sync_watcher_task is not None and not _sync_watcher_task.done():
        _sync_watcher_task.cancel()

    # Savunma amaçlı (teorik olarak imkansız — lifespan startup her zaman
    # ilk istekten önce tamamlanır): _main_event_loop henüz set edilmemişse
    # run_coroutine_threadsafe'e None loop veremeyiz; adım/DC komutu zaten
    # gönderildi ama gözetimsiz kalmasın diye en azından çıplak 500 yerine
    # açıklamalı bir 503 dönüyoruz.
    if _main_event_loop is None:
        raise HTTPException(
            status_code=503,
            detail="Servis henüz hazır değil (event loop başlatılmadı) — besleme başlatıldı ama senkron izleme kurulamadı, birazdan tekrar deneyin",
        )

    estimated_duration_s = c.distance_mm / c.speed_mms
    max_wait_s = max(
        FEED_SYNC_WAIT_MIN_TIMEOUT_S,
        estimated_duration_s * FEED_SYNC_WAIT_SAFETY_FACTOR + FEED_SYNC_WAIT_SAFETY_MARGIN_S,
    )
    # asyncio.create_task DEĞİL: bu SENKRON endpoint (def, async def değil)
    # FastAPI'nin thread pool'unda çalışıyor, o thread'in kendi çalışan bir
    # event loop'u YOK — create_task orada "RuntimeError: no running event
    # loop" fırlatırdı (24-09-2026 sahada gerçek bug, bkz. main.py başındaki
    # _main_event_loop yorumu). run_coroutine_threadsafe farklı bir thread'den
    # asıl loop'a güvenli şekilde görev iletmenin doğru API'si; bir
    # concurrent.futures.Future döner (asyncio.Task değil) ama .cancel()/
    # .done() aynı isimle çalışır, yukarıdaki/aşağıdaki (motor_stop, lifespan
    # shutdown) kullanımlar değişmeden çalışmaya devam eder.
    _sync_watcher_task = asyncio.run_coroutine_threadsafe(_sync_watcher(max_wait_s), _main_event_loop)

    return {
        "success": True,
        "step_calc": step_calc,
        "dc_calc": dc_calc,
        "step_result": step_result,
        "dc_result": dc_result,
    }


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
                    # Bridge'in kendi tanılama bilgisi (bozuk satır sayacı,
                    # otomatik yeniden bağlanma durumu) — 22-09-2026 eklendi,
                    # mevcut alanları değiştirmiyor, sadece ekliyor (eski UI
                    # bu alanı okumasa da bozulmaz).
                    "diagnostics": bridge.get_diagnostics(),
                    # Kontrol Kriterleri'nin en son değerlendirmesi — UI polling'e
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
#  GÜNLÜK TOPLAM BESLEME MİKTARI (Operasyonel Kayıt sayfası, 15-09-2026) —
#  bkz. feed_totalizer.py. _rule_engine_loop içinde her turda güncellenir,
#  burada SADECE en son özeti okuyup döner (hesaplama tek yerde).
# ==============================================================================


@app.get("/feed-total/today")
def feed_total_today():
    """Bugünün toplam besleme miktarı özeti (mm) — headline rakam
    total_mm_average, ayrıca e1/e2 ayrı ayrı + uyuşmazlık uyarısı."""
    return feed_totalizer.get_summary()


# ==============================================================================
#  GÖRÜNTÜ İŞLEME HAM VERİ KAYDI (15-09-2026) — bkz. vision_raw_log.py +
#  yukarıdaki _vision_raw_log_loop. Admin'den periyot ayarlanabilir.
# ==============================================================================


class VisionRawLogConfigPayload(BaseModel):
    interval_s: float = Field(gt=0, le=3600)


@app.get("/vision-raw-log/config")
def get_vision_raw_log_config():
    """Şu an ayarlı yazım periyodunu (saniye) döner."""
    return {"interval_s": vision_raw_log.get_interval_s()}


@app.post("/vision-raw-log/config")
def set_vision_raw_log_config(payload: VisionRawLogConfigPayload):
    """Yazım periyodunu değiştirir — bir sonraki döngü turundan itibaren
    geçerli olur (o an uyuyan görev kesintiye uğratılmaz, en fazla bir
    önceki periyot kadar gecikmeli devreye girer)."""
    vision_raw_log.set_interval_s(payload.interval_s)
    return {"success": True, "interval_s": payload.interval_s}


# ==============================================================================
#  SESLİ ALARM — MP3 ALTYAPISI (15-09-2026) — bkz. alarm_sounds.py. Fiziksel
#  USB hoparlör donanımı BUGÜN YOK, bu yüzden gerçek ses çıkışı sahada test
#  edilemiyor — ama dosya seçimi + tarayıcı üzerinden çalma test edilebilir.
# ==============================================================================


@app.get("/alarm-sounds")
def get_alarm_sounds():
    """ui/alarm_sounds/ klasöründeki mevcut MP3 dosyalarını listeler (Admin'de
    Kontrol Kriteri başına 'Alarm Sesi' dropdown'ını doldurmak için)."""
    return {"sounds": alarm_sounds.list_alarm_sounds()}


@app.get("/alarm-sounds/{filename}")
def get_alarm_sound_file(filename: str):
    """Tek bir MP3 dosyasını servis eder (tarayıcı <audio>/Audio() ile çalar).

    Güvenlik: filename'de yol ayıracı (path traversal) varsa ya da klasör
    dışına çıkıyorsa 404 döner (bkz. alarm_sounds.resolve_sound_path)."""
    path = alarm_sounds.resolve_sound_path(filename)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Ses dosyası bulunamadı: {filename}")
    return FileResponse(path, media_type="audio/mpeg")


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
    """Admin ekranı — tam erişim (ROI/kalibrasyon, Kontrol Kriterleri tanımlama, ham
    motor komutları, sistem logları/journal, hareket analizi). Şifre YOK
    (15-09-2026 kararı — bkz. landing() docstring'i)."""
    return _read_ui_file("admin.html")


@app.get("/operational-log", response_class=HTMLResponse)
def operational_log_page():
    """Operasyonel Kayıt sayfası (15-09-2026) — günlük toplam besleme miktarı
    (öncelikli rakam) + günün operasyonel journal kayıtları (ikincil, ham liste)."""
    return _read_ui_file("operational_log.html")


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
