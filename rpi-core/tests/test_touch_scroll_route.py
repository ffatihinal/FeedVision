"""
FeedVision — /touch_scroll.js route testi. Diğer main.py testleriyle aynı
desen: endpoint düz Python fonksiyonu, TestClient gerekmez.
"""

from pathlib import Path

import main


def test_touch_scroll_js_serves_file_as_javascript():
    resp = main.touch_scroll_js()
    assert resp.status_code == 200
    assert "javascript" in resp.media_type
    expected = (Path(main.__file__).resolve().parent.parent / "ui" / "touch_scroll.js").read_text(encoding="utf-8")
    assert resp.body.decode("utf-8") == expected
