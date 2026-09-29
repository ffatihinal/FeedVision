"""
FeedVision — UI Screen Camera ekran okuma (screen reading) modulu

Ne yapar: UI Screen Camera'nin gordugu bir karede, sabit bir ROI (region of
interest) tanimlar, o bolgeyi kirpar; ortalama rengini HSV olarak hesaplar
ve Tesseract (pytesseract) ile ROI icindeki metni/sayiyi okumaya calisir.

Neden ayri dosya: main.py/vision.py/serial_bridge.py'ye (motor, seri,
mevcut kamera akisi) dokunmadan, tamamen izole gelistirilebilsin diye.
main.py bu modulu sadece yeni /vision/{cam_id}/read-test endpoint'inde
(Adim 3) kullanacak.

ONEMLI — bugunku ROI koordinatlari GECICI/TEST amaclidir: gercek UA HMI
ekraninin fotografi/olculeri henuz elde degil (saha ziyareti yarin).
Bu yuzden DEFAULT_ROI, vision.py'deki STREAM_SIZE (1280x720) karesinin
ortasinda, herhangi bir telefon ekranini kaplayacak kadar buyuk keyfi bir
dikdortgen. Saha fotograflari gelince gercek HMI ekraninin piksel
koordinatlariyla degistirilecek (bkz. Azobex_WP1 saha notlari).
"""

from dataclasses import dataclass

import cv2
import numpy as np

try:
    import pytesseract

    PYTESSERACT_AVAILABLE = True
except ImportError:
    # pytesseract kurulu olsa da Tesseract binary'si (apt/brew ile ayrica
    # kurulan sistem paketi) eksik olabilir — modul yine de import edilebilsin
    # diye burada yutuyoruz, gercek cagri read_text_ocr() icinde denenir.
    pytesseract = None
    PYTESSERACT_AVAILABLE = False

# (x, y, genislik, yukseklik) piksel cinsinden — 1280x720 karede merkezi
# kaplayan gecici test ROI'si. Gercek koordinatlar yarin (saha fotograflari
# sonrasi) config'e tasinacak sekilde guncellenecek.
DEFAULT_ROI: tuple[int, int, int, int] = (340, 160, 600, 400)

# Grup 2 (READY/WORKING/SCAN OK/ERROR) kareleri icin: ROI'nin ortalama HSV
# V (parlaklik) kanali bu esigin ustundeyse "dolu/1", altindaysa "bos/0"
# kabul edilir. Icin bos kare koyu/dusuk parlaklik, ici mavi dolu kare
# belirgin sekilde daha parlak/doygun olur. GECICI SABIT — gercek HMI
# fotograflari elde olunca (saha ziyareti sonrasi) kalibre edilecek; simdilik
# 0-255 araliginin ortasinin biraz ustu makul bir baslangic noktasi.
BOOLEAN_BRIGHTNESS_THRESHOLD: float = 130.0


@dataclass
class ScreenReadResult:
    """Bir ROI okumasinin sonucu — kirpilan bolgenin OCR metni + ortalama rengi.

    ocr_error: OCR gercekten calisip bos metin bulmasi (normal/beklenen —
    goruntude yazi olmayabilir) ile OCR'in hic calisamamasi (pytesseract
    kurulu degil / Tesseract binary'si eksik / cagri exception firlatti)
    arasindaki farki tasir. None ise OCR sorunsuz calisti demektir (text
    bos da olabilir, dolu da).

    kind/bool_state: ROI "boolean" tipindeyse (Grup 2 — READY/WORKING/
    SCAN OK/ERROR kareleri) OCR hic calistirilmaz, bunun yerine bool_state
    0/1 olarak doldurulur; "numeric" ROI'lerde (varsayilan) bool_state None
    kalir, text/ocr_error eskisi gibi doldurulur.
    """

    roi: tuple[int, int, int, int]
    avg_color_hsv: tuple[float, float, float]
    avg_color_rgb: tuple[float, float, float]
    text: str
    ocr_error: str | None = None
    kind: str = "numeric"
    bool_state: int | None = None


