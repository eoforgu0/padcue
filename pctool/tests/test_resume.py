"""部分実行(手順の途中から試す)のテスト。

ラベルが再開点になる。再開点には全状態スナップショットが置かれるので、
そこから始めても入力状態が確定する(押しっぱなしの引き継ぎ漏れが起きない)。
"""
import json
import subprocess

import pytest

from switchctl import binfmt, engine
from switchctl.client import DeviceClient, proc_hash
from switchctl.mockdevice import MockDevice
from switchctl.project import Project
from tests.test_hostc import CORE, find_gcc, host_exe, run_c  # noqa: F401

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
ZL = 1 << binfmt.BUTTONS["ZL"]

FLOW = {
    "schema": 1, "name": "三段階", "body": [
        {"type": "label", "text": "前半"},
        {"type": "hold", "buttons": ["ZL"]},
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 55},
        {"type": "label", "text": "後半"},
        {"type": "press", "buttons": ["B"], "frames": 5},
        {"type": "wait", "frames": 55},
    ],
}


@pytest.fixture
def proj(tmp_path):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir()
    (tmp_path / "parts").mkdir()
    (tmp_path / "procedures" / "三段階.flow.json").write_text(
        json.dumps(FLOW, ensure_ascii=False), encoding="utf-8")
    return p


LOOP_FLOW = {
    "schema": 1, "name": "ループ前ラベル", "body": [
        {"type": "label", "text": "導入"},
        {"type": "press", "buttons": ["A"], "frames": 5},
        {"type": "wait", "frames": 55},
        {"type": "label", "text": "本題"},
        {"type": "loop", "count": 3, "body": [
            {"type": "press", "buttons": ["B"], "frames": 5},
            {"type": "wait", "frames": 25},
        ]},
        {"type": "wait", "frames": 30},
    ],
}


def test_labels_become_resume_points(proj):
    r = proj.build("三段階")
    names = [p["name"] for p in r.resume_points]
    assert names == ["先頭", "前半", "後半"]
    for p in r.resume_points:
        assert engine.resume_is_valid(r_ev(r), p["index"])


def r_ev(r):
    return binfmt.decode(r.blob)[1]


def test_resume_keeps_held_state(proj):
    """後半から始めても、前半で押した ZL が保たれること。

    かつ「すぐ動き出す」こと(飛ばした 60 フレームぶん待たされない)。
    """
    r = proj.build("三段階")
    pt = next(p for p in r.resume_points if p["name"] == "後半")
    events = r_ev(r)
    ems = engine.run(events, r.total_frames, 1,
                     start_index=pt["index"], start_base=pt["base"])
    # 再開点のスナップショット(ZL 保持)と直後の press B は同一フレームなので統合される
    assert [(e.frame, e.buttons) for e in ems] == [(0, ZL | B), (5, ZL)]


def test_resume_repeats_only_the_section(proj):
    """周回数を増やしたときも、飛ばした前半ぶんの空白が入らないこと。"""
    r = proj.build("三段階")
    pt = next(p for p in r.resume_points if p["name"] == "後半")
    ems = engine.run(r_ev(r), r.total_frames, 3,
                     start_index=pt["index"], start_base=pt["base"])
    # 後半は 60 フレームぶん。0/60/120 から始まる 3 区間になる
    assert [(e.frame, e.buttons) for e in ems] == [
        (0, ZL | B), (5, ZL), (60, ZL | B), (65, ZL), (120, ZL | B), (125, ZL)]


def test_resume_at_label_before_loop(proj, tmp_path):
    """くり返しの直前にあるラベルからも再開できること。

    この位置の再開点はカウンタ初期化(SETCNT)を指す。時間を消費しないので
    「全状態スナップショットから始まる」保証は崩れない。
    """
    (tmp_path / "procedures" / "ループ前ラベル.flow.json").write_text(
        json.dumps(LOOP_FLOW, ensure_ascii=False), encoding="utf-8")
    r = proj.build("ループ前ラベル")
    pt = next(p for p in r.resume_points if p["name"] == "本題")
    events = r_ev(r)
    assert isinstance(events[pt["index"]], binfmt.SetCnt)   # 前提の確認
    assert engine.resume_is_valid(events, pt["index"])
    ems = engine.run(events, r.total_frames, 1,
                     start_index=pt["index"], start_base=pt["base"])
    # ループ 3 回ぶんが即座に始まる(30 フレーム間隔)
    assert [(e.frame, e.buttons) for e in ems][:4] == [
        (0, B), (5, 0), (30, B), (35, 0)]


