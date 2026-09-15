"""
FeedVision — ROI drift düzeltme: ekran köşe tespiti (calibration) modülü

Ne yapar: AP2 kamerasının gördüğü karede, izlenen ekranın (UA cihazı HMI'ı)
4 köşesini tespit eder. Bu köşeler "referans" olarak saklanınca (bkz.
calibration_store.py), sonraki karelerde köşeler tekrar bulunup referansla
karşılaştırılır — kamera titrer/kayarsa aradaki fark perspektif dönüşümle
hesaplanıp ROI'ler buna göre kaydırılır (bkz. main.py entegrasyonu, sonraki
adım). Bu dosya SADECE saf görüntü işleme fonksiyonlarını içerir — dosya/ağ
G/Ç'si yok, bu yüzden donanımsız (Mac'te, sentetik görüntüyle) test edilebilir.

Neden fiziksel marker değil: ekranın kendi dikdörtgen çerçevesi zaten sabit
bir referans — ek donanım/etiket gerektirmez, "ekranın 4 köşesi" konturla
bulunabilecek kadar belirgin bir şekil.
"""

import cv2
import numpy as np

# Ekranın kapladığı alan, kare alanının bu oranından küçükse aday reddedilir
# — gürültü/küçük dikdörtgenleri (ör. ekrandaki bir ikon) "ekran" sanmayalım.
MIN_SCREEN_AREA_RATIO = 0.05

# Kontur, kare alanının bu oranından büyükse de reddedilir — tüm kareyi
# kaplayan bir kontur genelde "ekran" değil, gürültü/aydınlatma artefaktıdır
# (ör. tamamen düz/tekdüze bir bölgede kenar algılama yanlış pozitif üretebilir).
MAX_SCREEN_AREA_RATIO = 0.95

# Canny kenar eşikleri: düşük/yüksek histerezis sınırları. Atölye ışığı
# değişken olabileceğinden gri-seviye mutlak eşik (adaptiveThreshold) yerine
# GRADYAN tabanlı Canny tercih edildi — düz/tekdüze bölgelerde (ör. ekranın
# arkasındaki boş duvar) adaptiveThreshold yanlış pozitif üretebiliyordu
# (sentetik test sırasında görüldü), Canny gerçek kenar olmayan yerde
# sessiz kalıyor.
CANNY_LOW = 50
CANNY_HIGH = 150

# approxPolyDP toleransı: kontur çevresinin bu oranı kadar sapmaya izin verir
# — küçük tutulursa gerçek dörtgen bile 5-6 köşeli algılanıp reddedilebilir,
# büyük tutulursa dörtgen olmayan şekiller de 4 köşeye "yuvarlanabilir".
APPROX_POLY_EPSILON_RATIO = 0.02


def order_points(pts: np.ndarray) -> np.ndarray:
    """4 noktayı (rastgele sırada) sabit sıraya dizer: sol-üst, sağ-üst, sağ-alt, sol-alt.

    Neden gerekli: cv2.findContours/approxPolyDP köşeleri hep aynı sırada
    döndürmez (kontur yönüne göre değişir) — perspektif dönüşümün doğru
    çalışması için kalibrasyon anındaki ve şimdiki köşelerin AYNI köşeye
    karşılık geldiğinden emin olmamız gerekir (ör. ikisi de "sol-üst" ile
    başlamalı), yoksa dönüşüm görüntüyü yanlış eşler.

    Yöntem: toplamı (x+y) en küçük olan sol-üst, en büyük olan sağ-alt;
    farkı (x-y) en küçük olan sol-alt, en büyük olan sağ-üst — standart
    "4 nokta perspektif dönüşüm" tekniği.
    """
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)

    s = pts.sum(axis=1)
    ordered[0] = pts[np.argmin(s)]  # sol-üst
    ordered[2] = pts[np.argmax(s)]  # sağ-alt

    diff = np.diff(pts, axis=1).reshape(-1)
    ordered[1] = pts[np.argmin(diff)]  # sağ-üst
    ordered[3] = pts[np.argmax(diff)]  # sol-alt

    return ordered


