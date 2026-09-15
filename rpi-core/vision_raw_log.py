"""
FeedVision — Görüntü İşleme Ham Veri Kaydı (15-09-2026 eklendi)

Ne yapar: SADECE görüntü işlemeyle (ROI/OCR) takip edilen Kontrol
Kriterlerinin o anki HAM (parse edilmemiş, olduğu gibi) okumalarını,
Admin'den ayarlanabilir bir periyotla (varsayılan 10sn), sabit genişlikli
(fixed-width) monospace bir tabloya, gün başına bir .txt dosyasına yazar.

Neden journal.py'den AYRI: journal.py (operasyonel journal, .jsonl) hem
STM32 durumunu hem ROI okumalarını hem kural ihlallerini KARIŞIK/JSON
formatında tutuyor — makine/scriptler için uygun ama bir teknisyenin düz
metin editöründe açıp GÖZLE bir tabloyu tarayarak okuması için elverişli
değil. Bu dosya SADECE görüntü işleme (ROI) değerlerine odaklı, insan
gözüyle kolay okunur, hizalı bir tablo üretir (Fatih'in özel talebi).

Format örneği (sütun genişlikleri sabit, değer uzunluğu değişse de hizası
KAYMAZ):
    Zaman               | ui_screen/basinc   | ui_screen/sicaklik
    ------------------------------------------------------------
    2026-09-15 14:32:10  | 4.80 bar           | 62.1 C
    2026-09-15 14:32:20  | 4.81 bar           | 62.3 C

Sütunlar (hangi Kontrol Kriterleri izleniyor) zaman içinde değişebilir
(kriter eklendi/silindi) — böyle bir değişiklik olduğunda dosyaya YENİ bir
başlık satırı basılır (eski satırlar bozulmaz, sadece o noktadan itibaren
yeni sütun düzeni başlar). Bu, log dosyasını sürekli yeniden yazmadan
(pahalı/riskli) şema değişikliğini şeffaf tutmanın en basit yolu.
"""

import json
from datetime import datetime
from pathlib import Path

# Klasör adı kasıtlı olarak Türkçe/insan-okunur (Fatih'in talebi) — Linux
# (Raspberry Pi OS) UTF-8 + boşluklu dosya/klasör adlarını sorunsuz destekler.
LOG_DIR = Path(__file__).resolve().parent / "görüntü işleme raw data"

CONFIG_PATH = Path(__file__).resolve().parent / "vision_raw_log_config.json"
DEFAULT_INTERVAL_S = 10.0

TIMESTAMP_WIDTH = 20  # "YYYY-MM-DD HH:MM:SS" (19 karakter) + 1 pay
COLUMN_WIDTH = 18  # her kriter sütunu icin sabit genislik — deger bundan uzunsa kirpilir
SEPARATOR = " | "


def get_interval_s(config_path: Path = CONFIG_PATH) -> float:
    """Admin'in ayarladığı yazım periyodunu (saniye) döner — hiç ayarlanmadıysa DEFAULT_INTERVAL_S."""
    if not config_path.exists():
        return DEFAULT_INTERVAL_S
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        value = float(data.get("interval_s", DEFAULT_INTERVAL_S))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return DEFAULT_INTERVAL_S
    return value if value > 0 else DEFAULT_INTERVAL_S


def set_interval_s(value: float, config_path: Path = CONFIG_PATH) -> None:
    """Yazım periyodunu kalıcı olarak ayarlar (atomik yazım — roi_store.py ile aynı desen)."""
    if value <= 0:
        raise ValueError("interval_s pozitif olmalı")
    tmp_path = config_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({"interval_s": value}), encoding="utf-8")
    tmp_path.replace(config_path)


def _pad(text: str, width: int) -> str:
    """Metni sabit genişliğe kırpar/boşlukla tamamlar — sütun hizası hiçbir
    zaman değer uzunluğuna göre kaymasın diye (dosyayı düz metin editörde
    açan biri, ekran genişliği ne olursa olsun, sütunları hizalı görsün)."""
    return text[:width].ljust(width)


def format_header(columns: list[str]) -> str:
    parts = [_pad("Zaman", TIMESTAMP_WIDTH)] + [_pad(c, COLUMN_WIDTH) for c in columns]
    return SEPARATOR.join(parts)


def format_row(timestamp_str: str, columns: list[str], values: dict[str, str | None]) -> str:
    parts = [_pad(timestamp_str, TIMESTAMP_WIDTH)]
    for col in columns:
        raw = values.get(col)
        text = "—" if not raw else str(raw)
        parts.append(_pad(text, COLUMN_WIDTH))
    return SEPARATOR.join(parts)


def _file_path_for_today(log_dir: Path) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return log_dir / f"{today}.txt"


class VisionRawLogger:
    """Gün başına bir .txt dosyasına yazan tekil kayıt nesnesi.

    Sınıf olarak tasarlandı ki testler kendi izole log_dir'iyle (gerçek
    LOG_DIR'a dokunmadan) bağımsız test edebilsin (feed_totalizer.py'deki
    aynı desen)."""

    def __init__(self, log_dir: Path = LOG_DIR):
        self._log_dir = log_dir
        self._last_columns: list[str] | None = None  # son yazılan başlığın sütun düzeni

    def write_entry(self, columns: list[str], values: dict[str, str | None]) -> None:
        """Bir satır ekler; sütun düzeni ilk kez görülüyorsa (ya da gün
        değiştiği/servis yeniden başladığı için bilinmiyorsa) önce başlık basar."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = _file_path_for_today(self._log_dir)
        need_header = (not path.exists()) or (self._last_columns != columns)

        with open(path, "a", encoding="utf-8") as f:
            if need_header:
                header = format_header(columns)
                f.write(header + "\n")
                f.write("-" * len(header) + "\n")
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(format_row(timestamp_str, columns, values) + "\n")

        self._last_columns = columns


# Tek, paylaşılan örnek — main.py bunu import edip kullanır (roi_store.py/
# feed_totalizer.py ile aynı desen).
logger = VisionRawLogger()
