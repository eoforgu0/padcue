"""通し検証: 手順を書いてから Switch が受け取るバイト列まで、実機なしで確認する。

    手順のファイルとフレーム表のファイル
      -> コンパイル(PC)
      -> バイナリ転送(模擬装置)
      -> 実行エンジン(実機と同じ C 実装)が出力列を生成
      -> Pro コントローラー互換転送層(実機と同じ C 実装)が USB レポートに変換
      -> 模擬 Switch がレポートを解釈し、意図した操作になっているか照合

この経路が通れば「書き込めば自動操作できる」状態が実機なしで実証できる。
"""
import json

import pytest

from padcue import binfmt
from padcue.client import DeviceClient, proc_hash
from padcue.mockdevice import MockDevice
from padcue.procfmt import FORMAT_VERSION
from padcue.project import Project
from tests.helpers import Device, own, run_c, run_handshake
from tests.switchsim import parse_input_report

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
ZL = 1 << binfmt.BUTTONS["ZL"]

# 「ZL を押し続け、同時押しのフレーム表を 3 回くり返し、最後に離す」。表の間は表の
# とおりに入力し(書いていない ZL も離れる)、表を抜けると ZL に戻る
PROCEDURE = {
    "kind": "procedure",
    "format": FORMAT_VERSION,
    "precondition": "ホーム画面",
    "body": [
        {"type": "label", "id": "lb-1", "name": "準備"},
        {"type": "hold", "buttons": ["ZL"]},
        {"type": "wait", "frames": 10},
        {"type": "label", "id": "lb-2", "name": "連打"},
        {"type": "loop", "id": "lp-1", "count": 3, "body": [
            {"type": "frametable", "ref": "同時押し"},
            {"type": "wait", "frames": 12},
        ]},
        {"type": "label", "id": "lb-3", "name": "解除"},
        {"type": "release", "buttons": ["ZL"]},
        {"type": "wait", "frames": 30},
    ],
}
# 1 行=1 フレーム。A を 3F 押し、途中で B とスティック左・ジャイロ上を重ねる
FRAMETABLE_CSV = """F,A,B,LX,GP
1,1,,,
2,1,1,-1200,300
3,1,1,-1200,300
4,,,,
"""


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir()
    (tmp_path / "frametables").mkdir()
    (tmp_path / "procedures" / "通し検証.json").write_text(
        json.dumps(PROCEDURE, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "frametables" / "同時押し.csv").write_text(FRAMETABLE_CSV,
                                                      encoding="utf-8")
    return p


def test_full_chain_from_procedure_to_usb_reports(project, core_host_exe,
                                                  procon_host_exe, tmp_path):
    # ---- 1. コンパイル ----
    r = project.build("通し検証")
    assert r.warnings == []
    # 準備 10 + (4+12)×3 + 解除後 30 = 88 フレーム
    assert r.total_frames == 88
    assert [lb["text"] for lb in r.labels] == ["準備", "連打", "解除"]

    # ---- 2. 転送(模擬装置経由。実機と同じプロトコル) ----
    with MockDevice() as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        own(cli)
        assert cli.put(r.blob) == proc_hash(r.blob)
        cli.commit(r.hash)
        stored = {e["id"]: e for e in cli.list()}
        assert stored[r.hash]["size"] == len(r.blob)

    # ---- 3. 実機と同じ C 実行エンジンで出力列を生成 ----
    emissions = run_c(core_host_exe, r.blob, tmp_path)
    # (frame, buttons, lx, ly, rx, ry, gx, gy, gz, ax, ay, az)
    got = [(e[0], e[1], e[2], e[6]) for e in emissions]
    assert got == [
        (0, ZL, 0, 0),                 # 準備: ZL を押しっぱなし
        (10, ZL, 0, 0),                # ラベル「連打」の区切りの STATE
        (10, A, 0, 0),                 # 同時押し 1 行目(表にない ZL は離す)
        (11, A | B, -1200, 300),       # 2 行目: B・スティック左・ジャイロ上
        (13, 0, 0, 0),                 # 4 行目: 何も押さない
        (14, ZL, 0, 0),                # 表を抜けて ZL に戻る
        (26, A, 0, 0),                 # 2 回目(ブロックの正確な再生)
        (27, A | B, -1200, 300),
        (29, 0, 0, 0),
        (30, ZL, 0, 0),
        (42, A, 0, 0),                 # 3 回目
        (43, A | B, -1200, 300),
        (45, 0, 0, 0),
        (46, ZL, 0, 0),
        (58, 0, 0, 0),                 # 解除
        (88, 0, 0, 0),                 # 最後の周の終わり(区切り)
    ]

    # ---- 4. 実機と同じ転送層で USB レポートへ変換し、模擬 Switch が解釈する ----
    device = Device(procon_host_exe)
    try:
        run_handshake(device)   # Switch 側の初期化シーケンスを再生
        seen = []
        for frame, buttons, lx, _ly, _rx, _ry, gx, _gy, _gz, _ax, _ay, _az in emissions:
            device.set_state(buttons=buttons, lx=lx, gx=gx)
            rep = parse_input_report(device.input_report())
            seen.append((frame, rep["buttons"], rep["left"][0],
                         rep["imu"][0]["gyro"][0]))
    finally:
        device.close()

    # Switch が受け取る形(レポートのボタン 3 バイト、スティックはワイヤ形式 0..4095)
    assert seen == [
        (0, (0x00, 0x80, 0x80), 2048, 0),          # ZL は左バイトの 0x80
        (10, (0x00, 0x80, 0x80), 2048, 0),         # ラベルの区切りの STATE
        (10, (0x08, 0x80, 0x00), 2048, 0),         # A(右バイト 0x08)。ZL は離れる
        (11, (0x0C, 0x80, 0x00), 848, 300),        # + B(0x04)、-1200 → 848
        (13, (0x00, 0x80, 0x00), 2048, 0),
        (14, (0x00, 0x80, 0x80), 2048, 0),         # ZL に戻る
        (26, (0x08, 0x80, 0x00), 2048, 0),
        (27, (0x0C, 0x80, 0x00), 848, 300),
        (29, (0x00, 0x80, 0x00), 2048, 0),
        (30, (0x00, 0x80, 0x80), 2048, 0),
        (42, (0x08, 0x80, 0x00), 2048, 0),
        (43, (0x0C, 0x80, 0x00), 848, 300),
        (45, (0x00, 0x80, 0x00), 2048, 0),
        (46, (0x00, 0x80, 0x80), 2048, 0),
        (58, (0x00, 0x80, 0x00), 2048, 0),         # すべて解放(給電ビットは残る)
        (88, (0x00, 0x80, 0x00), 2048, 0),         # 最後の周の終わり
    ]


def test_raw_stick_resolution_survives_the_whole_chain(project, core_host_exe,
                                                       procon_host_exe, tmp_path):
    """生値の +1 の差が、コンパイル〜USB レポートまで失われずに送られること。

    精密な検証(最小の単位の入力差を見る用途)の前提。
    """
    values = [-2048, -2047, -1, 0, 1, 2046, 2047]
    body = []
    for v in values:
        body.append({"type": "stick", "side": "L", "x": v, "y": 0})
        body.append({"type": "wait", "frames": 10})
    (project.root / "procedures" / "刻み.json").write_text(
        json.dumps({"kind": "procedure", "format": FORMAT_VERSION,
                    "body": body}, ensure_ascii=False), encoding="utf-8")

    r = project.build("刻み")
    emissions = run_c(core_host_exe, r.blob, tmp_path)
    # 末尾には最後の周の終わりの STATE が 1 つ付く。押し続けているものは
    # くり返しの境界を越えないので(concepts.md §4)、入る直前の状態(中央)に戻る
    expected = [*values, 0]
    assert [e[2] for e in emissions] == expected   # C エンジンまでは符号付き生値

    device = Device(procon_host_exe)
    try:
        run_handshake(device)
        wire = []
        for e in emissions:
            device.set_state(lx=e[2])
            wire.append(parse_input_report(device.input_report())["left"][0])
    finally:
        device.close()
    # ワイヤ形式は +2048 の 1:1。隣り合う値の差が保たれている
    assert wire == [v + 2048 for v in expected]
    assert wire[1] - wire[0] == 1 and wire[4] - wire[3] == 1