def test_resume_at_label_before_loop_accepted_by_device(proj, tmp_path):
    (tmp_path / "procedures" / "ループ前ラベル.flow.json").write_text(
        json.dumps(LOOP_FLOW, ensure_ascii=False), encoding="utf-8")
    r = proj.build("ループ前ラベル")
    pt = next(p for p in r.resume_points if p["name"] == "本題")
    with MockDevice(speed=2.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        cli.put(r.name, r.blob)
        cli.commit(r.name)
        cli.run(r.name, proc_hash(r.blob), loop_n=1,
                resume={"index": pt["index"], "base": pt["base"]})
        assert cli.status()["running"] is True


def test_c_engine_matches_on_resume_before_loop(proj, host_exe, tmp_path):
    """SETCNT を指す再開点でも C 実装が同じ送出列を出すこと。"""
    (tmp_path / "procedures" / "ループ前ラベル.flow.json").write_text(
        json.dumps(LOOP_FLOW, ensure_ascii=False), encoding="utf-8")
    r = proj.build("ループ前ラベル")
    pt = next(p for p in r.resume_points if p["name"] == "本題")
    expected = [(e.frame, e.buttons) for e in
                engine.run(r_ev(r), r.total_frames, 2,
                           start_index=pt["index"], start_base=pt["base"])]
    p = tmp_path / "loop.bin"
    p.write_bytes(r.blob)
    res = subprocess.run(
        [str(host_exe), str(p), "2", "100000", str(pt["index"]), str(pt["base"])],
        capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    got = [(int(l.split()[0]), int(l.split()[1]))
           for l in res.stdout.strip().splitlines()[:-1]]
    assert got == expected


def test_c_engine_matches_on_resume(proj, host_exe, tmp_path):
    """C 実装も同じ位置から同じ送出列を出すこと。"""
    r = proj.build("三段階")
    pt = next(p for p in r.resume_points if p["name"] == "後半")
    events = r_ev(r)
    expected = [(e.frame, e.buttons) for e in
                engine.run(events, r.total_frames, 1,
                           start_index=pt["index"], start_base=pt["base"])]
    assert expected[0][0] == 0     # すぐ動き出す(両実装で一致すべき点)
    p = tmp_path / "proc.bin"
    p.write_bytes(r.blob)
    res = subprocess.run(
        [str(host_exe), str(p), "1", "100000",
         str(pt["index"]), str(pt["base"])],
        capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    got = [(int(l.split()[0]), int(l.split()[1]))
           for l in res.stdout.strip().splitlines()[:-1]]
    assert got == expected


def test_run_from_resume_point_via_device(proj):
    r = proj.build("三段階")
    # 早送りしすぎると開始直後に走り終わってしまう(区間は 60 フレームしかない)
    with MockDevice(speed=2.0) as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        cli.put(r.name, r.blob)
        cli.commit(r.name)
        pt = next(p for p in r.resume_points if p["name"] == "後半")
        cli.run(r.name, proc_hash(r.blob), loop_n=1,
                resume={"index": pt["index"], "base": pt["base"]})
        assert cli.status()["running"] is True


def test_invalid_resume_point_is_rejected(proj):
    r = proj.build("三段階")
    with MockDevice() as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        cli.put(r.name, r.blob)
        cli.commit(r.name)
        from switchctl.client import DeviceError
        with pytest.raises(DeviceError, match="再開点"):
            cli.run(r.name, proc_hash(r.blob), resume={"index": 9999, "base": 0})
        # END を指すのも不正(全状態スナップショットではないため)
        last = len(r_ev(r)) - 1
        with pytest.raises(DeviceError, match="再開点"):
            cli.run(r.name, proc_hash(r.blob), resume={"index": last, "base": 0})
