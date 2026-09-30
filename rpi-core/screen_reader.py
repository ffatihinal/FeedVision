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

import os
import threading
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

try:
    import pytesseract

    PYTESSERACT_AVAILABLE = True
except ImportError:
    # pytesseract kurulu olsa da Tesseract binary'si (apt/brew ile ayrica
    # kurulan sistem paketi) eksik olabilir — modul yine de import edilebilsin
    # diye burada yutuyoruz, gercek cagri read_text_ocr() icinde denenir.
    pytesseract = None
    PYTESSERACT_AVAILABLE = False

try:
    import tesserocr

    TESSEROCR_AVAILABLE = True
except ImportError:
    # tesserocr, pytesseract'in aksine Tesseract motorunu HER OCR cagrisinda
    # ayri bir subprocess olarak baslatmaz -- motoru BIR KERE acip surekli
    # acik tutan Cython tabanli bir baglayicidir (subprocess YOK, dogrudan
    # C++ API cagrisi, bkz. _get_tesserocr_api). Ama native derleme gerektirir:
    # sistem paketleri (libtesseract-dev + libleptonica-dev, bkz.
    # requirements.txt) onceden kurulu olmali. Bu genelde Mac gelistirme
    # ortaminda yok -> import basarisiz olur; PYTESSERACT_AVAILABLE ile AYNI
    # desenle burada sessizce yutuluyor, read_text_ocr() otomatik olarak
    # pytesseract yoluna duser (regresyon yok).
    tesserocr = None
    TESSEROCR_AVAILABLE = False

# (x, y, genislik, yukseklik) piksel cinsinden — 1280x720 karede merkezi
# kaplayan gecici test ROI'si. Gercek koordinatlar yarin (saha fotograflari
# sonrasi) config'e tasinacak sekilde guncellenecek.
DEFAULT_ROI: tuple[int, int, int, int] = (340, 160, 600, 400)

# --- Grup 2 (READY/WORKING/SCAN OK/ERROR) renk-ozel tespiti (30-09-2026 saha
# bulgusu duzeltmesi) ------------------------------------------------------
# Sorun: eski kod SADECE ortalama parlakliga (V kanali, sabit esik 130.0)
# bakiyordu — HMI ekranindaki dolu kareler TURKUAZ/CYAN renge burunuyor, ama
# "bu bolge yeterince parlak mi" sorusu "bu bolge turkuaz mi" sorusuyla ayni
# sey degil. Sahada gercek dolu bir kare bile bazen "bos (0)" okunuyordu,
# cunku kameranin gercek pozlama/isik kosulunda turkuazin parlakligi bazen
# 130 esiginin ALTINDA kaliyor; tersine, parlak ama turkuaz OLMAYAN (beyaz
# yansima, parlak gri metin) bir alan yanlislikla "dolu (1)" donebiliyordu.
#
# Cozum: parlakliga degil HSV Ton (Hue) araligina + doygunluk (Saturation)
# alt sinirina bakiyoruz. cv2.inRange ile turkuaz aralik+doygunluk kriterini
# KARSILAYAN piksel oranini (match_ratio) hesaplayip bir esikle karsilastiriyoruz
# — dusuk doygunluklu (gri/beyaz, S dusuk) parlak alanlar boylece ELENIYOR,
# cunku onlarin Hue degeri anlamsiz/rastgele olsa da S kriterini gecemiyorlar.
#
# HSV Hue degerleri (OpenCV 0-179 olcegi, ampirik olcum — bkz. gelistirme
# notlari): saf cyan H=90, web "turquoise" H=87, "medium/dark turquoise"
# H=89-90. (80, 105) araligi bu degerlerin etrafinda kamera renk sapmasina
# (beyaz dengesi, LED spektrumu farki) makul bir tolerans birakiyor, ama yesil
# (H=60) ve saf mavi (H=120) gibi komsu renkleri DISARIDA birakiyor.
BOOLEAN_HUE_RANGE: tuple[int, int] = (80, 105)

