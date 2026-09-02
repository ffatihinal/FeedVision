#!/usr/bin/env python3
"""
FeedVision Test UI — NUCLEO-G031K8 test arayuzu

Calistirma:
    pip install -r requirements.txt
    python3 feedvision_test_ui.py

Kart baglantisi: USB kablosu -> ST-LINK sanal COM portu -> USART2 -> 115200 baud
"""

import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import serial
from serial.tools import list_ports

BAUD = 115200
UI_YENILEME_MS = 60          # ekrani saniyede ~16 kez tazele


class SeriPort:
    """Seri portu ayri bir is parcaciginda (thread) dinler.

    Neden ayri thread: seri porttan veri beklemek programi dondurur; arayuz
    donmasin diye okuma isini arka plana aliyoruz. Gelen satirlar bir kuyruga
    (queue) atilir, arayuz o kuyrugu periyodik olarak bosaltir.
    """

    def __init__(self):
        self.ser = None
        self.kuyruk = queue.Queue()
        self._calisiyor = False
        self._thread = None

    @property
    def bagli(self):
        return self.ser is not None and self.ser.is_open

    def bagla(self, port):
        self.ser = serial.Serial(port, BAUD, timeout=0.2)
        self._calisiyor = True
        self._thread = threading.Thread(target=self._oku_dongusu, daemon=True)
        self._thread.start()

    def kes(self):
        self._calisiyor = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def gonder(self, sozluk):
        """Python sozlugunu tek satir JSON'a cevirip karta yollar."""
        if not self.bagli:
            raise IOError("Seri port bagli degil")
        satir = json.dumps(sozluk, separators=(",", ":")) + "\n"
        self.ser.write(satir.encode("ascii"))
        self.ser.flush()
        return satir.strip()

    def _oku_dongusu(self):
        tampon = b""
        while self._calisiyor:
            try:
                veri = self.ser.read(256)
            except Exception as e:
                self.kuyruk.put(("hata", str(e)))
                break
            if not veri:
                continue
            tampon += veri
            while b"\n" in tampon:
                ham, tampon = tampon.split(b"\n", 1)
                metin = ham.decode("ascii", errors="ignore").strip()
                if not metin:
                    continue
                try:
                    self.kuyruk.put(("veri", json.loads(metin)))
                except json.JSONDecodeError:
                    self.kuyruk.put(("metin", metin))


