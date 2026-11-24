"""手動操作の記録をフレーム表へ変換することと、その保存を確かめる。"""
import pytest

from padcue.binfmt import BUTTONS
from padcue.record import Recorder
from tests.helpers import get, post, wait_until

FRAME = 16666667 / 1e9   # 1 フレームの秒数


def rec_with(samples):
    r = Recorder()
    for t, st in samples:
        r.add(t, st)
    return r


def test_converts_to_frame_rows():
    """時刻つきの入力を、フレーム単位の表に変換すること。"""
    a = 1 << BUTTONS["A"]
    r = rec_with([
        (0.0, {"buttons": 0}),
        (0.0 + FRAME * 2, {"buttons": a}),
        (0.0 + FRAME * 5, {"buttons": 0}),
        (0.0 + FRAME * 7, {"buttons": 0}),
    ])
    t = r.to_table()
    assert t["header"] == ["F", "A"]
    # 先頭と末尾の無操作は取り除かれ、A を押していた 3 フレームが残る
    assert [row[1] for row in t["rows"]] == ["1", "1", "1"]
    assert [row[0] for row in t["rows"]] == ["1", "2", "3"]


def test_only_used_columns_are_written():
    """使っていないボタン・軸は、列に出さないこと(表が読みやすくなる)。"""
    b = 1 << BUTTONS["B"]
    r = rec_with([
        (0.0, {"buttons": b, "lx": -1500}),
        (FRAME, {"buttons": b, "lx": -1500}),
        (FRAME * 2, {"buttons": 0, "lx": 0}),
    ])
    assert r.to_table()["header"] == ["F", "B", "LX"]


def test_stick_deadzone_removes_drift():
    """微小なぶれは 0 として扱うこと(手が触れただけの値を記録しない)。"""
    r = rec_with([
        (0.0, {"buttons": 0, "lx": 30}),
        (FRAME, {"buttons": 0, "lx": -20}),
        (FRAME * 2, {"buttons": 0, "lx": 0}),
    ])
    assert r.to_table()["rows"] == []      # 何も操作していない扱い


def test_holds_value_until_next_sample():
    """送信周期がフレームより粗くても、間のフレームは直前の値で埋まること。"""
    x = 1 << BUTTONS["X"]
    r = rec_with([
        (0.0, {"buttons": x}),
        (FRAME * 4, {"buttons": 0}),
    ])
    t = r.to_table()
    assert len(t["rows"]) == 4            # 0..3 フレームは X 押下
    assert all(row[1] == "1" for row in t["rows"])


def test_empty_recording():
    assert Recorder().to_table()["rows"] == []


# ---- GUI 経由 ----

@pytest.fixture
def server(start_gui):
    proj, _devices, base = start_gui("mock00000000", speed=1000.0)
    return base, proj


def test_record_via_gui_creates_frametable(server):
    """画面が取った標本をまとめて送ると、フレーム表になること。"""
    base, proj = server
    assert post(f"{base}/api/record", {"action": "start"})["ok"]
    a = 1 << BUTTONS["A"]
    samples = [{"t": i * FRAME, "buttons": a if 2 <= i <= 4 else 0}
               for i in range(8)]
    assert post(f"{base}/api/record",
                {"action": "add", "samples": samples})["frames"] == 8
    r = post(f"{base}/api/record", {"action": "save", "name": "記録した表"})
    assert r["ok"] and r["frames"] >= 1
    assert "記録した表" in proj.frametable_names()
    # 保存されたフレーム表は手順から使える(検証を通っている)
    table = proj.load_frametable_grid("記録した表")
    assert "A" in table["header"]


def test_a_recording_does_not_silently_replace_a_frame_table(server):
    """同じ名前のフレーム表を黙って上書きせず、上書きしてよいと確認したときだけ
    書くこと。"""
    base, proj = server
    a = 1 << BUTTONS["A"]
    samples = [{"t": i * FRAME, "buttons": a if 2 <= i <= 4 else 0}
               for i in range(8)]
    post(f"{base}/api/record", {"action": "start"})
    post(f"{base}/api/record", {"action": "add", "samples": samples})
    assert post(f"{base}/api/record",
                {"action": "save", "name": "記録した表"})["ok"]
    before = proj.frametable_path("記録した表").read_bytes()
    post(f"{base}/api/record", {"action": "start"})
    post(f"{base}/api/record", {"action": "add", "samples": samples[:4]})
    r = post(f"{base}/api/record", {"action": "save", "name": "記録した表"})
    assert r.get("exists") and "既にあります" in r["error"], r
    assert proj.frametable_path("記録した表").read_bytes() == before
    r = post(f"{base}/api/record",
             {"action": "save", "name": "記録した表", "overwrite": True})
    assert r.get("ok"), r
    assert proj.frametable_path("記録した表").read_bytes() != before


