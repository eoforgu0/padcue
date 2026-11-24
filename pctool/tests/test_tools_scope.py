"""ツール(pctool/tools/)が、渡された物と、装置と、この PC に及ぼす範囲。

ツールは、人が渡した出力フォルダーやプロジェクトで動き、模擬装置や実機につなぐ。
消す物、読む物、起動する物を、ツールが自分で作った物と、確認に必要な物に限る。
"""
import sys
import tempfile
from types import SimpleNamespace

from padcue import condwatch, devicepool
from padcue.client import DeviceClient
from padcue.condwatch import CondWatcher
from padcue.frames import encode_png
from padcue.mockdevice import MockDevice
from padcue.project import Project
from padcue.sources import LANE_VIDEO_NAME
from tests.conftest import REPO
from tests.helpers import compile_nodes, get, gui_server, wait_until

sys.path.insert(0, str(REPO / "pctool" / "tools"))

import _workdir
import accept
import accept_device
import runbook_walk


def test_runbook_walk_rebuilds_only_its_own_folder(tmp_path):
    """runbook_walk は、出力フォルダーの中の自分が作ったフォルダーだけを作り直すこと。
    出力フォルダーのほかの物と、同じ名前でも自分が作った目印のないフォルダーは消さない
    こと。

    出力フォルダーには、uicheck と同じフォルダーや、人の作業のフォルダーが渡されうる。
    """
    keep = tmp_path / "前からある.txt"
    keep.write_text("x", encoding="utf-8")
    work = runbook_walk.prepare_work(tmp_path)
    assert work == tmp_path / runbook_walk.WORK
    assert list(work.iterdir()) == [work / _workdir.MARK]
    (work / "procedures").mkdir()
    assert runbook_walk.prepare_work(tmp_path) == work
    assert not (work / "procedures").exists(), "前回の中身が残っている"
    assert keep.read_text(encoding="utf-8") == "x"

    other = tmp_path / "人のフォルダー"
    theirs = other / runbook_walk.WORK
    theirs.mkdir(parents=True)
    (theirs / "手順.json").write_text("{}", encoding="utf-8")
    assert runbook_walk.prepare_work(other) is None
    assert (theirs / "手順.json").is_file(), "目印のないフォルダーを消した"


def test_accept_does_not_replace_a_folder_it_did_not_make(tmp_path):
    """accept.py は、出力先の _proj が自分の作ったコピーでなければ、消さずに拒否する
    こと。
    """
    src = Project(tmp_path / "src")
    src.init_sample()
    out = tmp_path / "out"
    theirs = out / "_proj"
    theirs.mkdir(parents=True)
    (theirs / "メモ.txt").write_text("x", encoding="utf-8")
    assert accept.main(["accept.py", str(out), str(src.root), "サンプル",
                        "サンプル"]) == 2
    assert (theirs / "メモ.txt").read_text(encoding="utf-8") == "x"


def _device_log_kinds(dev) -> list:
    """装置に残っている記録の種類。受け取りは知らせないので、読んでも消えない。"""
    with DeviceClient("127.0.0.1", dev.port) as c:
        return [e["kind"] for e in c.logs().entries]


