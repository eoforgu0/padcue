"""LAN を探し、登録情報の装置を追いかける監視。

登録することは「この装置を使う」という意思表示なので、登録情報に加えるのは
人である。探索がするのは次の 2 つだけである。

 - 登録情報にある装置の接続先が変わったら書き直す。一致を見るのは装置 ID だけ
   なので、動く装置が入れ替わることはない
 - 未登録の装置は候補として覚える。画面が「見つかった装置」として並べる

ただし、模擬装置は候補にしない。デモ用の装置が実機に混ざると、どれが本物か
読めない。
"""
from __future__ import annotations

import pytest

from padcue import autoscan
from padcue.autoscan import AutoScan
from padcue.discover import Found
from padcue.mockdevice import FW_VERSION, MockDevice
from padcue.project import Project


class _Pool:
    """登録情報の読み直しを受けるだけのオブジェクト。"""

    def __init__(self):
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


@pytest.fixture
def env(tmp_path):
    proj = Project(tmp_path)
    proj.init_sample()
    # 装置 ID が mock で始まらない装置は、実機として扱われる
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as real, \
            MockDevice(speed=2000.0, device_id="mock00000009") as fake:
        yield proj, real, fake


def found(dev, device_id):
    return Found(host="127.0.0.1", port=dev.port, device_id=device_id,
                 fw=FW_VERSION)


def scan(proj, results, monkeypatch):
    monkeypatch.setattr(autoscan, "discover", lambda *a, **k: list(results))
    pool = _Pool()
    sc = AutoScan(proj, pool, autostart=False)
    return sc.tick(), pool, sc


def test_a_device_that_is_not_in_the_config_becomes_a_candidate(env, monkeypatch):
    """未登録の装置は、候補になるだけで登録情報には含まれないこと。

    見つかった装置を自動で加えると、家に複数台あるときに、どれが動くのか
    分からないまま実行でき、使うのをやめた装置も電源が入っていれば登録情報に
    入って動いてしまう。
    """
    proj, real, _fake = env
    proj.save_config({"devices": []})
    changed, pool, sc = scan(proj, [found(real, "aaaa00000001")], monkeypatch)
    assert changed == [], changed
    assert proj.load_config()["devices"] == [], "人が選んでいないのに載った"
    assert pool.refreshed == 0
    assert sc.candidates == [{"id": "aaaa00000001", "host": "127.0.0.1",
                              "port": real.port, "fw": FW_VERSION}]


def test_a_device_that_stops_answering_leaves_the_candidates(env, monkeypatch):
    """電源を切った装置が、候補に残り続けないこと。"""
    proj, real, _fake = env
    proj.save_config({"devices": []})
    _c, _p, sc = scan(proj, [found(real, "aaaa00000001")], monkeypatch)
    assert sc.candidates
    monkeypatch.setattr(autoscan, "discover", lambda *a, **k: [])
    sc.tick()
    assert sc.candidates == []


def test_a_device_already_in_the_config_is_not_a_candidate(env, monkeypatch):
    """使うと決めた装置は、候補の側には出ないこと。"""
    proj, real, _fake = env
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "127.0.0.1", "port": real.port}]})
    _changed, _pool, sc = scan(proj, [found(real, "aaaa00000001")], monkeypatch)
    assert sc.candidates == []


def test_a_mock_device_is_not_added(env, monkeypatch):
    """模擬装置は、探索の候補にも登録情報にも含めないこと。"""
    proj, _real, fake = env
    proj.save_config({"devices": []})
    changed, _pool, sc = scan(proj, [found(fake, "mock00000009")], monkeypatch)
    assert changed == [], changed
    assert proj.load_config()["devices"] == []
    assert sc.candidates == [], "模擬装置が候補に出た"


def test_a_changed_address_is_followed(env, monkeypatch):
    """接続先が変わったら登録を書き直すこと。人が新しい接続先を探して打ち直さずに済む。"""
    proj, real, _fake = env
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "192.0.2.9", "port": 5555}]})
    changed, pool, _sc = scan(proj, [found(real, "aaaa00000001")], monkeypatch)
    assert changed == ["1P"], changed
    d = proj.load_config()["devices"][0]
    assert d["host"] == "127.0.0.1" and d["port"] == real.port, d
    assert d["name"] == "1P", "名前まで書き換えた"
    assert pool.refreshed == 1


def test_nothing_found_changes_nothing(env, monkeypatch):
    proj, _real, _fake = env
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "192.0.2.9", "port": 5555}]})
    changed, pool, _sc = scan(proj, [], monkeypatch)
    assert changed == [] and pool.refreshed == 0
    assert proj.load_config()["devices"][0]["host"] == "192.0.2.9"


def test_a_different_device_is_not_followed(env, monkeypatch):
    """別の装置が同じアドレスにいても、乗り換えないこと。"""
    proj, real, _fake = env
    proj.save_config({"devices": [{"id": "aaaa00009999", "name": "1P",
                                   "host": "192.0.2.9", "port": 5555}]})
    changed, pool, sc = scan(proj, [found(real, "aaaa00000001")], monkeypatch)
    assert changed == [] and pool.refreshed == 0
    assert proj.load_config()["devices"][0]["host"] == "192.0.2.9"
    # 未登録の装置なので、候補の側に出る
    assert [c["id"] for c in sc.candidates] == ["aaaa00000001"]


def test_an_address_that_does_not_answer_is_not_written(env, monkeypatch):
    """応答はあってもつながらないアドレスに書き換えないこと。

    探索の応答は「受け取った経路のアドレス」で見えるので、PC に仮想アダプターが
    あると、つながらない自分側のアドレスが混じる。確認せずに書き換えると、
    いまつながっているのに未接続に切り替わってしまう。
    """
    proj, real, _fake = env
    proj.save_config({"devices": [{"id": "aaaa00000001", "name": "1P",
                                   "host": "127.0.0.1", "port": real.port}]})
    # 同じ装置が、応答しないアドレスから見えた場合
    ghost = Found(host="192.0.2.9", port=5555, device_id="aaaa00000001",
                  fw=FW_VERSION)
    changed, pool, _sc = scan(proj, [ghost], monkeypatch)
    assert changed == [] and pool.refreshed == 0
    d = proj.load_config()["devices"][0]
    assert d["host"] == "127.0.0.1" and d["port"] == real.port, d
