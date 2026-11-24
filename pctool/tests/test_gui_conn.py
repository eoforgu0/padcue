"""GUI サーバーと実機の接続の扱い(接続は装置プールが一元的に持つ)。

手動操作は毎秒 30 回コマンドを送るので、毎回つなぎ直すと接続の開閉だけで時間を
使い切る。装置ごとに 1 本を使い回し、切れたらつなぎ直すこと(利用者には見えない
形で)。状態は収集スレッドのキャッシュの値をすぐに返すため、変化の反映は最大 1〜数秒
遅れる(テストは待つ)。
"""
import json
import time
import urllib.error

import pytest

from padcue import gui
from padcue.mockdevice import MockDevice
from tests.helpers import get, post, wait_until


@pytest.fixture
def env(start_gui):
    proj, (dev,), base = start_gui("mock00000000")
    return proj, dev, base


def post_any(base, path, obj):
    """拒否されたとき(4xx・5xx)も、返ってきた中身を読む。"""
    try:
        return post(base, path, obj)
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def device0(base) -> dict:
    """1 台目の装置の状態(画面が読むのと同じ devices[0])。"""
    return (get(base, "/api/state").get("devices") or [{}])[0]


def is_up(base) -> bool:
    """その装置が見えているか(応答があり、Switch にもつながっている)。"""
    return bool(device0(base).get("visible"))


def test_connection_is_reused(env):
    _proj, _dev, base = env
    assert wait_until(lambda: is_up(base)), "装置が見えない"
    link = gui._Handler.pool.links()[0]
    cl = link.client
    assert cl is not None, "接続が使い回されていない"
    time.sleep(2.5)                     # 収集 2〜3 回ぶん
    get(base, "/api/state")
    assert link.client is cl, "毎回つなぎ直している"


def test_reconnects_after_device_restart(env):
    """実機が切れて戻ってきても、次の操作で自動的につなぎ直すこと。"""
    _proj, dev, base = env
    assert wait_until(lambda: is_up(base))
    port = dev.port
    dev.stop()
    assert wait_until(lambda: not is_up(base)), "切断が画面に伝わらない"
    dev2 = MockDevice(speed=2000.0, port=port)
    for _ in range(50):                 # 排他 bind は TIME_WAIT で少し待つ
        try:
            dev2.start()
            break
        except OSError:
            time.sleep(0.1)
    try:
        assert wait_until(lambda: is_up(base)), "つなぎ直せていない"
    finally:
        dev2.stop()


def test_passthrough_burst_is_served(env):
    """手動操作の連射(毎秒 30 回相当)でも取りこぼさないこと。"""
    _proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True}).get("ok")
    for i in range(40):
        r = post(base, "/api/passthrough",
                 {"enable": True, "buttons": i % 2, "lx": i * 10})
        assert r.get("ok"), r
    assert dev.manual is not None
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")
    assert dev.manual is None


def test_an_ended_manual_mode_is_not_restarted_by_the_next_input(env):
    """別のタブで手動操作を終えたら、残った画面の続きの入力で始め直さないこと。

    続きの入力は毎秒 30 回 enable を真にして送る。そのまま装置へ送ると、終えた
    はずの手動操作がすぐ始まり直し、終えた画面にもまた手動操作中と出る。
    残った画面には、手動操作が終わったことを返す。
    """
    _proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True}).get("ok")
    assert post(base, "/api/passthrough",
                {"enable": True, "cont": True, "buttons": 1}).get("ok")
    assert dev.manual is not None
    # 別のタブが終える
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")
    assert dev.manual is None
    r = post(base, "/api/passthrough", {"enable": True, "cont": True, "buttons": 1})
    assert r.get("ended") is True, r
    assert dev.manual is None, "終えた手動操作が、続きの入力で始め直された"
    # 始める操作なら、また始められる
    assert post(base, "/api/passthrough", {"enable": True}).get("ok")
    assert dev.manual is not None
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")