def test_the_group_server_of_accept_device_leaves_the_device_logs(tmp_path):
    """accept_device の --two が起動するサーバーは、装置の記録を読まないこと。

    冒頭の約束(記録を読まない。読むと装置から消え、操作画面が集める記録を横取り
    する)のとおりである。通常のサーバーは、状態を読むたびに記録も読んで装置から
    消す。比べるため、同じ装置に通常のサーバーを起動すると消えることも見る。
    """
    proj = Project(tmp_path)
    proj.init_sample()
    with MockDevice(speed=2000.0, device_id="aaaa00000001") as dev:
        proj.save_config({"devices": [{"id": dev.device_id, "name": "1P",
                                       "host": "127.0.0.1", "port": dev.port}]})
        assert _device_log_kinds(dev)[:1] == ["BOOT"]

        with accept_device.server_without_logs(proj) as base:
            def read_at():
                d = get(base, "/api/state")["devices"][0]
                return d.get("visible") and d.get("status_at")
            # 収集は、状態を読んだ後に同じ回で記録を読む。状態を読んだ時刻が
            # 進めば、前の回の記録の読み込みは終わっている
            first = wait_until(read_at)
            assert first, get(base, "/api/state")["devices"]
            assert wait_until(lambda: (read_at() or 0) > first)
            assert _device_log_kinds(dev)[:1] == ["BOOT"], "装置の記録を読んで消した"
        assert devicepool.DeviceLink is not accept_device._LinkWithoutLogs

        with gui_server(proj) as base:
            get(base, "/api/state")          # 装置プールは最初の要求で作られる
            assert wait_until(lambda: not _device_log_kinds(dev)), \
                "通常のサーバーが記録を読まない(比較の対象になっていない)"


def test_the_group_checks_of_accept_device_pass_on_mocks(tmp_path):
    """accept_device の --two の 4 件が模擬装置 2 台で通り、装置の記録が残ること。
    テストの手順は装置に残さないこと。

    --two は実機を 2 台揃えたときにしか実行されないので、項目の作りの誤り(画面の
    状態にない欄を見るなど)は、実機の前で初めて分かる。同じ流れを模擬装置で通す。
    早送りは、開始を確認できる長さ(周回だけの手順が 6 秒)にする。
    """
    with MockDevice(speed=10.0, device_id="aaaa00000001") as d1, \
            MockDevice(speed=10.0, device_id="bbbb00000002") as d2:
        c = accept_device.Checker()
        accept_device.run_group_checks(c, f"127.0.0.1:{d1.port}",
                                       f"127.0.0.1:{d2.port}", tmp_path / "work")
        assert [r[0] for r in c.rows] == ["OK"] * 4, c.rows
        for d in (d1, d2):
            assert "RUN_START" in _device_log_kinds(d), "装置の記録を読んで消した"
            with DeviceClient("127.0.0.1", d.port) as dev:
                assert dev.list() == [], "テストの手順を装置に残した"


def test_accept_starts_no_command_of_the_procedures(tmp_path, monkeypatch):
    """accept.py は、手順のスクリプトのコマンドを起動せず、終了コード 0 で終えたことに
    すること。

    コマンドは別のプロセスになるので、lock_to_mock の安全策が適用されず、コピーの外にも
    書ける。完了まで待つスクリプトのブロック、待たずに進むスクリプトの
    ブロック、選択肢の「スクリプトの結果」(_run_shell)の 3 つの経路を通す。
    """
    # skip_scripts が差し替える condwatch.subprocess を、テストの終わりに元へ戻す
    monkeypatch.setattr(condwatch, "subprocess", condwatch.subprocess)
    skipped = accept.skip_scripts()
    out = tmp_path / "ran.txt"
    cmd = f'"{sys.executable}" -c "open(r\'{out}\', \'w\').close()"'
    proj = Project(tmp_path)
    proj.init_sample()
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = SimpleNamespace(cfg={"id": "aaaa00000001", "name": "1P", "video": None},
                           registered_id="aaaa00000001", status={})
    cw._launch(link, "周回", {"cmd": cmd, "wait": True})
    cw._launch(link, "周回", {"cmd": cmd, "wait": False})
    assert condwatch._run_shell(cmd, {}, 5.0) == 0
    assert not out.exists(), "コマンドを起動した"
    assert skipped.skipped == [cmd] * 3
    ran = [e for e in proj.logs.read(20) if e.get("kind") == "PC_SCRIPT"]
    assert [(e.get("bg"), e.get("exit"), e.get("ok")) for e in ran] == [
        (False, 0, None), (True, None, True)], ran


