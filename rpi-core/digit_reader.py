"""
FeedVision — HMI sabit-fontlu rakam okuma: karakter segmentasyonu + şablon eşleştirme

Ne yapar: screen_reader.read_text_ocr (Tesseract) yolunun SİLİNMEDEN yanına
eklenen ALTERNATİF bir rakam okuma yolu (2026-09-30, saha kararı — bkz.
main.py RoiDef.reader). HMI paneli gerçek/düzgün bir font kullanıyor
(7-segment DEĞİL, önceki saha incelemesi bunu doğruladı) — bu yüzden "özel
rakam tanıma" burada template-matching (her karakter şeklinin şablonunu
tutup karşılaştırma) mantığıyla yapılabiliyor, 7-segment'e özel karmaşık
mantık gerekmiyor.

İddia (ChatGPT + Gemini, ikisi de bağımsız önerdi): Tesseract'a göre 20-30 kat
daha hızlı — Tesseract her ROI okumasında ayrı bir subprocess başlatıyor
(bkz. screen_reader.read_text_ocr, PSM/affinity notları), bu modül SADECE
Python/OpenCV içinde çalışıyor, subprocess yok. Bu iddia
tests/test_digit_reader.py::TestPerformanceComparison içinde GERÇEKTEN
ölçülüyor — körlemesine güvenilmiyor.

Neden ayrı dosya: screen_reader.py'nin (Tesseract) sorumluluğuna dokunmadan,
main.py'nin RoiDef.reader alanına göre ("tesseract" varsayılan / "template"
opsiyonel) hangi yolun çağrılacağını seçebilmesi için (bkz. main.py
_read_all_rois).

Güven skoru sözleşmesi (kritik): match_character/read_digits belirsiz bir
eşleşmede SESSİZCE yanlış tahmin ETMEZ — güven eşiğinin (CONFIDENCE_THRESHOLD)
altında kalan karakter "?" ile işaretlenir, genel güven skoru da (en düşük
karakter güveni) düşük kalır. Bozuk/gürültülü bir görüntüde yüksek güvenle
yanlış rakam döndürmek, HMI değerini yanlış okuyup motoru yanlış kontrol
etmekten daha tehlikelidir — bu yüzden "emin değilsem söyle" tasarım kararı.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Bir konturun "karakter" sayılması için minimum bounding-box alanı (piksel
# kare) — kamera/JPEG gürültüsünden gelen tek-piksel speckle'ları eler.
# Ampirik/muhafazakâr başlangıç değeri (bkz. tests/test_digit_reader.py
# gürültülü girdi testleri) — saha kalibrasyonunda gerekirse ayarlanır.
MIN_CONTOUR_AREA = 10

# Her karakter görüntüsü (segment edilen VE şablon) karşılaştırma öncesi bu
# sabit boyuta normalize edilir (cv2.resize) — matchTemplate/MSE karşılaştırması
# ancak aynı boyuttaki görüntüler arasında anlamlı olur. (genişlik, yükseklik).
TEMPLATE_SIZE: tuple[int, int] = (20, 32)

# match_character/read_digits güven skoru [0,1] aralığında (1 = piksel piksel
# aynı ikili görüntü). Bu eşiğin ALTINDA kalan eşleşme "?" ile işaretlenir —
# CONFIDENCE_THRESHOLD sözleşmesi için yukarıdaki modül docstring'ine bakın.
CONFIDENCE_THRESHOLD = 0.55

# Desteklenen karakter etiketleri — HMI sayısal göstergelerinde beklenen
# tüm karakterler (rakam + ondalık nokta + eksi işareti).
CHARACTER_LABELS: tuple[str, ...] = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ".", "-")

# "Belirsiz eşleşme" durumunda dönen özel etiket — sessizce yanlış tahmin
# yerine main.py/operatör bu değeri görüp okumaya GÜVENMEMESİ gerektiğini anlar.
UNKNOWN_LABEL = "?"


def _odd_block_size(shape: tuple[int, int]) -> int:
    """cv2.adaptiveThreshold icin gecerli (tek sayi, >=3) blockSize hesaplar
    -- goruntunun kisa kenarinin yaklasik yarisi, kucuk kirpintilarda da
    (ör. tek karakterlik ROI) makul bir komsuluk buyuklugu versin diye."""
    shortest_side = min(shape[:2])
    block_size = max(3, (shortest_side // 2) | 1)
    if block_size % 2 == 0:
        block_size += 1
    return block_size


def segment_characters(image: np.ndarray) -> list[np.ndarray]:
    """ROI kirpintisini siyah-beyaza cevirip ayri karakterleri (rakam/nokta/
    eksi) soldan-saga sirali kucuk goruntuler olarak doner.

    Adimlar: gri tonlama -> hafif Gaussian blur (kamera gurultusunu
    yumusatir) -> kenar piksellerinin ortalamasina gore polarite tespiti
    (arka plan HANGI ROI'de acik/koyu olacagi onceden bilinmiyor -- hem
    "koyu zeminde parlak yazi" hem "acik zeminde koyu yazi" HMI'lerde
    goruluyor, bu yuzden kenar/border piksel ortalamasi arka plani temsil
    eder varsayimiyla gerekirse gri goruntu ters cevrilir) -> adaptif esikleme
    (cv2.adaptiveThreshold) -> kucuk gurultu benekelerini temizleyen morfolojik
    acma (opening) -> dis konturlar (cv2.findContours) -> alani cok kucuk
    olanlar elenir -> soldan saga sirali kirpilmis ikili goruntuler.

    Bos goruntude ([]) hicbir islem yapmadan bos liste doner.
    """
    if image.size == 0:
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)

    border_mask = np.zeros_like(blurred, dtype=bool)
    border_mask[0, :] = True
    border_mask[-1, :] = True
    border_mask[:, 0] = True
    border_mask[:, -1] = True
    border_mean = float(blurred[border_mask].mean()) if border_mask.any() else 0.0
    if border_mean > 127:
        # Kenarlar (arka plan) parlak -> "koyu yazi/acik zemin" -- karakterler
        # her zaman PARLAK olsun diye gri goruntuyu ters cevir (asagidaki esikleme
        # tek bir polarite varsayimiyla calisiyor: parlak = on plan/karakter).
        blurred = 255 - blurred

    block_size = _odd_block_size(blurred.shape)
    thresh = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, -5
    )
    # Kucuk gurultu benekelerini (tek piksel/birkac piksellik yanlis pozitifler)
    # temizler -- gercek karakter govdelerini bozacak kadar buyuk bir kernel degil.
    opening_kernel = np.ones((2, 2), np.uint8)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, opening_kernel)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h < MIN_CONTOUR_AREA:
            continue
        boxes.append((x, y, w, h))
    boxes.sort(key=lambda box: box[0])  # soldan saga
    return [thresh[y : y + h, x : x + w] for (x, y, w, h) in boxes]


def _normalize_char_image(image: np.ndarray, size: tuple[int, int] = TEMPLATE_SIZE) -> np.ndarray:
    """Bir karakter goruntusunu (segment_characters ciktisi ya da ham sablon)
    sabit boyuta (size) normalize edip ikili (0/255) hale getirir --
    karsilastirma icin hem sablonlarin hem segment edilen karakterlerin AYNI
    boyut/format'ta olmasi sart.

    ONEMLI (30-09-2026 duzeltmesi): en/boy oranini KORUYARAK olceklendirir
    (letterbox -- sigdirip ortalar, kalan alani siyahla doldurur), DOGRUDAN
    size'a GERMEZ. Gerekce: "." (nokta) ve "-" (eksi) ikisi de kucuk/basit
    konturlar -- sıkı bounding-box'a kirpildiktan sonra dogrudan sabit boyuta
    GERILIRSE (aspect ratio'ya bakilmadan) ikisi de "tuvali dolduran bir
    blok" haline gelip AYIRT EDILEMEZ oluyordu (ampirik bulgu: "7.3" gibi
    girdilerde nokta eksi ile karistiriliyordu, bkz. tests/test_digit_reader.py
    -- letterbox'tan ONCEKI halde bu test basarisizdi). En/boy orani
    korunursa nokta kucuk/kare, eksi ince/genis KALIR -- ayirt edici sekil
    bilgisi kaybolmaz.

    Bos goruntude (boyut 0) tamami sifir (siyah) sabit boyutlu bir goruntu
    doner -- cagiran taraf (match_character) bunu "hicbir sey yok" gibi
    dogal olarak dusuk benzerlik skoruyla eslestirir, ozel durum kontrolu gerekmez.
    """
    target_w, target_h = size
    if image.size == 0:
        return np.zeros((target_h, target_w), dtype=np.uint8)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    src_h, src_w = gray.shape[:2]
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    resized = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((target_h, target_w), dtype=np.uint8)
    x_off = (target_w - new_w) // 2
    y_off = (target_h - new_h) // 2
    canvas[y_off : y_off + new_h, x_off : x_off + new_w] = resized
    _, binary = cv2.threshold(canvas, 127, 255, cv2.THRESH_BINARY)
    return binary


def _similarity_score(a: np.ndarray, b: np.ndarray) -> float:
    """Iki ikili (0/255) goruntu arasinda basit piksel-MSE tabanli benzerlik
    skoru -- 1.0 piksel piksel ayni demek, 0.0 tamamen ters (siyah<->beyaz
    her piksel farkli). cv2.matchTemplate yerine bu tercih edildi cunku
    karsilastirilan goruntuler ZATEN ayni boyuta normalize edilmis (TEMPLATE_SIZE)
    -- kaydirma/olcek aramasi gerekmiyor, dogrudan piksel karsilastirmasi
    hem daha hizli hem daha yorumlanabilir (bkz. modul docstring'i, "basit
    piksel MSE farki" gorev tanimi).
    """
    a_norm = a.astype(np.float32) / 255.0
    b_norm = b.astype(np.float32) / 255.0
    mse = float(np.mean((a_norm - b_norm) ** 2))
    return 1.0 - mse


@dataclass
class DigitTemplateSet:
    """0-9, '.', '-' icin etiketli sablon goruntulerini bellekte tutar.

    Sablonlar ZATEN sabit boyuta normalize edilmis (_normalize_char_image)
    olarak saklanir -- match_character her karsilastirmada yeniden
    normalize etmesin diye (sablon sayisi az, ROI okuma sik oldugu icin
    bu on-hesaplama performans farkini onemli olcude etkiler, bkz. iddia
    edilen 20-30x hiz kazanci).
    """

    templates: dict[str, np.ndarray] = field(default_factory=dict)

    def add(self, label: str, image: np.ndarray) -> None:
        """Bir karakter icin sablon ekler/degistirir (ayni etiketle tekrar
        cagrilirsa eskisinin uzerine yazar -- kalibrasyon sirasinda operator
        yanlislikla ayni rakami iki kez yakalarsa sorun cikarmaz)."""
        self.templates[label] = _normalize_char_image(image)

    def labels(self) -> list[str]:
        """Su an sablonu kayitli etiketlerin listesini doner (sira onemsiz)."""
        return list(self.templates.keys())

    def __len__(self) -> int:
        return len(self.templates)


def match_character(char_image: np.ndarray, templates: DigitTemplateSet) -> tuple[str, float]:
    """Bir karakter goruntusunu, sablon setindeki HER etiketle karsilastirip
    en iyi eslesen karakteri + guven skorunu doner.

    Guven skoru CONFIDENCE_THRESHOLD'un altindaysa ("belirsiz eslesme")
    SESSIZCE yanlis tahmin ETMEZ -- UNKNOWN_LABEL ("?") + o dusuk skoru doner
    (bkz. modul docstring'i, "Guven skoru sozlesmesi").

    Sablon seti bossa (hic sablon yakalanmamis/generate_default_templates
    hic cagrilmamis) da ayni sekilde ("?", 0.0) doner -- karsilastirilacak
    hicbir sey yoksa emin olunamaz.
    """
    if len(templates) == 0:
        return UNKNOWN_LABEL, 0.0
    normalized = _normalize_char_image(char_image)
    best_label = UNKNOWN_LABEL
    best_score = -1.0
    for label, template_image in templates.templates.items():
        score = _similarity_score(normalized, template_image)
        if score > best_score:
            best_score = score
            best_label = label
    if best_score < CONFIDENCE_THRESHOLD:
        return UNKNOWN_LABEL, best_score
    return best_label, best_score


def read_digits(image: np.ndarray, templates: DigitTemplateSet) -> tuple[str, float]:
    """ROI kirpintisini segment edip her karakteri sablonlarla eslestirir,
    birlestirilmis metni + genel guven skorunu (en dusuk karakter guveni)
    doner.

    Genel guven skoru olarak MINIMUM secildi (ortalama degil) -- bir tek
    karakter bile belirsizse ("?" donmus olsun ya da olmasin) butun okumaya
    guvenilmemeli; ortalama alsaydik 4 karakterden 1'i tamamen yanlis/belirsiz
    olsa bile genel skor yuksek gorunebilirdi (yanlis guven verirdi).

    Hic karakter segment edilemezse (bos ROI, ör. HMI o an hicbir sey
    gostermiyor) ("", 0.0) doner -- bu bir hata degil, "okunacak bir sey yok"
    durumu (screen_reader.read_text_ocr'daki bos-goruntu davranisiyla tutarli).
    """
    char_images = segment_characters(image)
    if not char_images:
        return "", 0.0
    matches = [match_character(char_image, templates) for char_image in char_images]
    text = "".join(label for label, _score in matches)
    confidence = min(score for _label, score in matches)
    return text, confidence


# --- Sentetik/varsayilan sablon seti (yedek, gercek sablon hic yakalanmamissa) ----
# Görev B (2026-09-30): Fatih sahada gercek HMI karakterlerini yakalayana kadar
# (bkz. digit_templates_store.py + ui/admin.html yakalama akisi) sistem YINE DE
# calisir durumda olsun diye -- dusuk dogrulukla da olsa "hic sablon yok" =
# "sistem hic okuyamiyor" durumuna dusulmesin. Gercek sablonlar yakalandikca
# per-label bazinda ezilir (main.py._load_digit_template_set, bkz. orasi).

# Aday TTF yollari -- ilk bulunan kullanilir. DejaVuSans, cogu Debian/Raspberry
# Pi OS kurulumunda (fonts-dejavu-core paketi, siklikla baska paketlerin
# bagimliligi olarak zaten kurulu) bulunur; Arial/Helvetica Mac gelistirme
# ortami icin (bu repo Mac'te de test ediliyor, bkz. pyproject.toml/CI notlari).
_DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _load_default_font(font_size: int):
    """Aday yollardan ilk bulunan TTF'yi yukler. Hicbiri yoksa (ör. minimal/
    headless bir kurulum) PIL'in GOMULU bitmap fontuna duser -- dosya
    sistemine hic bagimli olmadan HER ZAMAN calisir (dusuk cozunurluklu/sabit
    boyutlu olsa da), boylece generate_default_templates() hicbir ortamda
    'font bulunamadi' diye patlamaz."""
    for path in _DEFAULT_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, font_size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render_default_char_image(label: str, font) -> np.ndarray:
    """Tek bir karakteri (label) PIL ile buyuk-ish bir tuvale (canvas) beyaz
    yazi/siyah zemin olarak cizip, ciziminin GERCEKTE kapladigi (sifir
    olmayan) bolgeye siki kirpar -- segment_characters'in ciktisiyla ayni
    'karakterin siki bounding box'i' konvansiyonunu paylassin diye (aksi halde
    sablon cok fazla bos kenar bosluguyla normalize edilir, gercek karakterle
    orani/hizasi bozulur).

    Render tamamen bos donerse (ör. font bu glyph'i hic desteklemiyor --
    beklenmez ama imkansiz degil) tuvalin tamamini (hepsi sifir/siyah) doner;
    cagiran taraf (DigitTemplateSet.add -> _normalize_char_image) bunu ozel
    bir durum kontrolu gerekmeden dogal olarak "duz siyah sablon" olarak isleri.
    """
    canvas_size = 64
    canvas = Image.new("L", (canvas_size, canvas_size), color=0)
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (canvas_size - text_w) // 2 - bbox[0]
    y = (canvas_size - text_h) // 2 - bbox[1]
    draw.text((x, y), label, fill=255, font=font)
    array = np.array(canvas, dtype=np.uint8)

    nonzero_rows = np.where(np.any(array > 0, axis=1))[0]
    nonzero_cols = np.where(np.any(array > 0, axis=0))[0]
    if nonzero_rows.size == 0 or nonzero_cols.size == 0:
        return array
    return array[nonzero_rows[0] : nonzero_rows[-1] + 1, nonzero_cols[0] : nonzero_cols[-1] + 1]


def generate_default_templates(font_size: int = 40) -> DigitTemplateSet:
    """0-9/./- icin PIL ile render edilmis, kullanima HAZIR bir varsayilan
    DigitTemplateSet doner -- Fatih sahada henuz hic gercek sablon yakalamamis
    olsa BILE sistem bu setle (dusuk dogrulukla da olsa) CALISIR durumda
    baslar (bkz. modul ustu blok docstring'i)."""
    font = _load_default_font(font_size)
    template_set = DigitTemplateSet()
    for label in CHARACTER_LABELS:
        template_set.add(label, _render_default_char_image(label, font))
    return template_set
