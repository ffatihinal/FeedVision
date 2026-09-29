"""
FeedVision — genel goruntu on-isleme (preprocessing) kancasi

Ne yapar: ROI kirpma isleminden ONCE, yakalanan karenin tamamina
uygulanabilecek on-isleme adimlarini toplar. Simdilik SADECE CLAHE
(kontrast sinirli adaptif histogram esitleme) var — daha once arastirilmis
"HMI ekranindaki sayisal gostergeler dusuk kontrastta kalirsa OCR
zorlanir" bulgusunun somut karsiligi. Adaptif esikleme (ileride eklenebilir)
bu turun kapsaminda degil.

Neden ayri dosya: screen_reader.py'nin (ROI kirpma + OCR/renk) sorumlulugunu
buyutmeden, "kareyi ROI'den ONCE nasil iyilestiririz" sorusu izole kalsin diye.
"""

import cv2
import numpy as np


def apply_clahe(image: np.ndarray, clip_limit: float = 2.0, tile_grid_size: tuple[int, int] = (8, 8)) -> np.ndarray:
    """CLAHE'yi SADECE parlaklik (L) kanaline uygular — LAB renk uzayina
    gecip a/b (renk) kanallarina dokunmadan geri BGR'ye doner. Boylece
    durum karelerinin (Grup 2) renk kimligi (mavi dolu/bos) bozulmadan
    Grup 1/3'un sayisal gostergelerindeki kontrast artar.

    Bos goruntude (boyut 0) oldugu gibi geri doner — cv2 CLAHE bos
    goruntude exception firlatir, cagiran taraf (main.py) zaten boyutu
    kontrol etmeden bu fonksiyonu cagirabilsin diye burada korunuyor.
    """
    if image.size == 0:
        return image
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)
    l_equalized = clahe.apply(l_channel)
    lab_equalized = cv2.merge((l_equalized, a_channel, b_channel))
    return cv2.cvtColor(lab_equalized, cv2.COLOR_LAB2BGR)