def test_accept_leaves_no_capture_folder_in_temp(tmp_path, monkeypatch):
    """accept.py は、待たずに進むスクリプトのブロックの取り込みの一時フォルダーを、
    PC の一時フォルダーに残さないこと。

    コマンドを起動しないので、取り込んだ一時フォルダーを読んで消すものがいない。
    50 周の実行では周ごとに作られる。
    """
    # skip_scripts が差し替える condwatch.subprocess を、テストの終わりに元へ戻す
    monkeypatch.setattr(condwatch, "subprocess", condwatch.subprocess)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    skipped = accept.skip_scripts()
    shot = tmp_path / "shot.png"
    shot.write_bytes(encode_png(4, 4, bytes((10, 20, 30)) * 16))
    proj = Project(tmp_path / "proj")
    proj.init_sample()
    cw = CondWatcher(proj, pool=None, autostart=False)
    link = SimpleNamespace(
        cfg={"id": "aaaa00000001", "name": "1P",
             "video": {"source": {"kind": "file", "path": str(shot)}}},
        registered_id="aaaa00000001", status={})
    # 一時フォルダーを作ったことを覚える(作らずに通っていないことを見る)
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(real_mkdtemp(*args, **kwargs))
        return made[-1]
    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    cw._launch(link, "周回", {"cmd": "python collect.py", "wait": False,
                             "capture": [LANE_VIDEO_NAME]})
    assert skipped.skipped == ["python collect.py"], skipped.skipped
    assert made and all(d.startswith(str(temp)) for d in made), made
    assert not list(temp.iterdir()), list(temp.iterdir())


def _branch(arms_when=None, what=None) -> dict:
    """選択肢 1 本と「それ以外」を持つ分岐。"""
    arm = {"name": "甲", "what": what or {"kind": "manual"},
           "body": [{"type": "wait", "frames": 5}]}
    if arms_when:
        arm["when"] = arms_when
    return {"type": "branch", "id": "br-1", "arms": [arm],
            "otherwise": [{"type": "wait", "frames": 5}]}


def test_accept_stops_at_a_mark_only_when_a_second_hand_wait_holds_the_run():
    """accept.py の「区切りで停止」の項目は、人が選ぶ選択待ちに 2 回以上着く手順で
    だけ行うこと(hand_waits)。

    1 つ目を人の代わりに選んだ後、2 つ目を選ばずに置いて停止する。2 つ目がないと、
    早送りの模擬装置は停止する前に最後まで終わり、手順に問題がないのに項目が NG に
    なる。最後の周にしかない選択待ち、条件やスクリプトの結果で進む選択待ちは数えない。
    """
    press = {"type": "press", "buttons": ["A"], "frames": 5}
    every = compile_nodes([press, _branch()], laps=50)
    assert accept.hand_waits(every) == 2
    last = compile_nodes(
        [press, _branch({"loop": "@whole", "kind": "last"})], laps=50)
    assert last.awaits, "最後の周には選択待ちがある"
    assert accept.hand_waits(last) == 1
    scripted = compile_nodes(
        [press, _branch(what={"kind": "script", "cmd": "python x.py"})], laps=50)
    assert accept.hand_waits(scripted) == 0
    assert accept.hand_waits(compile_nodes([press], laps=50)) == 0


def test_accept_expects_the_frames_of_the_arms_it_picks():
    """accept.py が装置の記録と比べる経過フレーム数は、先頭の選択肢を選んで進んだ
    長さであること(expected_frames)。

    変換器の総フレーム数は選択肢を長さ 0 として数えるので、分岐のある手順では
    実行した長さと合わない。条件やスクリプトの結果で進む選択待ちがあれば、どの
    選択肢へ進むか決まらないので None にする。
    """
    press = {"type": "press", "buttons": ["A"], "frames": 5}
    every = compile_nodes([press, _branch()], laps=50)
    assert accept.expected_frames(every) == every.total_frames + 5 * 50
    plain = compile_nodes([press], laps=50)
    assert accept.expected_frames(plain) == plain.total_frames
    scripted = compile_nodes(
        [press, _branch(what={"kind": "script", "cmd": "python x.py"})], laps=50)
    assert accept.expected_frames(scripted) is None
