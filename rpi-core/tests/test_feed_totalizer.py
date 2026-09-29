"""
FeedVision — feed_totalizer.py testleri.

Gerçek feed_total_state.json'a dokunmamak için her testte
`isolated_feed_total_state` fixture'ı (conftest.py) kullanılır — bu
STATE_PATH'i tmp_path'e yönlendirir. FeedTotalizer ayrıca kendi
state_path parametresini de kabul ettiği için, testlerde doğrudan bu
tmp path ile örnek oluşturuyoruz (modül-seviyesi `totalizer` tekiline
dokunmadan).

Yön-farkındalığı (29-09-2026, Madde 5): step_dir=0 İLERİ, step_dir=1 GERİ
(main.py `_last_step_dir` ile aynı kural). update() çağrılarında artık
step_dir AÇIKÇA veriliyor — üretim kodunda main.py her zaman geçiyor,
default (None) sadece "yön bilinmiyor" durumunu simüle eden testlerde
kullanılıyor.
"""

import json

from feed_totalizer import FeedTotalizer, _default_state, _load_state, _save_state


# ---------------------------------------------------------------------------
# _load_state / _save_state
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_no_file_returns_default(self, isolated_feed_total_state):
        state = _load_state(isolated_feed_total_state)
        assert state["total_forward_mm_e1"] == 0.0
        assert state["total_backward_mm_e1"] == 0.0
        assert state["prev_um1"] is None

    def test_corrupt_json_returns_default(self, isolated_feed_total_state):
        isolated_feed_total_state.write_text("{bozuk json", encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state == _default_state(state["date"])

    def test_different_date_resets(self, isolated_feed_total_state):
        stale = {
            "date": "2020-01-01",
            "total_forward_mm_e1": 42.0,
            "total_forward_mm_e2": 40.0,
            "total_backward_mm_e1": 5.0,
            "total_backward_mm_e2": 5.0,
            "prev_um1": 1000,
            "prev_um2": 1000,
        }
        isolated_feed_total_state.write_text(json.dumps(stale), encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state["total_forward_mm_e1"] == 0.0
        assert state["total_backward_mm_e1"] == 0.0
        assert state["date"] != "2020-01-01"

    def test_missing_field_returns_default(self, isolated_feed_total_state):
        # "prev_um2" eksik — beklenmedik/eski şema
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        partial = {"date": today, "total_forward_mm_e1": 5.0, "prev_um1": None}
        isolated_feed_total_state.write_text(json.dumps(partial), encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state["total_forward_mm_e1"] == 0.0

    def test_old_schema_without_direction_split_resets(self, isolated_feed_total_state):
        # Yön ayrımından ÖNCEKİ eski şema (sadece total_mm_e1/e2) — yeni
        # zorunlu alanlar (total_forward_mm_e1 vb.) eksik olduğu için
        # sessizce sıfırlanmalı (geriye dönük veri kaybı kabul edilebilir).
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        old_schema = {"date": today, "total_mm_e1": 42.0, "total_mm_e2": 40.0, "prev_um1": 1000, "prev_um2": 1000}
        isolated_feed_total_state.write_text(json.dumps(old_schema), encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state["total_forward_mm_e1"] == 0.0
        assert state["total_backward_mm_e1"] == 0.0

    def test_save_then_load_roundtrip(self, isolated_feed_total_state):
        state = _default_state("2099-01-01")
        state["total_forward_mm_e1"] = 12.5
        _save_state(state, isolated_feed_total_state)
        assert isolated_feed_total_state.exists()
        reloaded = json.loads(isolated_feed_total_state.read_text(encoding="utf-8"))
        assert reloaded["total_forward_mm_e1"] == 12.5


# ---------------------------------------------------------------------------
# FeedTotalizer.update / get_summary — yön-farkındalıksız temel davranış
# (ilk örnek, hareketsizlik, negatif delta, eksik alan) — step_dir=0 (ileri)
# ile test edilir, davranış madde 5 ÖNCESİYLE AYNI kalmalı bu senaryolarda.
# ---------------------------------------------------------------------------

class TestFeedTotalizerUpdateBasics:
    def test_first_sample_no_addition_only_sets_prev(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = totalizer.update({"um1": 1000, "um2": 1000, "remaining": 500}, step_dir=0)
        # ilk örnekte prev yoktu, toplama eklenmez
        assert summary["total_mm_e1"] == 0.0
        assert summary["total_mm_e2"] == 0.0

    def test_not_moving_does_not_add_but_updates_prev(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 1000, "um2": 1000, "remaining": 500}, step_dir=0)
        # remaining=0 iken buyuk bir sicrama gelse de eklenmemeli
        summary = totalizer.update({"um1": 9000, "um2": 9000, "remaining": 0}, step_dir=0)
        assert summary["total_mm_e1"] == 0.0
        assert summary["total_mm_e2"] == 0.0
        # ama referans güncellenmiş olmalı — sonraki hareketli örnekte sahte
        # sıçrama oluşmasın
        summary2 = totalizer.update({"um1": 9500, "um2": 9200, "remaining": 100}, step_dir=0)
        assert summary2["total_mm_e1"] == 0.5
        assert summary2["total_mm_e2"] == 0.2

    def test_negative_delta_not_added(self, isolated_feed_total_state):
        # encoder resetlendi/geri döndü senaryosu — negatif fark sayılmaz
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 5000, "um2": 5000, "remaining": 500}, step_dir=0)
        summary = totalizer.update({"um1": 1000, "um2": 5000, "remaining": 300}, step_dir=0)
        assert summary["total_mm_e1"] == 0.0
        assert summary["total_mm_e2"] == 0.0

    def test_missing_um_fields_skipped_silently(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = totalizer.update({"remaining": 500}, step_dir=0)
        assert summary["total_mm_e1"] == 0.0
        assert summary["date"]

    def test_empty_status_dict_does_not_crash(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = totalizer.update({}, step_dir=0)
        assert summary["total_mm_e1"] == 0.0


# ---------------------------------------------------------------------------
# Yön-farkındalığı (Madde 5, EN KRİTİK) — ileri/geri ayrı toplanıp net
# hesaplanıyor mu.
# ---------------------------------------------------------------------------

class TestFeedTotalizerDirectionAware:
    def test_forward_movement_increases_net_total(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        summary = totalizer.update({"um1": 50000, "um2": 50000, "remaining": 300}, step_dir=0)
        assert summary["total_mm_e1"] == 50.0
        assert summary["total_mm_e2"] == 50.0
        assert summary["total_forward_mm_e1"] == 50.0
        assert summary["total_backward_mm_e1"] == 0.0

    def test_backward_movement_does_not_increase_net_total(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=1)
        # step_dir=1 (geri) iken encoder artışı (um artıyor, motor geri
        # giderken de fiziksel olarak encoder count'u artırabilir — encoder
        # işaretli sayım firmware tarafında; burada testin amacı SADECE
        # totalizer'ın step_dir'e göre hangi kovaya yazdığı) backward'a gider.
        summary = totalizer.update({"um1": 20000, "um2": 20000, "remaining": 300}, step_dir=1)
        assert summary["total_mm_e1"] == -20.0  # net = forward(0) - backward(20)
        assert summary["total_forward_mm_e1"] == 0.0
        assert summary["total_backward_mm_e1"] == 20.0

    def test_forward_100_then_backward_20_then_forward_20_nets_to_100(self, isolated_feed_total_state):
        # Fatih'in tam tarif ettiği senaryo: net 100mm olmalı, 140mm DEĞİL.
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 1000}, step_dir=0)
        # ileri 100mm
        totalizer.update({"um1": 100_000, "um2": 100_000, "remaining": 500}, step_dir=0)
        # geri 20mm (um değeri firmware'de yine artan işaretli sayım olabilir
        # ama totalizer'ın umursadığı TEK şey: hangi kovaya yazılacağı, delta
        # her zaman pozitif alınır — bkz. modül docstring'i "negatif delta
        # sayılmaz" kuralı, o yüzden burada um'u ARTIRARAK 20mm'lik bir
        # "geri" hareketi simüle ediyoruz)
        totalizer.update({"um1": 120_000, "um2": 120_000, "remaining": 300}, step_dir=1)
        # tekrar ileri 20mm — remaining>0 (hâlâ hareket halinde) OLMALI,
        # yoksa is_moving=False olur ve bu son delta hiç sayılmaz (mevcut
        # "is_moving CARİ örneğin remaining'ine bakar" kuralı, madde 5'ten
        # ÖNCE de böyleydi, değişmedi).
        summary = totalizer.update({"um1": 140_000, "um2": 140_000, "remaining": 50}, step_dir=0)

        assert summary["total_forward_mm_e1"] == 120.0  # 100 + 20
        assert summary["total_backward_mm_e1"] == 20.0
        assert summary["total_mm_e1"] == 100.0  # NET, 140 DEĞİL
        assert summary["total_mm_e2"] == 100.0
        assert summary["total_mm_average"] == 100.0

    def test_unknown_direction_does_not_credit_either_bucket(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=None)
        summary = totalizer.update({"um1": 10000, "um2": 10000, "remaining": 300}, step_dir=None)
        assert summary["total_forward_mm_e1"] == 0.0
        assert summary["total_backward_mm_e1"] == 0.0
        assert summary["total_mm_e1"] == 0.0

    def test_two_encoders_tracked_independently_then_averaged(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        totalizer.update({"um1": 40_000, "um2": 44_000, "remaining": 400}, step_dir=0)
        summary = totalizer.update({"um1": 40_000, "um2": 44_000, "remaining": 300}, step_dir=1)  # hareketsiz, sadece dir degisti
        # e1=40mm net (sadece ileri), e2=44mm net (sadece ileri) — ortalama 42
        assert summary["total_mm_e1"] == 40.0
        assert summary["total_mm_e2"] == 44.0
        assert summary["total_mm_average"] == 42.0

    def test_day_change_resets_forward_and_backward_totals(self, isolated_feed_total_state, monkeypatch):
        import feed_totalizer as ft_module

        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        totalizer.update({"um1": 30_000, "um2": 30_000, "remaining": 300}, step_dir=1)
        summary_before = totalizer.get_summary()
        assert summary_before["total_backward_mm_e1"] == 30.0

        monkeypatch.setattr(ft_module, "_today_str", lambda: "2099-12-31")
        summary_after = totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        assert summary_after["total_forward_mm_e1"] == 0.0
        assert summary_after["total_backward_mm_e1"] == 0.0
        assert summary_after["date"] == "2099-12-31"

    def test_mutation_check_direction_unaware_sum_would_give_140_not_100(self, isolated_feed_total_state):
        # Bu test DOĞRUDAN mutasyon testi değil (mutasyon manuel olarak
        # geliştirme sırasında yapılıp geri alındı — bkz. rapor), ama eski
        # (yön-körü) davranışın burada YANLIŞ olacağını somut rakamla
        # belgeliyor: forward+backward ham toplamı (140) NET (100) ile
        # KARIŞTIRILMAMALI, ikisi de summary'de ayrı ayrı erişilebilir olmalı.
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 1000}, step_dir=0)
        totalizer.update({"um1": 100_000, "um2": 100_000, "remaining": 500}, step_dir=0)
        totalizer.update({"um1": 120_000, "um2": 120_000, "remaining": 300}, step_dir=1)
        summary = totalizer.update({"um1": 140_000, "um2": 140_000, "remaining": 50}, step_dir=0)
        raw_sum_old_behavior = summary["total_forward_mm_e1"] + summary["total_backward_mm_e1"]
        assert raw_sum_old_behavior == 140.0
        assert summary["total_mm_e1"] == 100.0
        assert raw_sum_old_behavior != summary["total_mm_e1"]

    def test_state_persisted_across_instances(self, isolated_feed_total_state):
        t1 = FeedTotalizer(state_path=isolated_feed_total_state)
        t1.update({"um1": 1000, "um2": 1000, "remaining": 500}, step_dir=0)
        t1.update({"um1": 4000, "um2": 4000, "remaining": 300}, step_dir=0)

        t2 = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = t2.get_summary()
        assert summary["total_mm_e1"] == 3.0
        assert summary["total_mm_e2"] == 3.0


class TestFeedTotalizerSummary:
    def test_mismatch_warning_triggers_above_thresholds(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        # e1 çok fazla ilerler, e2 çok az — hem oran hem mutlak fark eşiği aşılsın
        summary = totalizer.update({"um1": 50000, "um2": 1000, "remaining": 300}, step_dir=0)
        assert summary["mismatch_warning"] is True

    def test_no_mismatch_warning_when_within_tolerance(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        summary = totalizer.update({"um1": 10000, "um2": 10050, "remaining": 300}, step_dir=0)
        assert summary["mismatch_warning"] is False

    def test_average_is_mean_of_two_encoders_net(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        summary = totalizer.update({"um1": 4000, "um2": 2000, "remaining": 300}, step_dir=0)
        assert summary["total_mm_average"] == 3.0

    def test_no_mismatch_warning_when_absolute_diff_below_min_mm(self, isolated_feed_total_state):
        # AND mantığının MISMATCH_MIN_MM bacağı: oransal fark eşiği aşsa
        # bile (diff=4.5 > 0.05*6.5=0.325) mutlak fark 5.0mm eşiğinin
        # ALTINDAYSA uyarı tetiklenmemeli — sadece oran kontrolü yeterli
        # değil, min-mm kapısı da geçilmeli.
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        summary = totalizer.update({"um1": 2000, "um2": 6500, "remaining": 300}, step_dir=0)
        assert summary["total_mm_e1"] == 2.0
        assert summary["total_mm_e2"] == 6.5
        assert summary["mismatch_warning"] is False

    def test_mismatch_warning_based_on_raw_distance_not_net(self, isolated_feed_total_state):
        # e1 hep ileri gidiyor (net=+50), e2 aynı MİKTARDA ama YARI ileri
        # yarı geri gidip net'i küçük çıkıyor (net=+10) — ham mesafe (50 vs
        # 50) eşit olduğu için mismatch OLMAMALI, net'e bakılsaydı (50 vs 10)
        # yanlışlıkla tetiklenirdi. Bu, get_summary() docstring'indeki
        # "mismatch NET değil ham mesafe üzerinden" kararının testi.
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500}, step_dir=0)
        totalizer.update({"um1": 50_000, "um2": 30_000, "remaining": 400}, step_dir=0)  # e1 +50 ileri, e2 +30 ileri
        summary = totalizer.update({"um1": 50_000, "um2": 50_000, "remaining": 50}, step_dir=1)  # e2 +20 geri -> e2 net=10, ham=50
        assert summary["total_mm_e1"] == 50.0
        assert summary["total_mm_e2"] == 10.0
        # ham mesafeler: e1=50 (hep ileri), e2=30+20=50 (ileri+geri) -> eşit -> mismatch YOK
        assert summary["mismatch_warning"] is False