def crop_roi(frame: np.ndarray, roi: tuple[int, int, int, int] = DEFAULT_ROI) -> np.ndarray:
    """Bir goruntu karesinden ROI dikdortgenini kirpar.

    Kare ROI'den kucukse (ör. dusuk cozunurluklu bir test goruntusu) ROI
    karenin sinirlarina gore kirpilir — cv2 tasma hatasi yerine sessizce
    kucuk bir goruntu doner.
    """
    x, y, w, h = roi
    frame_h, frame_w = frame.shape[:2]
    x0 = max(0, min(x, frame_w))
    y0 = max(0, min(y, frame_h))
    x1 = max(0, min(x + w, frame_w))
    y1 = max(0, min(y + h, frame_h))
    return frame[y0:y1, x0:x1]


def crop_roi_quad(
    frame: np.ndarray,
    quad_corners: np.ndarray,
    output_size: tuple[int, int] | None = None,
) -> np.ndarray:
    """Eksene hizali olmayan (egik/dondurulmus) bir dortgeni duz, dikdortgen
    bir goruntuye "utuleyerek" kirpar — crop_roi'nin (duz dikdortgen) aksine
    perspektif duzeltme uygular (cv2.getPerspectiveTransform + warpPerspective).

    quad_corners: (4,2) [sol-ust, sag-ust, sag-alt, sol-alt] sirali koseler
    (bkz. screen_calibration.warp_roi_quad / order_points ile ayni sira).

    output_size verilmezse dortgenin kenar uzunluklarinin ortalamasindan
    makul bir cikti boyutu hesaplanir (ust/alt kenar ortalamasi = genislik,
    sol/sag kenar ortalamasi = yukseklik) — boylece kalibrasyon anindaki ROI
    boyutuna yakin bir olcek korunur.

    Yan fayda: egik metin OCR'i zorlar, duzlestirilmis goruntu Tesseract
    icin daha temiz bir girdi olur — ama asil amac geometrik dogruluk
    (bounding box'in gercek dortgeni temsil etmemesi sorununu cozmek).
    """
    quad = np.asarray(quad_corners, dtype=np.float32).reshape(4, 2)
    tl, tr, br, bl = quad

    if output_size is None:
        width_top = float(np.linalg.norm(tr - tl))
        width_bottom = float(np.linalg.norm(br - bl))
        height_left = float(np.linalg.norm(bl - tl))
        height_right = float(np.linalg.norm(br - tr))
        out_w = max(1, round((width_top + width_bottom) / 2))
        out_h = max(1, round((height_left + height_right) / 2))
    else:
        out_w, out_h = output_size
        out_w = max(1, int(out_w))
        out_h = max(1, int(out_h))

    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(frame, matrix, (out_w, out_h))


def average_color_hsv(image: np.ndarray) -> tuple[float, float, float]:
    """Bir goruntunun ortalama rengini HSV (H:0-179, S:0-255, V:0-255) olarak doner.

    Neden HSV: HMI ekranlarindaki durum renkleri (yesil/kirmizi/sari alarm
    vb.) parlaklik/golge degisiminden RGB'ye gore daha az etkilenir; ton (H)
    kanali renk kimligini daha stabil tasir.
    """
    if image.size == 0:
        return (0.0, 0.0, 0.0)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mean = cv2.mean(hsv)  # (H, S, V, alpha) doner, alpha kanalimiz yok
    return (float(mean[0]), float(mean[1]), float(mean[2]))


def average_color_rgb(image: np.ndarray) -> tuple[float, float, float]:
    """Bir goruntunun ortalama rengini (R, G, B) 0-255 araliginda doner — insan icin okunakli.

    Dogrudan ham BGR crop uzerinden cv2.mean ile hesaplanir (kanal sirasi RGB'ye
    cevrilir); HSV sonucundan geri donusturulmez — ortalamanin ortalamasi
    (HSV->RGB) matematiksel olarak farkli/hatali bir sonuc verir, bu yuzden
    bagimsiz olarak ayrica hesaplaniyor.
    """
    if image.size == 0:
        return (0.0, 0.0, 0.0)
    mean = cv2.mean(image)  # BGR sirasinda (B, G, R, alpha) doner
    return (float(mean[2]), float(mean[1]), float(mean[0]))