# Doygunluk (S, 0-255) alt siniri — beyaz/gri/siyah gibi renksiz (dusuk S)
# alanlarin, parlakligi ne olursa olsun turkuaz sayilmasini engeller (eski
# koddaki yanlis-pozitif riskinin kok nedeni buydu). Turkuaz renkler S=167-255
# araliginda olculdu (yukaridaki not); 80 makul, gevsek bir alt sinir.
BOOLEAN_SAT_MIN: float = 80.0

# Parlaklik (V) alt siniri — cok karanlik/neredeyse siyah piksellerde Hue
# degeri sayisal olarak tanimsiz/gurultulu olabilir (dusuk V'de renk bilgisi
# guvenilmez); bu tabanin altindaki pikseller Hue/Saturation kriterini
# karsilasa bile turkuaz sayilmaz.
BOOLEAN_VAL_MIN: float = 40.0

# ROI icindeki piksellerin en az bu orani (0.0-1.0) yukaridaki turkuaz
# kriterini (Hue araligi + S/V alt sinirlari) karsilarsa "dolu/1" kabul
# edilir, azsa "bos/0". GECICI/makul baslangic degeri (~%28) — gercek HMI
# fotograflari/saha testleriyle kalibre edilecek. Kismi/gurultulu turkuaz
# (ör. kare kismen dolu, kismen arka plan karisik) bu esigin etrafinda
# ara bir match_ratio uretir; debug/kalibrasyon icin ScreenReadResult.match_ratio
# olarak da UI'a tasinir (bkz. asagidaki ScreenReadResult).
BOOLEAN_MATCH_RATIO_THRESHOLD: float = 0.28


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

    match_ratio: SADECE kind="boolean" icin doldurulur — ROI icindeki
    piksellerin turkuaz/cyan renk kriterini (Hue+Saturation, bkz.
    read_boolean_state) karsilama orani (0.0-1.0). bool_state'in HANGI
    esikle 0/1'e yuvarlandigini gorunur kilmak icin debug/kalibrasyon
    amacli tasinir (duration_ms'in UI'da gosterilmesiyle ayni desen);
    "numeric" ROI'lerde None kalir.
    """

    roi: tuple[int, int, int, int]
    avg_color_hsv: tuple[float, float, float]
    avg_color_rgb: tuple[float, float, float]
    text: str
    ocr_error: str | None = None
    kind: str = "numeric"
    bool_state: int | None = None
    match_ratio: float | None = None


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


# --- Tesseract PSM secimi (30-09-2026 saha bulgusu) -------------------------
# Sorun: kamera/ROI SABITKEN bile "Oksijen: 19" gibi degerler 5-6 okumadan
# sadece 1'inde dogru okunuyordu ("metin bulunamadi" kalani). Kok neden: eski
# kod pytesseract.image_to_string()'i config'siz cagiriyordu -> Tesseract
# varsayilan PSM 3'u (tam otomatik SAYFA analizi) kullaniyordu; bu mod kucuk,
# tek-satir/tek-sayi kirpilmis goruntulerde (bizim ROI'lerimiz tam bu) sayfa
# duzeni/blok tespiti yaparken tutarsiz calisiyor.
#
# Ampirik test (sentetik ROI benzeri goruntuler: "19", "-1", "-12.5", "100",
# "0", "1.05e+03"; 12 varyasyon/deger, gurultu+pozisyon jitter'i ile, bkz.
# gelistirme notlari): PSM 7 ("tek satir metin varsay") %73.6 dogruluk +
# %0 "bos donme" oraniyla PSM 3 varsayilanini (%52.8, %27.8 bos) VE PSM 8'i
# ("tek kelime varsay", %8.3 dogruluk — noktali/eksili kisa sayilari "kelime"
# olarak segmentlemeye calisirken sistematik basarisiz oluyor) acik farkla
# gecti. Bu yuzden PSM 7 sabit/varsayilan secildi.
_TESSERACT_PSM = "--psm 7"

# Pi 5 (4 cekirdek) sahada: surekli OCR donguye girildiginde (her ROI okumasi
# ayri bir Tesseract subprocess'i baslatiyor) Chamber Camera'nin FPS'i dustu
# (30-09-2026 saha gozlemi) -- kamera decode + web sunucusu ile Tesseract
# subprocess'leri CPU cekirdeklerini rastgele paylasiyordu. Cozum: Tesseract
# cagrisi SIRASINDA ana sureci (bu Python process) gecici olarak {2,3}
# cekirdeklerine sabitleyip kamera+web sunucusunun kullandigi 0-1'i serbest
# birakiyoruz.
#
# Neden Tesseract subprocess'ini DOGRUDAN degil ANA SURECI pinliyoruz:
# pytesseract.image_to_string() icerde subprocess.Popen kullanarak Tesseract'i
# baslatiyor ama Popen nesnesine kutuphane API'si uzerinden erisimimiz yok.
# POSIX'te DOGAN bir alt-surec, dogdugu anda EBEVEYNININ (bizim ana surecimiz)
# CPU affinity kumesini MIRAS ALIR -- yani ana sureci cagri oncesi {2,3}'e
# sabitleyip cagri BITER BITMEZ eski haline dondurmek, doğan Tesseract
# subprocess'inin de {2,3}'te calismasini SAGLAR (ekstra izin/uid gerekmez).
_OCR_AFFINITY_CORES = frozenset({2, 3})


def _pin_current_process_to_ocr_cores() -> frozenset[int] | None:
    """Mevcut sureci OCR icin ayrilan cekirdeklere gecici sabitler.

    Geri yukleme icin ONCEKI affinity kumesini doner; hicbir sey
    degistirmediyse (asagidaki durumlardan biri) None doner:
    - Linux disi platform (ör. Mac gelistirme ortami) -> os.sched_setaffinity
      hic yok, hasattr kontroluyle sessizce no-op.
    - Hedef cekirdekler ({2,3}) bu surec icin zaten kullanilabilir degil
      (ör. Pi 5 disinda 4'ten az cekirdekli/cgroup ile kisitlanmis bir cihaz)
      -> sabitlemenin anlami yok, mevcut kumeyi degistirmeden birak.
    - sched_setaffinity beklenmedik sekilde OSError firlatirsa (ör. izin
      sorunu) -> OCR'i engellemesin diye sessizce vazgecilir.
    """
    if not hasattr(os, "sched_setaffinity"):
        return None
    try:
        previous = os.sched_getaffinity(0)
        target = _OCR_AFFINITY_CORES & previous
        if not target:
            return None
        os.sched_setaffinity(0, target)
        return frozenset(previous)
    except OSError:
        return None


def _restore_process_affinity(previous: frozenset[int] | None) -> None:
    """_pin_current_process_to_ocr_cores() ile sabitlenmis affinity'yi geri yukler.

    previous None ise (sabitleme hic uygulanmadiysa) hicbir sey yapmaz.
    """
    if previous is None:
        return
    try:
        os.sched_setaffinity(0, previous)
    except OSError:
        pass


def _build_ocr_config(char_whitelist: str | None) -> str:
    """Tesseract'a gecirilecek config string'ini kurar: sabit PSM 7 + opsiyonel
    karakter whitelist'i. Saf/yan-etkisiz — Tesseract cagrilmadan test edilebilir.
    Sadece pytesseract yolunda kullanilir (tesserocr ayni PSM/whitelist
    kararlarini kendi API'si -- SetPageSegMode/SetVariable -- uzerinden alir,
    bkz. _get_tesserocr_api / _read_text_via_tesserocr).
    """
    config = _TESSERACT_PSM
    if char_whitelist:
        config += f" -c tessedit_char_whitelist={char_whitelist}"
    return config


# tesserocr.PyTessBaseAPI ornegi -- lazy-init (ilk gercek OCR cagrisinda
# olusturulur, IMPORT ANINDA degil). Neden lazy: Tesseract veri yolu
# bulunamamasi gibi bir kurulum sorunu boylece modul import'unu KIRMAZ,
# screen_reader yine de import edilebilir kalir; hata sadece gercekten OCR
# cagrildiginda (read_text_ocr icinde, ayni try/except ile) ortaya cikar.
# Motor BIR KERE acilip surekli acik tutuldugu icin (subprocess YOK) sonraki
# her cagrida ayni ornek tekrar kullanilir.
_tesserocr_api = None

# _tesserocr_api paylasilan, thread-safe OLMAYAN bir C++ nesnesi (PyTessBaseAPI)
# sarmalar; main.py'de en az uc ayri thread bunu ayni anda cagirabilir:
# vision_read_test (FastAPI threadpool worker), _rule_engine_loop (asyncio
# event-loop thread, her 2sn) ve _journal_loop (aynisi, her 10sn). Kilitsiz
# durumda iki cagri ic ice girerse (SetVariable/SetImage/GetUTF8Text) bir
# ROI'nin whitelist'i baska ROI'nin goruntusuyle karisabilir -- bu da
# rule_engine'in motor durdurma kararina yanlis veri karistirir.
# calibration_store.py'deki modul-seviyesi _lock deseniyle ayni: tek kilit,
# hem lazy-init hem her cagri bu kilit altinda.
_tesserocr_lock = threading.Lock()


def _get_tesserocr_api():
    """tesserocr.PyTessBaseAPI ornegini ilk cagrida olusturup modul-seviyesinde
    saklar, sonraki cagrilarda ayni orneği doner. PSM 7 burada BIR KERE
    ayarlanir (_build_ocr_config'teki PSM 7 karariyla AYNI, farkli API
    uzerinden: SetPageSegMode).

    Cagiran taraf (_read_text_via_tesserocr) _tesserocr_lock'u zaten tutuyor
    olmali -- bu fonksiyon kendi basina kilitlenmez (ayni thread'in ayni
    kilidi iki kez almasi RLock olmadan deadlock olurdu)."""
    global _tesserocr_api
    if _tesserocr_api is None:
        api = tesserocr.PyTessBaseAPI()
        api.SetPageSegMode(tesserocr.PSM.SINGLE_LINE)
        _tesserocr_api = api
    return _tesserocr_api


def _read_text_via_tesserocr(image: np.ndarray, char_whitelist: str | None) -> str:
    """tesserocr (motor surekli acik, subprocess YOK) uzerinden OCR calistirir.

    whitelist ROI'den ROI'ye degisebildigi (bkz. read_text_ocr docstring'i)
    icin PSM'in aksine HER cagrida yeniden ayarlanir -- bos string Tesseract
    icin "kisitlama yok" anlamina gelir, bu yuzden char_whitelist None/bos
    oldugunda onceki bir cagridan kalma whitelist'i de temizler.

    TUMU _tesserocr_lock altinda: lazy-init + SetVariable + SetImage +
    GetUTF8Text tek bir atomik blok olmali -- yoksa iki thread'in cagrilari
    ic ice girip (interleave) bir ROI'nin whitelist/goruntusu digerininkiyle
    karisabilir (bkz. _tesserocr_lock tanimindaki not)."""
    with _tesserocr_lock:
        api = _get_tesserocr_api()
        api.SetVariable("tessedit_char_whitelist", char_whitelist or "")
        api.SetImage(Image.fromarray(image))
        text = api.GetUTF8Text()
    return text or ""


def read_text_ocr(image: np.ndarray, char_whitelist: str | None = None) -> tuple[str, str | None]:
    """Kirpilan ROI goruntusunu Tesseract'tan gecirip (metin, hata) tuple'i doner.

    Iki OCR yolu vardır — TESSEROCR_AVAILABLE ise tesserocr (motor surekli
    acik, subprocess YOK, ~130ms/cagri sabit maliyeti YOK) kullanilir; degilse
    (kurulu degil/Mac'te derlenemedi) mevcut pytesseract yoluna (subprocess
    bazli, HER ortamda calisan) duşülür — davranis/donus degerleri asagidaki
    uc durum icin HER IKI yolda da AYNIDIR:
    - OCR calisti, metin bulunamadi -> ("", None) — normal/beklenen, hata degil.
    - ne tesserocr ne pytesseract kurulu -> ("", "acik sebep mesaji").
    - Tesseract calisamadi / cagri patladi -> ("", gercek exception mesaji).
    Bos goruntude (boyut 0) Tesseract'a hic girmeden ("", None) donulur —
    bu OCR'in basarisizligi degil, zaten okunacak goruntu yok demektir.

    char_whitelist: verilirse SADECE bu karakterlere izin verilir (ör.
    "0123456789.-" sayisal ROI'ler icin, bkz. main.py RoiDef.ocr_whitelist).
    Opsiyonel/ROI-bazinda tutuluyor, GLOBAL/sabit yapilmadi — bazi ROI'ler
    gelecekte harf de icerebilir (ör. durum metni). DIKKAT (ampirik gozlem):
    cok kisa (tek karakter) girdilerde whitelist bazen OCR'in TAMAMEN bos
    ("") donmesine yol aciyor (LSTM motorunun bilinen bir kisitlamasi — kisa
    girdide whitelist kisitlamasi ic guven skorunu sifira dusurebiliyor) —
    bu yuzden whitelist'i tek karakterlik degil 2+ karakterlik sayisal ROI'ler
    icin kullanmak daha tutarli sonuc verir.
    """
    if image.size == 0:
        return "", None
    if not TESSEROCR_AVAILABLE and not PYTESSERACT_AVAILABLE:
        return "", "pytesseract Python paketi kurulu degil (pip install pytesseract)"
    # Tesseract renkli goruntude de calisir ama gri tonlama + upsample
    # kucuk/HMI fontlarinda dogruluk icin genelde daha iyi sonuc verir.
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    upscaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    previous_affinity = _pin_current_process_to_ocr_cores()
    try:
        if TESSEROCR_AVAILABLE:
            text = _read_text_via_tesserocr(upscaled, char_whitelist)
        else:
            config = _build_ocr_config(char_whitelist)
            text = pytesseract.image_to_string(upscaled, config=config)
    except Exception as exc:  # noqa: BLE001 — Tesseract binary/motor eksik/izin hatasi vb. onceden bilinmiyor
        return "", f"Tesseract calistirilamadi: {exc}"
    finally:
        # finally: hem basarili donuste hem exception'da (yukaridaki return
        # dahil) affinity mutlaka eski haline donsun, OCR sonrasi kamera/web
        # sunucusu dongusu eskisi gibi tum cekirdekleri kullanabilsin.
        _restore_process_affinity(previous_affinity)
    return text.strip(), None


def read_boolean_state(
    image: np.ndarray,
    hue_range: tuple[int, int] = BOOLEAN_HUE_RANGE,
    sat_min: float = BOOLEAN_SAT_MIN,
    val_min: float = BOOLEAN_VAL_MIN,
    match_ratio_threshold: float = BOOLEAN_MATCH_RATIO_THRESHOLD,
) -> tuple[int, float]:
    """Grup 2 (READY/WORKING/SCAN OK/ERROR) kareleri icin OCR YERINE karar verir.

    ESKI davranis (30-09-2026'ya kadar) SADECE ortalama HSV V (parlaklik)
    kanalina bakiyordu — bu, saha gozlemiyle YANLIS bulundu: dolu kareler
    turkuaz/cyan renge burunuyor, "parlak mi" sorusu "turkuaz mi" sorusuyla
    ayni sey degil (bkz. BOOLEAN_HUE_RANGE tanimindaki not). YENI davranis:
    ROI'nin HSV Hue (ton) + Saturation (doygunluk) + Value (parlaklik) alt
    sinirlarini AYNI ANDA karsilayan piksellerin oranini (match_ratio) hesaplar
    (cv2.inRange + cv2.countNonZero) ve bu oran match_ratio_threshold'u gecerse
    1 (dolu/turkuaz), gecmezse 0 (bos) doner.

    Neden Hue+Saturation birlikte: Hue tek basina yeterli degil — dusuk
    doygunluklu (gri/beyaz/siyah) bir piksel de HSV donusumunde "tesadufen"
    turkuaz Hue araligina dusebilir (renk bilgisi neredeyse yok, Hue gurultulu);
    sat_min bu yanlis-pozitifi eler. val_min ise ayni nedenle cok karanlik
    pikselleri (Hue/S guvenilmez) eler.

    Boyutu 0 olan (kare disina tasmis) goruntude 0/0.0 doner — "bos" ile ayni
    sonuc, zaten okunacak piksel yok.

    hue_range/sat_min/val_min/match_ratio_threshold opsiyonel parametreler —
    varsayilanlar turkuaz icin kalibre edildi, ama ileride farkli renkte bir
    gosterge (ör. kirmizi alarm karesi) cikarsa kod DEGISMEDEN, cagiran taraf
    farkli degerler gecerek ayarlayabilir (bkz. modul-seviyesi BOOLEAN_*
    sabitlerindeki notlar).

    Doner: (state 0|1, match_ratio) — match_ratio debug/kalibrasyon icin ham
    deger olarak da UI'a tasinir (bkz. ScreenReadResult.match_ratio).
    """
    if image.size == 0:
        return 0, 0.0
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue_min, hue_max = hue_range
    lower = np.array([hue_min, sat_min, val_min], dtype=np.uint8)
    upper = np.array([hue_max, 255, 255], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    match_ratio = float(cv2.countNonZero(mask)) / float(mask.size)
    state = 1 if match_ratio >= match_ratio_threshold else 0
    return state, match_ratio


def read_roi(
    frame: np.ndarray,
    roi: tuple[int, int, int, int] = DEFAULT_ROI,
    kind: str = "numeric",
    quad: np.ndarray | None = None,
    ocr_whitelist: str | None = None,
) -> ScreenReadResult:
    """ROI'yi kirpar; "numeric" ise OCR+renk, "boolean" ise SADECE renk (Hue/
    Saturation) kriteriyle 0/1 karari hesaplar — bu modulun tek giris noktasi.

    kind="boolean" durumunda OCR hic calistirilmaz (Tesseract kucuk dolu/bos
    kareler icin anlamsiz/gereksiz CPU yuku) — bunun yerine bool_state doldurulur.

    quad verilirse (kalibrasyon sonrasi gercek/olasi egik dortgen koseleri,
    bkz. screen_calibration.warp_roi_quad) crop_roi_quad ile perspektif-
    duzeltilmis kirpma yapilir; quad None ise (kalibrasyon yok/geriye uyumluluk)
    eskisi gibi crop_roi (duz dikdortgen, roi bbox) kullanilir.

    ocr_whitelist: sadece kind="numeric" icin anlamli, read_text_ocr'a oldugu
    gibi iletilir (bkz. o fonksiyonun docstring'i — opsiyonel/ROI-bazinda).
    """
    cropped = crop_roi_quad(frame, quad) if quad is not None else crop_roi(frame, roi)
    hsv_color = average_color_hsv(cropped)
    rgb_color = average_color_rgb(cropped)
    if kind == "boolean":
        state, match_ratio = read_boolean_state(cropped)
        return ScreenReadResult(
            roi=roi,
            avg_color_hsv=hsv_color,
            avg_color_rgb=rgb_color,
            text="",
            ocr_error=None,
            kind="boolean",
            bool_state=state,
            match_ratio=match_ratio,
        )
    text, ocr_error = read_text_ocr(cropped, char_whitelist=ocr_whitelist)
    return ScreenReadResult(
        roi=roi,
        avg_color_hsv=hsv_color,
        avg_color_rgb=rgb_color,
        text=text,
        ocr_error=ocr_error,
        kind="numeric",
        bool_state=None,
        match_ratio=None,
    )