def test_a_one_frame_press_survives_at_sixty_hertz(server):
    """1 フレームだけ押した操作が、60 ヘルツの標本なら必ず残ること。

    標本を画面の側で取るのはこのためである。送信の経路(約 30 ヘルツ、33 ミリ秒
    ごと)で取ると、16.7 ミリ秒の押しがおよそ半分の確率で消える。
    """
    base, _proj = server
    post(f"{base}/api/record", {"action": "start"})
    a = 1 << BUTTONS["A"]
    # 60 ヘルツ = 1 フレームに 1 回。3 フレーム目だけ押す
    samples = [{"t": i * FRAME, "buttons": a if i == 3 else 0}
               for i in range(8)]
    post(f"{base}/api/record", {"action": "add", "samples": samples})
    r = post(f"{base}/api/record", {"action": "pause"})
    assert r["frames"] == 1, "1 フレームの押しが残っていない"


def test_the_deadzone_can_be_chosen(server):
    """記録のデッドゾーンを入力欄で選べること(0 と 500 で確かめる)。"""
    base, _proj = server
    assert post(f"{base}/api/record", {"action": "start", "deadzone": 0})["ok"]
    # デッドゾーンが 0 なら、小さなぶれもそのまま残る
    samples = [{"t": i * FRAME, "buttons": 0, "lx": 30 if i else 0}
               for i in range(4)]
    post(f"{base}/api/record", {"action": "add", "samples": samples})
    assert post(f"{base}/api/record", {"action": "pause"})["frames"] >= 1
    for bad in (-1, 501, "いくつか"):
        r = post(f"{base}/api/record", {"action": "start", "deadzone": bad})
        assert "error" in r, f"{bad} を受け付けている"


def test_the_motion_axes_are_recorded(server):
    """ジャイロと加速度も記録に残ること(装置は手動操作でも受け取る)。"""
    base, proj = server
    post(f"{base}/api/record", {"action": "start"})
    samples = [{"t": i * FRAME, "buttons": 0,
                "gx": 500 if i == 2 else 0, "az": 4096}
               for i in range(5)]
    post(f"{base}/api/record", {"action": "add", "samples": samples})
    assert post(f"{base}/api/record",
                {"action": "save", "name": "ジャイロ記録"})["ok"]
    header = proj.load_frametable_grid("ジャイロ記録")["header"]
    assert "GP" in header, f"ジャイロの列がない: {header}"
    # 静止のままの軸は列に出さない(加速度は静止でも 4096 が出続ける)
    assert "AZ" not in header, f"動いていない軸まで列にしている: {header}"


def test_record_save_without_data_is_reported(server):
    base, _proj = server
    post(f"{base}/api/record", {"action": "start"})
    r = post(f"{base}/api/record", {"action": "save", "name": "空"})
    assert r.get("error") == "記録された操作がありません", r


def test_a_screen_whose_recording_is_gone_is_told_so(server):
    """記録した画面が送る要求に記録がなければ、追加・停止・保存のどれも、記録が
    ないこと(missing)を返すこと。

    記録はサーバーにだけあり、padcue を起動し直すとなくなる。画面は毎秒の状態で
    それに気づくが、その前に押した保存に「記録された操作がありません」と返すと、
    原因が分からない。
    """
    base, _proj = server
    url = f"{base}/api/record"
    post(url, {"action": "stop", "owner": "画面A"})
    for body in ({"action": "add", "samples": []}, {"action": "pause"},
                 {"action": "save", "name": "表"}):
        got = post(url, dict(body, owner="画面A"))
        assert got.get("missing") is True, (body, got)


def test_two_screens_do_not_mix_their_recordings(server):
    """別のタブが記録している間は、記録を始められないこと。

    記録はサーバーに 1 つだけである。始められると、2 つの画面の操作が 1 つの記録に
    混ざり、先に記録していた画面は「記録中」のまま何も残らない。
    """
    base, _proj = server
    url = f"{base}/api/record"
    assert post(url, {"action": "start", "owner": "画面A"})["ok"]
    got = post(url, {"action": "start", "owner": "画面B"})
    assert got.get("error") == "別のタブで記録中です", got
    # 閉じる画面が送る「捨てる」は、別のタブの記録を捨てない
    post(url, {"action": "stop", "owner": "画面B"})
    assert get(base, "/api/state")["recording"]["owner"] == "画面A"
    # 状態にも、どの画面が記録しているかが出る(画面はボタンを押せなくする)
    st = get(base, "/api/state")
    want = {"owner": "画面A", "active": True, "frames": 0}
    assert st["recording"] == want, st["recording"]
    # 停止した後なら始められる。前の画面の記録は、もう保存できないと返す
    post(url, {"action": "add", "owner": "画面A",
               "samples": [{"t": i * FRAME, "buttons": 0} for i in range(4)]})
    post(url, {"action": "pause", "owner": "画面A"})
    assert post(url, {"action": "start", "owner": "画面B"})["ok"]
    got = post(url, {"action": "save", "owner": "画面A", "name": "前の記録"})
    assert "別のタブで新しく記録を開始した" in got.get("error", ""), got