def read_text_ocr(image: np.ndarray) -> tuple[str, str | None]:
    """Kirpilan ROI goruntusunu Tesseract'tan gecirip (metin, hata) tuple'i doner.

    Uc durum ayirt edilir:
    - OCR calisti, metin bulunamadi -> ("", None) — normal/beklenen, hata degil.
    - pytesseract (Python paketi) kurulu degil -> ("", "acik sebep mesaji").
    - Tesseract binary'si (sistem paketi) bulunamadi / cagri patladi ->
      ("", gercek exception mesaji).
    Bos goruntude (boyut 0) Tesseract'a hic girmeden ("", None) donulur —
    bu OCR'in basarisizligi degil, zaten okunacak goruntu yok demektir.
    """
    if image.size == 0:
        return "", None
    if not PYTESSERACT_AVAILABLE:
        return "", "pytesseract Python paketi kurulu degil (pip install pytesseract)"
    # Tesseract renkli goruntude de calisir ama gri tonlama + upsample
    # kucuk/HMI fontlarinda dogruluk icin genelde daha iyi sonuc verir.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    try:
        text = pytesseract.image_to_string(upscaled)
    except Exception as exc:  # noqa: BLE001 — Tesseract binary eksik/izin hatasi vb. onceden bilinmiyor
        return "", f"Tesseract calistirilamadi: {exc}"
    return text.strip(), None


def read_boolean_state(
    image: np.ndarray, threshold: float = BOOLEAN_BRIGHTNESS_THRESHOLD
) -> tuple[int, float]:
    """Grup 2 (READY/WORKING/SCAN OK/ERROR) kareleri icin OCR YERINE karar verir.

    Kirpilan ROI'nin ortalama HSV V (parlaklik) kanalini hesaplar; esigin
    ustundeyse 1 (dolu/mavi), altindaysa 0 (bos) doner. Boyutu 0 olan
    (kare disina tasmis) goruntude 0/0.0 doner — "bos" ile ayni sonuc,
    zaten okunacak piksel yok.

    Doner: (state 0|1, avg_brightness) — avg_brightness debug/kalibrasyon
    icin ham deger olarak da UI'a tasinir.
    """
    if image.size == 0:
        return 0, 0.0
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    avg_brightness = float(cv2.mean(hsv)[2])
    state = 1 if avg_brightness >= threshold else 0
    return state, avg_brightness


def read_roi(
    frame: np.ndarray,
    roi: tuple[int, int, int, int] = DEFAULT_ROI,
    kind: str = "numeric",
    quad: np.ndarray | None = None,
) -> ScreenReadResult:
    """ROI'yi kirpar; "numeric" ise OCR+renk, "boolean" ise SADECE renk/parlaklik
    esigiyle 0/1 karari hesaplar — bu modulun tek giris noktasi.

    kind="boolean" durumunda OCR hic calistirilmaz (Tesseract kucuk dolu/bos
    kareler icin anlamsiz/gereksiz CPU yuku) — bunun yerine bool_state doldurulur.

    quad verilirse (kalibrasyon sonrasi gercek/olasi egik dortgen koseleri,
    bkz. screen_calibration.warp_roi_quad) crop_roi_quad ile perspektif-
    duzeltilmis kirpma yapilir; quad None ise (kalibrasyon yok/geriye uyumluluk)
    eskisi gibi crop_roi (duz dikdortgen, roi bbox) kullanilir.
    """
    cropped = crop_roi_quad(frame, quad) if quad is not None else crop_roi(frame, roi)
    hsv_color = average_color_hsv(cropped)
    rgb_color = average_color_rgb(cropped)
    if kind == "boolean":
        state, _avg_brightness = read_boolean_state(cropped)
        return ScreenReadResult(
            roi=roi,
            avg_color_hsv=hsv_color,
            avg_color_rgb=rgb_color,
            text="",
            ocr_error=None,
            kind="boolean",
            bool_state=state,
        )
    text, ocr_error = read_text_ocr(cropped)
    return ScreenReadResult(
        roi=roi,
        avg_color_hsv=hsv_color,
        avg_color_rgb=rgb_color,
        text=text,
        ocr_error=ocr_error,
        kind="numeric",
        bool_state=None,
    )
