"""
FeedVision — MP3 tabanlı sesli alarm altyapısı (15-09-2026 eklendi)

Ne yapar: `ui/alarm_sounds/` klasöründeki MP3 dosyalarını listeler ve
güvenli (path traversal korumalı) şekilde tek tek servis eder. Her Kontrol
Kriteri (bkz. main.py RuleDef.alarm_sound) kendi MP3'ünü seçebilir —
seçilmemişse UI tarafında mevcut Web Audio ton sistemi (kritik/uyarı bip)
devreye girer, bu dosya SADECE dosya listesi/servis tarafı.

Fiziksel USB hoparlör donanımı bugün YOK — bu yüzden gerçek ses çıkışı
sahada test edilemiyor, ama dosya seçimi + tarayıcı üzerinden çalma (kiosk
Chromium'un kendi ses çıkışı) test edilebilir. Klasör başlangıçta boş
olabilir; Fatih hazır MP3'leri buraya bırakınca liste otomatik dolar,
kod değişikliği gerekmez.
"""

from pathlib import Path

ALARM_SOUNDS_DIR = Path(__file__).resolve().parent.parent / "ui" / "alarm_sounds"


def list_alarm_sounds(sounds_dir: Path = ALARM_SOUNDS_DIR) -> list[str]:
    """Klasördeki .mp3 dosyalarının adlarını (yol değil, sadece dosya adı) alfabetik döner.

    Klasör hiç yoksa (henüz hiç MP3 konulmadıysa) boş liste döner — hata değil."""
    if not sounds_dir.exists():
        return []
    return sorted(p.name for p in sounds_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mp3")


def resolve_sound_path(filename: str, sounds_dir: Path = ALARM_SOUNDS_DIR) -> Path | None:
    """Bir dosya adını güvenli şekilde tam yola çevirir.

    Path traversal koruması: sadece klasörün DOĞRUDAN içindeki, yol ayıracı
    (/ veya \\) İÇERMEYEN bir isim kabul edilir — "../../etc/passwd" gibi
    bir istek asla klasör dışına çıkamaz. Dosya yoksa/klasör dışındaysa None
    döner (çağıran taraf bunu 404 olarak ele alır).
    """
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        return None
    candidate = (sounds_dir / filename).resolve()
    try:
        candidate.relative_to(sounds_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file() or candidate.suffix.lower() != ".mp3":
        return None
    return candidate
