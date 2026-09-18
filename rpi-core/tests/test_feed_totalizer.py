"""
FeedVision — feed_totalizer.py testleri.

Gerçek feed_total_state.json'a dokunmamak için her testte
`isolated_feed_total_state` fixture'ı (conftest.py) kullanılır — bu
STATE_PATH'i tmp_path'e yönlendirir. FeedTotalizer ayrıca kendi
state_path parametresini de kabul ettiği için, testlerde doğrudan bu
tmp path ile örnek oluşturuyoruz (modül-seviyesi `totalizer` tekiline
dokunmadan).
"""

import json

from feed_totalizer import FeedTotalizer, _default_state, _load_state, _save_state


# ---------------------------------------------------------------------------
# _load_state / _save_state
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_no_file_returns_default(self, isolated_feed_total_state):
        state = _load_state(isolated_feed_total_state)
        assert state["total_mm_e1"] == 0.0
        assert state["prev_um1"] is None

    def test_corrupt_json_returns_default(self, isolated_feed_total_state):
        isolated_feed_total_state.write_text("{bozuk json", encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state == _default_state(state["date"])

    def test_different_date_resets(self, isolated_feed_total_state):
        stale = {
            "date": "2020-01-01",
            "total_mm_e1": 42.0,
            "total_mm_e2": 40.0,
            "prev_um1": 1000,
            "prev_um2": 1000,
        }
        isolated_feed_total_state.write_text(json.dumps(stale), encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state["total_mm_e1"] == 0.0
        assert state["date"] != "2020-01-01"

    def test_missing_field_returns_default(self, isolated_feed_total_state):
        # "prev_um2" eksik — beklenmedik/eski şema
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        partial = {"date": today, "total_mm_e1": 5.0, "prev_um1": None}
        isolated_feed_total_state.write_text(json.dumps(partial), encoding="utf-8")
        state = _load_state(isolated_feed_total_state)
        assert state["total_mm_e1"] == 0.0

    def test_save_then_load_roundtrip(self, isolated_feed_total_state):
        state = _default_state("2099-01-01")
        state["total_mm_e1"] = 12.5
        _save_state(state, isolated_feed_total_state)
        assert isolated_feed_total_state.exists()
        reloaded = json.loads(isolated_feed_total_state.read_text(encoding="utf-8"))
        assert reloaded["total_mm_e1"] == 12.5


# ---------------------------------------------------------------------------
# FeedTotalizer.update / get_summary
# ---------------------------------------------------------------------------

class TestFeedTotalizerUpdate:
    def test_first_sample_no_addition_only_sets_prev(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = totalizer.update({"um1": 1000, "um2": 1000, "remaining": 500})
        # ilk örnekte prev yoktu, toplama eklenmez
        assert summary["total_mm_e1"] == 0.0
        assert summary["total_mm_e2"] == 0.0

    def test_moving_adds_positive_delta(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 1000, "um2": 1000, "remaining": 500})
        summary = totalizer.update({"um1": 3000, "um2": 2500, "remaining": 300})
        assert summary["total_mm_e1"] == 2.0  # (3000-1000)/1000
        assert summary["total_mm_e2"] == 1.5  # (2500-1000)/1000

    def test_not_moving_does_not_add_but_updates_prev(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 1000, "um2": 1000, "remaining": 500})
        # remaining=0 iken buyuk bir sicrama gelse de eklenmemeli
        summary = totalizer.update({"um1": 9000, "um2": 9000, "remaining": 0})
        assert summary["total_mm_e1"] == 0.0
        assert summary["total_mm_e2"] == 0.0
        # ama referans güncellenmiş olmalı — sonraki hareketli örnekte sahte
        # sıçrama oluşmasın
        summary2 = totalizer.update({"um1": 9500, "um2": 9200, "remaining": 100})
        assert summary2["total_mm_e1"] == 0.5
        assert summary2["total_mm_e2"] == 0.2

    def test_negative_delta_not_added(self, isolated_feed_total_state):
        # encoder resetlendi/geri döndü senaryosu — negatif fark sayılmaz
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 5000, "um2": 5000, "remaining": 500})
        summary = totalizer.update({"um1": 1000, "um2": 5000, "remaining": 300})
        assert summary["total_mm_e1"] == 0.0
        assert summary["total_mm_e2"] == 0.0

    def test_missing_um_fields_skipped_silently(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = totalizer.update({"remaining": 500})
        assert summary["total_mm_e1"] == 0.0
        assert summary["date"]

    def test_state_persisted_across_instances(self, isolated_feed_total_state):
        t1 = FeedTotalizer(state_path=isolated_feed_total_state)
        t1.update({"um1": 1000, "um2": 1000, "remaining": 500})
        t1.update({"um1": 4000, "um2": 4000, "remaining": 300})

        t2 = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = t2.get_summary()
        assert summary["total_mm_e1"] == 3.0
        assert summary["total_mm_e2"] == 3.0

    def test_empty_status_dict_does_not_crash(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        summary = totalizer.update({})
        assert summary["total_mm_e1"] == 0.0


class TestFeedTotalizerSummary:
    def test_mismatch_warning_triggers_above_thresholds(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500})
        # e1 çok fazla ilerler, e2 çok az — hem oran hem mutlak fark eşiği aşılsın
        summary = totalizer.update({"um1": 50000, "um2": 1000, "remaining": 300})
        assert summary["mismatch_warning"] is True

    def test_no_mismatch_warning_when_within_tolerance(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500})
        summary = totalizer.update({"um1": 10000, "um2": 10050, "remaining": 300})
        assert summary["mismatch_warning"] is False

    def test_average_is_mean_of_two_encoders(self, isolated_feed_total_state):
        totalizer = FeedTotalizer(state_path=isolated_feed_total_state)
        totalizer.update({"um1": 0, "um2": 0, "remaining": 500})
        summary = totalizer.update({"um1": 4000, "um2": 2000, "remaining": 300})
        assert summary["total_mm_average"] == 3.0