class Uygulama(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("FeedVision — Motor / Encoder Test")
        self.resizable(False, False)

        self.seri = SeriPort()
        self._arayuzu_kur()
        self.after(UI_YENILEME_MS, self._kuyrugu_isle)
        self.protocol("WM_DELETE_WINDOW", self._kapat)

    # ------------------------------------------------------------------ arayuz
    def _arayuzu_kur(self):
        ana = ttk.Frame(self, padding=10)
        ana.grid(row=0, column=0)

        # ---------------- Baglanti ----------------
        kutu_baglanti = ttk.LabelFrame(ana, text="Baglanti", padding=8)
        kutu_baglanti.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.port_secim = ttk.Combobox(kutu_baglanti, width=28, state="readonly")
        self.port_secim.grid(row=0, column=0, padx=(0, 6))

        ttk.Button(kutu_baglanti, text="Portlari Tara",
                   command=self._portlari_tara).grid(row=0, column=1, padx=3)

        self.btn_baglan = ttk.Button(kutu_baglanti, text="Seri Port Baglan",
                                     command=self._baglan_kes)
        self.btn_baglan.grid(row=0, column=2, padx=3)

        self.lbl_durum = ttk.Label(kutu_baglanti, text="Bagli degil",
                                   foreground="#b00000")
        self.lbl_durum.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---------------- Step motor ----------------
        kutu_step = ttk.LabelFrame(ana, text="Step Motor (Nema23 / DM556)", padding=8)
        kutu_step.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(kutu_step, text="Hiz — darbe araligi (us):").grid(
            row=0, column=0, sticky="w")
        self.giris_hiz = ttk.Entry(kutu_step, width=12)
        self.giris_hiz.insert(0, "500")
        self.giris_hiz.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(kutu_step, text="kucuk deger = hizli (min 20)",
                  foreground="#666").grid(row=0, column=2, sticky="w")

        ttk.Label(kutu_step, text="Mesafe — step sayisi:").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        self.giris_step = ttk.Entry(kutu_step, width=12)
        self.giris_step.insert(0, "2000")
        self.giris_step.grid(row=1, column=1, sticky="w", padx=6, pady=(4, 0))

        self.yon = tk.IntVar(value=1)
        cerceve_yon = ttk.Frame(kutu_step)
        cerceve_yon.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(cerceve_yon, text="Yon:").grid(row=0, column=0)
        ttk.Radiobutton(cerceve_yon, text="Ileri", variable=self.yon,
                        value=1).grid(row=0, column=1, padx=6)
        ttk.Radiobutton(cerceve_yon, text="Geri", variable=self.yon,
                        value=0).grid(row=0, column=2)

        ttk.Button(kutu_step, text="Harekete Gec",
                   command=self._step_baslat).grid(row=3, column=0, columnspan=2,
                                                   sticky="ew", pady=(8, 0))
        ttk.Button(kutu_step, text="DURDUR",
                   command=self._step_durdur).grid(row=3, column=2,
                                                   sticky="ew", pady=(8, 0))

        # ---------------- DC motor ----------------
        kutu_dc = ttk.LabelFrame(ana, text="DC Motor 6V (L9110)", padding=8)
        kutu_dc.grid(row=2, column=0, sticky="ew", pady=(0, 8))

        ttk.Button(kutu_dc, text="Ileri",
                   command=lambda: self._dc("ileri")).grid(row=0, column=0, padx=4)
        ttk.Button(kutu_dc, text="Geri",
                   command=lambda: self._dc("geri")).grid(row=0, column=1, padx=4)
        ttk.Button(kutu_dc, text="Dur",
                   command=lambda: self._dc("dur")).grid(row=0, column=2, padx=4)

        # ---------------- Encoder ----------------
        kutu_enc = ttk.LabelFrame(ana, text="Encoder Verisi", padding=8)
        kutu_enc.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        yazi_tipi = ("TkDefaultFont", 12, "bold")

        ttk.Label(kutu_enc, text="Encoder 1 (motorlu tekerlek):").grid(
            row=0, column=0, sticky="w")
        self.lbl_enc1 = ttk.Label(kutu_enc, text="—", font=yazi_tipi)
        self.lbl_enc1.grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(kutu_enc, text="Encoder 2 (bosta tekerlek):").grid(
            row=1, column=0, sticky="w", pady=(4, 0))
        self.lbl_enc2 = ttk.Label(kutu_enc, text="—", font=yazi_tipi)
        self.lbl_enc2.grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))

        ttk.Label(kutu_enc, text="Fark (patinaj):").grid(
            row=2, column=0, sticky="w", pady=(4, 0))
        self.lbl_fark = ttk.Label(kutu_enc, text="—", font=yazi_tipi)
        self.lbl_fark.grid(row=2, column=1, sticky="w", padx=8, pady=(4, 0))

        self.lbl_kalan = ttk.Label(kutu_enc, text="Kalan step: —  |  Durum: —")
        self.lbl_kalan.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Button(kutu_enc, text="Encoder Sayaclarini Sifirla",
                   command=self._sifirla).grid(row=4, column=0, columnspan=2,
                                               sticky="ew", pady=(8, 0))

        # ---------------- Log ----------------
        kutu_log = ttk.LabelFrame(ana, text="Kayit", padding=8)
        kutu_log.grid(row=4, column=0, sticky="ew")

        self.log = tk.Text(kutu_log, width=58, height=8, state="disabled")
        self.log.grid(row=0, column=0)
        kaydirma = ttk.Scrollbar(kutu_log, command=self.log.yview)
        kaydirma.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=kaydirma.set)

        self._portlari_tara()

    # ------------------------------------------------------------------ yardim
    def _logla(self, metin):
        self.log.configure(state="normal")
        self.log.insert("end", metin + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _portlari_tara(self):
        portlar = [p.device for p in list_ports.comports()]
        self.port_secim["values"] = portlar
        if portlar and not self.port_secim.get():
            # ST-LINK genelde adinda "usbmodem" (mac) veya "COM" (Windows) gecer
            tercih = [p for p in portlar if "usbmodem" in p.lower()]
            self.port_secim.set(tercih[0] if tercih else portlar[0])

    def _tamsayi_oku(self, giris, ad):
        try:
            return int(giris.get().strip())
        except ValueError:
            messagebox.showerror("Hatali giris", f"{ad} icin tam sayi gir.")
            return None

    def _komut(self, sozluk):
        if not self.seri.bagli:
            messagebox.showwarning("Bagli degil", "Once seri porta baglan.")
            return
        try:
            self._logla("-> " + self.seri.gonder(sozluk))
        except Exception as e:
            messagebox.showerror("Gonderilemedi", str(e))

    # ------------------------------------------------------------------ olaylar
    def _baglan_kes(self):
        if self.seri.bagli:
            self.seri.kes()
            self.btn_baglan.configure(text="Seri Port Baglan")
            self.lbl_durum.configure(text="Bagli degil", foreground="#b00000")
            self._logla("Baglanti kesildi.")
            return

        port = self.port_secim.get().strip()
        if not port:
            messagebox.showwarning("Port yok", "Once bir seri port sec.")
            return
        try:
            self.seri.bagla(port)
        except Exception as e:
            messagebox.showerror("Baglanamadi", str(e))
            return

        self.btn_baglan.configure(text="Baglantiyi Kes")
        self.lbl_durum.configure(text=f"Bagli: {port} @ {BAUD}",
                                 foreground="#006000")
        self._logla(f"Baglandi: {port}")

    def _step_baslat(self):
        hiz = self._tamsayi_oku(self.giris_hiz, "Hiz (delay)")
        adet = self._tamsayi_oku(self.giris_step, "Mesafe (step sayisi)")
        if hiz is None or adet is None:
            return
        self._komut({"cmd": "step", "dir": self.yon.get(),
                     "delay": hiz, "steps": adet})

    def _step_durdur(self):
        self._komut({"cmd": "stop"})

    def _dc(self, yon):
        self._komut({"cmd": "dc", "dir": yon})

    def _sifirla(self):
        self._komut({"cmd": "sifirla"})

    # ------------------------------------------------------- gelen veriyi isle
    def _kuyrugu_isle(self):
        son_durum = None
        while True:
            try:
                tur, icerik = self.seri.kuyruk.get_nowait()
            except queue.Empty:
                break

            if tur == "veri":
                if "e1" in icerik:
                    son_durum = icerik          # akan durum satiri: sadece sonuncusu onemli
                else:
                    self._logla("<- " + json.dumps(icerik, ensure_ascii=False))
            elif tur == "metin":
                self._logla("<- " + icerik)
            elif tur == "hata":
                self._logla("!! seri port hatasi: " + icerik)

        if son_durum is not None:
            self._durumu_goster(son_durum)

        self.after(UI_YENILEME_MS, self._kuyrugu_isle)

    def _durumu_goster(self, d):
        # Firmware mesafeyi mikrometre olarak gonderiyor, mm'e burada ceviriyoruz.
        mm1 = d.get("um1", 0) / 1000.0
        mm2 = d.get("um2", 0) / 1000.0

        self.lbl_enc1.configure(text=f"{d.get('e1', 0):>8} sayim   |  {mm1:8.2f} mm")
        self.lbl_enc2.configure(text=f"{d.get('e2', 0):>8} sayim   |  {mm2:8.2f} mm")
        self.lbl_fark.configure(text=f"{mm1 - mm2:8.2f} mm")

        durum = "HAREKET EDIYOR" if d.get("calisiyor") else "BOSTA"
        dc_metin = {0: "dur", 1: "ileri", 2: "geri"}.get(d.get("dc", 0), "?")
        self.lbl_kalan.configure(
            text=f"Kalan step: {d.get('kalan', 0)}  |  Step: {durum}  |  DC: {dc_metin}")

    def _kapat(self):
        if self.seri.bagli:
            try:
                self.seri.gonder({"cmd": "stop"})
                self.seri.gonder({"cmd": "dc", "dir": "dur"})
            except Exception:
                pass
            self.seri.kes()
        self.destroy()


if __name__ == "__main__":
    Uygulama().mainloop()
