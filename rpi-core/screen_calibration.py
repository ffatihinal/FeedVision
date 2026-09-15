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
# sessiz kalıyor. (Sadece FALLBACK yolunda kullanılıyor, bkz. asağıda.)
CANNY_LOW = 50
CANNY_HIGH = 150

# Gerçek hedef ekranın (Amazemet Plasma Atomization HMI) fotoğrafında dış
# çerçeve (bezel) kalın SİYAH plastik, içerik (menü/renk) ise değişken —
# "en parlak/en büyük iç bölge = ekran" varsayımı bu yüzden kırılgan. Bunun
# yerine PRİMER yöntem: gri seviyesi bu eşiğin ALTINDAKİ (near-black) piksel
# kümesinin dış konturu — bezel içeriği sarmaladığı için, içerik ne renk/
# parlaklıkta olursa olsun dış kontur her zaman bezelin GERÇEK fiziksel
# sınırını verir (içerik maskeye dahil olsa da olmasa da dış sınır değişmez).
# 40/255 başlangıç değeri — saha fotoğrafları/gerçek kamerayla kalibre
# edilecek (bkz. Açık Kalanlar), şimdilik makul bir "neredeyse siyah" eşiği.
DARK_BEZEL_THRESHOLD = 40

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


def _largest_quad_in_contours(
    contours: list[np.ndarray], min_area: float, max_area: float
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Verilen kontur listesinde alan sınırları içindeki EN BÜYÜK konturu VE
    (varsa) EN BÜYÜK temiz-4-köşeli-dışbükey konturu ayrı ayrı bulup döner.

    Döner: (best_quad_or_None, best_contour_or_None) — best_contour, best_quad
    bulunamazsa minAreaRect fallback'i için kullanılır (bkz. çağıran).
    """

    def in_bounds(area: float) -> bool:
        return min_area <= area <= max_area

    best_quad: np.ndarray | None = None
    best_quad_area = 0.0
    best_contour: np.ndarray | None = None
    best_contour_area = 0.0

    for contour in contours:
        area = cv2.contourArea(contour)
        if not in_bounds(area):
            continue
        if area > best_contour_area:
            best_contour = contour
            best_contour_area = area
        if area <= best_quad_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, APPROX_POLY_EPSILON_RATIO * perimeter, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            best_quad = approx.reshape(4, 2).astype(np.float32)
            best_quad_area = area

    return best_quad, best_contour


def _corners_from_mask(mask: np.ndarray, min_area: float, max_area: float) -> np.ndarray | None:
    """Bir ikili maskede (255=aday bölge) en büyük geçerli konturdan 4 köşe çıkarır.

    Önce temiz 4-köşeli dışbükey kontur denenir (en isabetli); bulunamazsa
    (ör. yansıma konturu kısmen kırmışsa) en büyük konturun convex hull'unun
    minAreaRect'i ile kaba bir dörtgen tahmini üretilir — RETR_EXTERNAL
    kullanıldığı için (sadece dış sınır) bu her koşulda maskenin dış sınırını
    temsil eder, iç boşluklar/delikler etkilemez.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    best_quad, best_contour = _largest_quad_in_contours(contours, min_area, max_area)
    if best_quad is not None:
        return order_points(best_quad)
    if best_contour is None:
        return None

    hull = cv2.convexHull(best_contour)
    rect = cv2.minAreaRect(hull)
    box = cv2.boxPoints(rect).astype(np.float32)
    return order_points(box)


def detect_screen_corners(frame: np.ndarray) -> np.ndarray | None:
    """Karede izlenen ekranın DIŞ SİYAH BEZEL (çerçeve) köşelerini bulmaya çalışır.

    Neden bezel (ekranın aktif içeriği değil): gerçek hedef HMI (Amazemet
    Plasma Atomization) fotoğrafında ekran içeriği (menü/parlaklık/standby'a
    göre) değişken, ama etrafındaki kalın siyah fiziksel çerçeve her koşulda
    sabit ve yüksek kontrastlı. "En parlak/en büyük iç bölge = ekran" varsayımı
    bu yüzden kırılgan.

    PRİMER yöntem — karanlık maske: gri seviyesi DARK_BEZEL_THRESHOLD altındaki
    piksellerden ikili maske çıkarılır (morfolojik CLOSE ile yansımanın açtığı
    küçük kopukluklar köprülenir), maskenin DIŞ sınırı bezelin fiziksel
    köşelerini verir — içerik ne renk/parlaklıkta olursa olsun dış sınır
    değişmez (içerik maskeye dahil olsa da bezelin İÇİNDE kaldığı için dış
    kontur etkilenmez).

    YEDEK yöntem — Canny kenar tespiti: karanlık maske hiçbir aday bulamazsa
    (ör. eşik gerçek ışıkla uyuşmuyorsa) genel kenar tabanlı yaklaşıma düşülür.

    Döner: (4, 2) float32 sıralı köşe dizisi [sol-üst, sağ-üst, sağ-alt,
    sol-alt] ya da hiçbir aday bulunamazsa None (ör. bezel kamera görüşünde
    hiç yok, kare tamamen karanlık/aydınlık — "sessizce yanlış okumak yerine
    açıkça bulunamadı" davranışı burada başlıyor; çağıran taraf bu durumda
    son bilinen referansı kullanıp "referans belirsiz" uyarısı düşürecek).
    """
    if frame is None or frame.size == 0:
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    frame_area = gray.shape[0] * gray.shape[1]
    min_area = frame_area * MIN_SCREEN_AREA_RATIO
    max_area = frame_area * MAX_SCREEN_AREA_RATIO
    close_kernel = np.ones((5, 5), np.uint8)

    # --- 1) Primer: karanlık bezel maskesi ---
    _, dark_mask = cv2.threshold(gray, DARK_BEZEL_THRESHOLD, 255, cv2.THRESH_BINARY_INV)
    dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_CLOSE, close_kernel, iterations=2)
    corners = _corners_from_mask(dark_mask, min_area, max_area)
    if corners is not None:
        return corners

    # --- 2) Yedek: Canny kenar tespiti ---
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, CANNY_LOW, CANNY_HIGH)
    edges = cv2.dilate(edges, close_kernel, iterations=2)
    return _corners_from_mask(edges, min_area, max_area)


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