def test_an_old_screen_does_not_cut_into_a_restarted_manual_mode(env):
    """別のタブが始め直した手動操作に、前の手動操作の画面の入力が割り込まないこと。

    終えたフラグだけで判別すると、始め直した時点でフラグがクリアされ、前の画面の遅れた
    入力もまた通る。入力は、始めるたびに画面が付けるセッション ID で判別する。
    """
    _proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True, "session": "A"}).get("ok")
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")
    assert post(base, "/api/passthrough", {"enable": True, "session": "B"}).get("ok")
    assert post(base, "/api/passthrough",
                {"enable": True, "cont": True, "session": "B", "buttons": 2}).get("ok")
    r = post(base, "/api/passthrough",
             {"enable": True, "cont": True, "session": "A", "buttons": 1})
    assert r.get("ended") is True, r
    assert dev.manual and dev.manual.get("buttons") == 2, \
        f"前の画面の入力が割り込んだ: {dev.manual}"
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")


def test_an_end_that_did_not_reach_the_device_is_not_reported_as_ended(env):
    """終える指示を装置に送れなかったら、残った画面へ「終わった」と答えないこと。

    答えると、残った画面は手動操作をやめ、装置は手動操作のまま残る。
    """
    _proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True, "session": "A"}).get("ok")
    # 終える指示だけを送れない(続きの入力は送れる)
    client = gui._Handler.pool.links()[0].client
    real = client.passthrough

    def end_fails(on, **st):
        if not on:
            raise ConnectionError("終える指示を送れない")
        return real(on, **st)
    client.passthrough = end_fails
    got = post_any(base, "/api/passthrough", {"enable": False})
    assert got.get("error"), got
    got = post_any(base, "/api/passthrough",
                   {"enable": True, "cont": True, "session": "A"})
    assert got.get("ended") is not True, got
    assert dev.manual is not None
    client.passthrough = real
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")


def test_continued_input_after_a_restart_keeps_manual_mode(env):
    """操作画面を起動し直した後の続きの入力は、そのまま装置へ送ること。

    起動し直した操作画面は、手動操作を始めたことも終えたことも見ていない。
    終わったと答えると、手動操作中のまま残った画面が「外で終了した」と出して
    操作をやめ、装置は手動操作のまま残る。
    """
    _proj, dev, base = env
    r = post(base, "/api/passthrough", {"enable": True, "cont": True, "buttons": 1})
    assert r.get("ok"), r
    assert dev.manual is not None
    assert post(base, "/api/passthrough", {"enable": False}).get("ok")


def test_a_start_ends_the_manual_mode_of_its_lane_first(env):
    """開始するレーンの装置が手動操作中なら、サーバーが先に手動操作を終えること。

    装置は手動操作のままでも実行を受け付けて実行を優先し、実行が終わると
    手動操作の入力をまた出す。別のタブが始めたばかりの手動操作は、開始を
    押した画面の状態にまだ出ていないので、画面だけでは先に終えられない。
    """
    _proj, dev, base = env
    assert post(base, "/api/passthrough", {"enable": True, "session": "A"}).get("ok")
    assert dev.manual is not None
    # 拒否される開始では、手動操作を終えない
    r = post_any(base, "/api/run", {"plan": [{"name": "ない手順", "laps": 1}]})
    assert r.get("error"), r
    assert dev.manual is not None, "拒否された開始で手動操作が終わった"
    r = post_any(base, "/api/run", {"plan": [{"name": "サンプル", "laps": 1}]})
    assert r.get("ok"), r
    assert dev.manual is None, "手動操作のまま実行が始まった"
    # 手動操作をしていた画面の続きの入力は、終わったと返される
    got = post_any(base, "/api/passthrough",
                   {"enable": True, "cont": True, "session": "A"})
    assert got.get("ended") is True, got
    post_any(base, "/api/stop", {"mode": "immediate"})


def test_host_change_drops_old_connection(env):
    """登録情報の接続先が変わったら、古い接続を捨ててつなぎ直すこと。

    接続先を書き換えるのは、探索の追従(autoscan)とコマンドである。どちらも
    登録情報を書くだけなので、プールは次の状態の取得で読み直して追いかける。
    """
    proj, _dev, base = env
    assert wait_until(lambda: is_up(base))
    proj.update_device(proj.load_config(), 0, host="192.0.2.9")
    assert wait_until(lambda: not is_up(base)), \
        "接続先を変えたのに古い接続を使っている"
    proj.update_device(proj.load_config(), 0, host="127.0.0.1")
    assert wait_until(lambda: is_up(base))
