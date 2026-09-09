#!/usr/bin/env bash
# FeedVision -- kod guncellemesi sonrasi servisi yenile.
#
# Kullanimi (RPi'de, repo kok dizininde ya da herhangi bir yerden):
#   bash rpi-core/scripts/update_and_restart.sh
#
# Ne yapar: git pull -> (varsa) yeni pip bagimliliklarini kurar -> servisi
# restart eder -> son 20 satir logu gosterip saglik durumunu ozetler.
set -e
cd "$(dirname "$0")/../.."   # repo kok dizini (FeedVision/)

echo "1/4 - git pull"
git pull

echo "2/4 - bagimliliklar guncelleniyor"
./rpi-core/.venv/bin/pip install -r rpi-core/requirements.txt --quiet

echo "3/4 - servis yeniden baslatiliyor"
sudo systemctl restart feedvision

echo "4/4 - durum kontrolu (3 sn bekleniyor)"
sleep 3
sudo systemctl --no-pager --lines=20 status feedvision || true

if sudo systemctl is-active --quiet feedvision; then
  echo ""
  echo "OK - feedvision servisi calisiyor."
else
  echo ""
  echo "HATA - servis calismiyor, yukaridaki loga bak: journalctl -u feedvision -n 50"
  exit 1
fi