def detect_screen_corners(frame: np.ndarray) -> np.ndarray | None:
    """Karede izlenen ekranın 4 köşesini bulmaya çalışır.

    Adımlar: gri tonlama -> bulanıklaştırma -> Canny kenar tespiti -> dilate
    (kopuk kenar parçalarını birleştir) -> kontur tespiti -> en büyük alanlı,
    4 köşeye yaklaştırılabilen (approxPolyDP) dörtgen kontur seçilir.

    Döner: (4, 2) float32 sıralı köşe dizisi [sol-üst, sağ-üst, sağ-alt,
    sol-alt] ya da uygun bir dörtgen bulunamazsa None (ör. ekran kapalı,
    çok karanlık, kamera tamamen kapalı bir şeye bakıyor — "sessizce yanlış
    okumak yerine açıkça bulunamadı" davranışı burada başlıyor).
    """
    if frame is None or frame.size == 0:
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, CANNY_LOW, CANNY_HIGH)
    # Ekranın çerçeve kenarı bazen tek bir kesintisiz kontur olarak çıkmaz
    # (parlama/gölge kenarı böler) — hafif dilate ile kopuk parçalar birleşir.
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    frame_area = frame.shape[0] * frame.shape[1]
    min_area = frame_area * MIN_SCREEN_AREA_RATIO
    max_area = frame_area * MAX_SCREEN_AREA_RATIO

    best_quad: np.ndarray | None = None
    best_area = 0.0

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area or area <= best_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, APPROX_POLY_EPSILON_RATIO * perimeter, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            best_quad = approx.reshape(4, 2).astype(np.float32)
            best_area = area

    if best_quad is None:
        return None

    return order_points(best_quad)


def compute_warp_matrix(reference_corners: np.ndarray, current_corners: np.ndarray) -> np.ndarray:
    """Kalibrasyon anındaki köşelerden şimdiki köşelere perspektif dönüşüm matrisini hesaplar.

    Bu matrisle kalibrasyon anında çizilmiş bir ROI'nin köşe noktaları,
    kameranın şu anki (kaymış/titremiş olabilecek) görüş açısına taşınabilir.
    """
    ref = np.asarray(reference_corners, dtype=np.float32).reshape(4, 2)
    cur = np.asarray(current_corners, dtype=np.float32).reshape(4, 2)
    return cv2.getPerspectiveTransform(ref, cur)


def warp_roi_rect(roi: tuple[int, int, int, int], matrix: np.ndarray) -> tuple[int, int, int, int]:
    """Bir (x, y, w, h) dikdörtgenin 4 köşesini verilen matrisle dönüştürüp,
    sonucun eksen-hizalı (axis-aligned) sınırlayıcı kutusunu (bounding box)
    yeni (x, y, w, h) olarak döner.

    Neden bounding box: crop_roi (screen_reader.py) düz dikdörtgen kırpma
    yapıyor, döndürülmüş/eğik bir dörtgeni doğrudan kıramaz — küçük kayma
    açılarında (kamera titremesi ölçeğinde) bounding box yeterli hassasiyeti
    verir; büyük açısal kaymalar zaten şartname kapsamı dışı (kamera montajı
    sabit, sadece küçük titreşim/kayma telafi ediliyor).
    """
    x, y, w, h = roi
    corners = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    warped = cv2.perspectiveTransform(corners, matrix).reshape(4, 2)
    x0 = float(np.min(warped[:, 0]))
    y0 = float(np.min(warped[:, 1]))
    x1 = float(np.max(warped[:, 0]))
    y1 = float(np.max(warped[:, 1]))
    return (round(x0), round(y0), round(x1 - x0), round(y1 - y0))
