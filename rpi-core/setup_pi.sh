#!/usr/bin/env bash
# FeedVision RPi Core -- ilk kurulum (Raspberry Pi'de BİR KERE çalıştırılır).
#
# Ne yapar: proje için ayrı/izole bir Python "araç kutusu" (venv) oluşturur,
# gerekli paketleri (fastapi, uvicorn, opencv, pyserial) oraya kurar.
# --system-site-packages ile açılıyor ki ileride kamera için apt'tan kurulacak
# picamera2 de (sistem paketi, pip'ten kurulamıyor) bu venv içinden görünsün.
#
# Kullanımı:
#   cd FeedVision/rpi-core
#   bash setup_pi.sh
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Sanal ortam (venv) oluşturuluyor..."
  python3 -m venv --system-site-packages .venv
else
  echo "Sanal ortam zaten var, atlanıyor."
fi

echo "Paketler kuruluyor..."
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo ""
echo "Kurulum tamam."

# --- systemd servisi: boot'ta otomatik başlatma -----------------------------
# Sadece Linux'ta (RPi) kurulur; Mac'te geliştirme sırasında bu adım atlanır.
# Idempotent: tekrar çalıştırılırsa (kod güncellemesi sonrası setup_pi.sh
# yine çağrılabilir) unit dosyası içeriği değişmediyse hiçbir şey yapmaz,
# değiştiyse günceller + daemon-reload eder. Servis zaten çalışıyorsa
# durdurup yeniden kurmaz, sadece restart eder.
if [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
  echo ""
  echo "systemd servisi kuruluyor (boot'ta otomatik başlama)..."

  APP_DIR="$(cd .. && pwd)"                 # repo kök dizini (FeedVision/)
  SERVICE_USER="$(whoami)"
  SERVICE_GROUP="$(id -gn)"
  UNIT_PATH="/etc/systemd/system/feedvision.service"

  # Web'den /system/logs ucu icin journalctl sudo'suz calisabilsin diye
  # kullanici systemd-journal grubuna eklenir (idempotent -- zaten uyeyse
  # usermod hicbir sey degistirmez).
  if ! id -nG "$SERVICE_USER" | grep -qw "systemd-journal"; then
    sudo usermod -a -G systemd-journal "$SERVICE_USER"
    echo "  Kullanici '$SERVICE_USER' systemd-journal grubuna eklendi (etkinlesmesi icin oturum/servis yeniden baslamali)."
  fi

  RENDERED="$(sed \
    -e "s#__APP_DIR__#${APP_DIR}#g" \
    -e "s#__SERVICE_USER__#${SERVICE_USER}#g" \
    -e "s#__SERVICE_GROUP__#${SERVICE_GROUP}#g" \
    feedvision.service.template)"

  if [ -f "$UNIT_PATH" ] && [ "$(sudo cat "$UNIT_PATH" 2>/dev/null)" = "$RENDERED" ]; then
    echo "  Unit dosyası zaten güncel, atlanıyor."
  else
    echo "$RENDERED" | sudo tee "$UNIT_PATH" > /dev/null
    sudo systemctl daemon-reload
    echo "  Unit dosyası yazıldı: $UNIT_PATH"
  fi

  # Log rotasyonu: haftalar/aylar süren saha çalışmasında disk dolmasın diye
  # journald'e üst sınır (idempotent: dosya aynıysa dokunmaz).
  JOURNALD_DROPIN="/etc/systemd/journald.conf.d/feedvision.conf"
  sudo mkdir -p /etc/systemd/journald.conf.d
  if [ -f "$JOURNALD_DROPIN" ] && diff -q journald-feedvision.conf "$JOURNALD_DROPIN" >/dev/null 2>&1; then
    : # zaten güncel
  else
    sudo cp journald-feedvision.conf "$JOURNALD_DROPIN"
    sudo systemctl restart systemd-journald
    echo "  journald log limiti kuruldu (200M / 30 gün): $JOURNALD_DROPIN"
  fi

  sudo systemctl enable feedvision
  if sudo systemctl is-active --quiet feedvision; then
    sudo systemctl restart feedvision
    echo "  Servis yeniden başlatıldı (kod/ayar güncel)."
  else
    sudo systemctl start feedvision
    echo "  Servis başlatıldı."
  fi

  echo ""
  echo "Durum:      sudo systemctl status feedvision"
  echo "Loglar:     journalctl -u feedvision -f"
  echo "Güncelleme: bash rpi-core/scripts/update_and_restart.sh"
else
  echo "  (Linux/systemd bulunamadı, servis kurulum adımı atlandı -- bu normal, Mac'te geliştirme yapılıyor.)"
  echo "  Manuel çalıştırmak için: ./.venv/bin/python3 main.py"
fi

echo ""
echo "Tarayıcıdan erişmek için (aynı ağdaki başka bir cihazdan da):"
echo "  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
