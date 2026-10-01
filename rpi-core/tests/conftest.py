"""
FeedVision — pytest ortak fixture'ları.

Kural: testler ASLA gerçek `rules_config.json`, `roi_config.json`,
`calibration_config.json`, `feed_total_state.json` gibi üretim dosyalarına
dokunmaz. Bu dosyalar operatörün sahada girdiği gerçek veriyi tutuyor —
bir testin üzerine yazması/silmesi saha verisini kaybettirir. Aşağıdaki
fixture'lar ilgili modüllerin CONFIG_PATH/STATE_PATH sabitlerini
`tmp_path`'e yönlendirir (monkeypatch ile), test bitince otomatik geri alınır.
"""

import sys
from pathlib import Path

import pytest

# rpi-core'u import edilebilir kılmak için (testler rpi-core/tests altında,
# modüller bir üst dizinde).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def isolated_rules_config(tmp_path, monkeypatch):
    """rules_store.CONFIG_PATH'i geçici bir dosyaya yönlendirir."""
    import rules_store

    path = tmp_path / "rules_config.json"
    monkeypatch.setattr(rules_store, "CONFIG_PATH", path)
    return path


@pytest.fixture
def isolated_roi_config(tmp_path, monkeypatch):
    """roi_store.CONFIG_PATH'i geçici bir dosyaya yönlendirir."""
    import roi_store

    path = tmp_path / "roi_config.json"
    monkeypatch.setattr(roi_store, "CONFIG_PATH", path)
    return path


@pytest.fixture
def isolated_calibration_config(tmp_path, monkeypatch):
    """calibration_store.CONFIG_PATH'i (VE şablon JPEG'lerinin yazıldığı
    TEMPLATES_DIR'i) geçici bir dizine yönlendirir — şablon testleri gerçek
    rpi-core/calibration_templates/ klasörüne dosya yazmasın diye (2026-09-30,
    Görev A: add_template artık diske gerçek JPEG yazıyor)."""
    import calibration_store

    path = tmp_path / "calibration_config.json"
    monkeypatch.setattr(calibration_store, "CONFIG_PATH", path)
    monkeypatch.setattr(calibration_store, "TEMPLATES_DIR", tmp_path / "calibration_templates")
    return path


@pytest.fixture
def isolated_digit_templates_store(tmp_path, monkeypatch):
    """digit_templates_store.CONFIG_PATH'i (VE şablon JPEG'lerinin yazıldığı
    TEMPLATES_DIR'i) geçici bir dizine yönlendirir — calibration_store'daki
    isolated_calibration_config fixture'ıyla aynı gerekçe (2026-09-30, Görev B:
    testler gerçek rpi-core/digit_templates/ klasörüne dosya yazmasın diye)."""
    import digit_templates_store

    path = tmp_path / "digit_templates_config.json"
    monkeypatch.setattr(digit_templates_store, "CONFIG_PATH", path)
    monkeypatch.setattr(digit_templates_store, "TEMPLATES_DIR", tmp_path / "digit_templates")
    return path


@pytest.fixture
def isolated_feed_total_state(tmp_path, monkeypatch):
    """feed_totalizer.STATE_PATH'i geçici bir dosyaya yönlendirir."""
    import feed_totalizer

    path = tmp_path / "feed_total_state.json"
    monkeypatch.setattr(feed_totalizer, "STATE_PATH", path)
    return path


@pytest.fixture
def isolated_motion_params_config(tmp_path, monkeypatch):
    """motion_params.CONFIG_PATH'i geçici bir dosyaya yönlendirir."""
    import motion_params

    path = tmp_path / "motion_params.json"
    monkeypatch.setattr(motion_params, "CONFIG_PATH", path)
    return path


@pytest.fixture
def isolated_vision_settings_config(tmp_path, monkeypatch):
    """vision_settings_store.CONFIG_PATH'i geçici bir dosyaya yönlendirir."""
    import vision_settings_store

    path = tmp_path / "vision_settings.json"
    monkeypatch.setattr(vision_settings_store, "CONFIG_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _reset_roi_read_cache():
    """main._roi_read_cache (01-10-2026, ROI okuma CPU önbelleği) modül
    seviyesinde, test'ler arasında sızabilir — ör. iki test aynı boş/sabit
    kareyi kullanırsa ikinci test yanlışlıkla birincinin önbelleğinden sonuç
    alabilir. Her testten önce VE sonra temizlenir (otomatik, her test
    dosyasına ayrı ayrı eklemeye gerek kalmasın diye)."""
    import main

    main.reset_roi_read_cache()
    yield
    main.reset_roi_read_cache()
