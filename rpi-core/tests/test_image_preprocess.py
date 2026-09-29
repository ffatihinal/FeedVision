"""
FeedVision — image_preprocess.py testleri (SADECE saf goruntu islemesi).

Sentetik (numpy ile uretilen) goruntulerle calisir, kameraya/donanima dokunmaz.
"""

import numpy as np

from image_preprocess import apply_clahe


class TestApplyClahe:
    def test_empty_image_returned_unchanged(self):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        result = apply_clahe(empty)
        assert result.size == 0

    def test_output_shape_and_dtype_preserved(self):
        image = np.random.randint(0, 256, (50, 80, 3), dtype=np.uint8)
        result = apply_clahe(image)
        assert result.shape == image.shape
        assert result.dtype == image.dtype

    def test_increases_contrast_of_low_contrast_image(self):
        # Dusuk kontrastli (tum piksel degerleri dar bir aralikta) sentetik goruntu.
        image = np.full((40, 40, 3), 120, dtype=np.uint8)
        image[10:30, 10:30] = 130  # hafif farkli bir bolge
        result = apply_clahe(image)
        # CLAHE sonrasi standart sapma (kontrast) azalmamali — genelde artar/esit kalir.
        assert result.astype(np.float32).std() >= image.astype(np.float32).std()

    def test_does_not_mutate_input(self):
        image = np.random.randint(0, 256, (30, 30, 3), dtype=np.uint8)
        original = image.copy()
        apply_clahe(image)
        np.testing.assert_array_equal(image, original)
