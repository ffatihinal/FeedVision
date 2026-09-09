"""
FeedVision — AP2 ekran okuma (screen reading) modulu

Ne yapar: AP2 kamerasinin gordugu bir karede, sabit bir ROI (region of
interest) tanimlar, o bolgeyi kirpar; ortalama rengini HSV olarak hesaplar
ve Tesseract (pytesseract) ile ROI icindeki metni/sayiyi okumaya calisir.

Neden ayri dosya: main.py/vision.py/serial_bridge.py'ye (motor, seri,
mevcut kamera akisi) dokunmadan, tamamen izole gelistirilebilsin diye.
main.py bu modulu sadece yeni /vision/{cam_id}/read-test endpoint'inde
(Adim 3) kullanacak.

ONEMLI — bugunku ROI koordinatlari GECICI/TEST amaclidir: gercek AP2 HMI
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


@dataclass
class ScreenReadResult:
    """Bir ROI okumasinin sonucu — kirpilan bolgenin OCR metni + ortalama rengi."""

    roi: tuple[int, int, int, int]
    avg_color_hsv: tuple[float, float, float]
    text: str


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


def read_text_ocr(image: np.ndarray) -> str:
    """Kirpilan ROI goruntusunu Tesseract'tan gecirip metni doner.

    pytesseract kurulu degilse veya Tesseract binary'si (sistem paketi)
    bulunamazsa bos string doner — cagiran taraf (endpoint) bunu "OCR
    calismadi" olarak yorumlayabilir, servis cokmez. Bos goruntude de
    (boyut 0) Tesseract'a hic girmeden bos string donulur.
    """
    if image.size == 0:
        return ""
    if not PYTESSERACT_AVAILABLE:
        return ""
    # Tesseract renkli goruntude de calisir ama gri tonlama + upsample
    # kucuk/HMI fontlarinda dogruluk icin genelde daha iyi sonuc verir.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    try:
        text = pytesseract.image_to_string(upscaled)
    except Exception:  # noqa: BLE001 — Tesseract binary eksik/izin hatasi vb. onceden bilinmiyor
        return ""
    return text.strip()


def read_roi(frame: np.ndarray, roi: tuple[int, int, int, int] = DEFAULT_ROI) -> ScreenReadResult:
    """ROI'yi kirpar, ortalama HSV rengini ve OCR metnini hesaplar — bu modulun tek giris noktasi."""
    cropped = crop_roi(frame, roi)
    hsv_color = average_color_hsv(cropped)
    text = read_text_ocr(cropped)
    return ScreenReadResult(roi=roi, avg_color_hsv=hsv_color, text=text)
