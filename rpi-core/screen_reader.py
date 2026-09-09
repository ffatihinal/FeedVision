"""
FeedVision — AP2 ekran okuma (screen reading) modulu

Ne yapar: AP2 kamerasinin gordugu bir karede, sabit bir ROI (region of
interest) tanimlar, o bolgeyi kirpar ve ortalama rengini HSV olarak
hesaplar. Bu akscamin ilk iskeleti — bu adimda henuz OCR yok (bkz. Adim 2),
sadece ROI kirpma + renk analizi.

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

# (x, y, genislik, yukseklik) piksel cinsinden — 1280x720 karede merkezi
# kaplayan gecici test ROI'si. Gercek koordinatlar yarin (saha fotograflari
# sonrasi) config'e tasinacak sekilde guncellenecek.
DEFAULT_ROI: tuple[int, int, int, int] = (340, 160, 600, 400)


@dataclass
class ScreenReadResult:
    """Bir ROI okumasinin sonucu — OCR eklenene kadar sadece renk tasir."""

    roi: tuple[int, int, int, int]
    avg_color_hsv: tuple[float, float, float]


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


def read_roi(frame: np.ndarray, roi: tuple[int, int, int, int] = DEFAULT_ROI) -> ScreenReadResult:
    """ROI'yi kirpar ve ortalama HSV rengini hesaplar — Adim 1'in tek giris noktasi."""
    cropped = crop_roi(frame, roi)
    hsv_color = average_color_hsv(cropped)
    return ScreenReadResult(roi=roi, avg_color_hsv=hsv_color)