def test_ending_the_manual_operation_stops_its_recording(server):
    """記録している装置の手動操作が終わったら、記録も停止した扱いになること。

    記録している画面を閉じたり読み込み直したりすると、その画面はもう記録を
    停止できない。手動操作は見出しの「終了」でどの画面からでも終えられるので、
    それで記録も停止すれば、別のタブが新しく記録を始められる。
    """
    base, _proj = server
    url = f"{base}/api/record"
    pt = f"{base}/api/passthrough"
    assert post(pt, {"enable": True, "dev": "1P", "session": "s1",
                     "buttons": 0}).get("ok")
    assert post(url, {"action": "start", "owner": "画面A", "dev": "1P"})["ok"]
    a = 1 << BUTTONS["A"]
    post(url, {"action": "add", "owner": "画面A",
               "samples": [{"t": i * FRAME, "buttons": a if 1 <= i <= 2 else 0}
                           for i in range(4)]})
    # 別のタブ(見出しの「終了」)が 1P の手動操作を終える
    assert post(pt, {"enable": False, "dev": "1P"}).get("ok")
    st = get(base, "/api/state")
    # 停止した記録は、残ったフレーム数を含める(捨てる前に確認するため)
    want = {"owner": "画面A", "active": False, "frames": 2}
    assert st["recording"] == want, st["recording"]
    # 記録した画面が送り残した分は、停止した後でも受ける
    post(url, {"action": "add", "owner": "画面A",
               "samples": [{"t": i * FRAME, "buttons": a if i <= 5 else 0}
                           for i in range(4, 8)]})
    assert post(url, {"action": "pause", "owner": "画面A"})["frames"] == 5
    # 別のタブの分は、停止した記録に追加しない
    post(url, {"action": "add", "owner": "画面B",
               "samples": [{"t": 9 * FRAME, "buttons": a}]})
    assert post(url, {"action": "pause", "owner": "画面A"})["frames"] == 5
    # 別のタブは、新しく記録を始められる
    assert post(url, {"action": "start", "owner": "画面B", "dev": "1P"})["ok"]


def test_a_closing_screen_does_not_end_anothers_manual_operation(server):
    """閉じる画面の手動操作の終了(セッション ID つき)は、別のタブの手動操作を
    終えないこと。

    裏で動いていなかった画面が閉じるとき、別のタブが同じ装置で始め直した手動操作
    まで終えると、押していない人の手動操作が切れる。セッション ID のない終了(見出しの
    「終了」)は、必ず終える。
    """
    base, _proj = server
    pt = f"{base}/api/passthrough"

    def manual():
            return get(base, "/api/state")["devices"][0].get("manual")

    assert post(pt, {"enable": True, "dev": "1P", "session": "画面B",
                     "buttons": 0}).get("ok")
    # 始めたことは次の収集で状態に含まれる(終えたことは、その場で書き戻す)
    assert wait_until(manual, 5) is True
    # 前に 1P を操作していた画面 A が閉じる
    assert post(pt, {"enable": False, "dev": "1P", "session": "画面A"}).get("ok")
    assert manual() is True, "別のタブの手動操作まで終えた"
    # 見出しの「終了」(セッション ID なし)は終える
    assert post(pt, {"enable": False, "dev": "1P"}).get("ok")
    assert manual() is False


# ---- 記録できる長さ ----

def test_a_recording_stops_growing_at_the_frame_table_limit(tmp_path):
    """記録は、フレーム表の行数の上限で停止し、そこまでは保存できること。

    上限を見ずに溜めると、約 27 分を超えた記録は保存のときに初めて拒否され、
    一部も保存できない。
    """
    from padcue.frametable import MAX_ROWS
    from padcue.project import Project

    a = 1 << BUTTONS["A"]
    r = Recorder()
    assert r.add(0.0, {"buttons": a})
    assert r.add((MAX_ROWS - 1) * FRAME, {"buttons": 0})
    assert not r.add(MAX_ROWS * FRAME, {"buttons": a}), "上限を超えて記録した"
    table = r.to_table(trim=False)
    assert len(table["rows"]) == MAX_ROWS
    Project(tmp_path).save_frametable_grid("長い記録", table["header"], table["rows"])


def test_the_screen_is_told_once_the_recording_is_full(server):
    """上限に達したら、画面にそのことを示し、途中が抜けたように見せないこと。

    画面は full を見て、メッセージを 1 度だけ出す(manual.js)。
    """
    from padcue.frametable import MAX_ROWS

    base, _proj = server
    assert post(f"{base}/api/record", {"action": "start"})["ok"]
    r = post(f"{base}/api/record", {"action": "add", "samples": [
        {"t": 0.0, "buttons": 0}, {"t": MAX_ROWS * FRAME, "buttons": 0}]})
    assert r.get("full") is True and r["frames"] == 1, r
    assert "約 27 分" in r["error"], r
