"""
FeedVision — Operasyonel Journal (madde 6)

Ne yapar: ekrandan takip edilen tüm değerleri (STM32 durumu + kayıtlı ROI
okumaları + o anki kural ihlalleri) belirli periyotla diske yazar. main.py
STM32 komut geçmişini/besleme miktarını AYRICA loglamıyor — bu journal
SADECE "operatörün o an gördüğü değerler zaman içinde ne oldu" kaydı
(bkz. ui/index.html'deki yorum: motor komutu/besleme miktarı journal'ı
kapsam dışı, bu operasyonel journal ONDAN AYRI).

Neden gün başına yeni dosya: tek büyük dosya zamanla şişer (arama/açma
yavaşlar), günlük dosya hem "bugün ne oldu"yu kolay bulunur kılar hem de
eski günleri silmek/arşivlemek (disk doluysa) basit bir dosya işlemine
indirger. JSON Lines (.jsonl) formatı seçildi: her satır bağımsız bir JSON
nesnesi — yazım yarıda kesilse (güç kesintisi) bile önceki satırlar
bozulmaz, tek büyük bir JSON dizisinin ortası kesilirse tüm dosya bozuk
olurdu.
"""

import json
import time
from datetime import datetime
from pathlib import Path

JOURNAL_DIR = Path(__file__).resolve().parent / "journal"


def _file_path_for_today(journal_dir: Path = JOURNAL_DIR) -> Path:
    """Bugünün tarihine (yerel saat) göre dosya yolunu döner — gün değişince
    otomatik olarak yeni bir dosyaya yazmaya başlar, ayrı bir "gün değişti mi"
    kontrolüne gerek yok, her çağrıda tarih yeniden hesaplanıyor."""
    today = datetime.now().strftime("%Y-%m-%d")
    return journal_dir / f"{today}.jsonl"


def write_entry(entry: dict, journal_dir: Path = JOURNAL_DIR) -> None:
    """Bir kayıt satırını bugünün journal dosyasına ekler (append).

    journal_dir parametresi test edilebilirlik için var — testler gerçek
    rpi-core/journal/ klasörüne yazmadan geçici bir dizin verebilir.
    entry'ye "timestamp" alanı otomatik eklenir (unix epoch, saniye) —
    çağıran taraf bunu tekrar eklemek zorunda değil.
    """
    journal_dir.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": time.time(), **entry}
    line = json.dumps(record, ensure_ascii=False)
    path = _file_path_for_today(journal_dir)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
