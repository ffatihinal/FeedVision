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
echo "Kurulum tamam. Çalıştırmak için:"
echo "  ./.venv/bin/python3 main.py"
echo ""
echo "Tarayıcıdan erişmek için (aynı ağdaki başka bir cihazdan da):"
echo "  http://$(hostname -I | awk '{print $1}'):8000"
