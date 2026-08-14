"""通し検証: 手順を書いてから Switch が受け取るバイト列まで、実機なしで確認する。

    flow.json + parts.csv
      → コンパイル(PC)
      → バイナリ転送(模擬デバイス)
      → 実行エンジン(**実機と同じ C 実装**)が送出列を生成
      → プロコン互換転送層(**実機と同じ C 実装**)が USB レポートに変換
      → 模擬 Switch がレポートを解釈し、意図した操作になっているか照合

この経路が通れば「書き込めば自動操作できる」状態が実機なしで実証できる。
"""
import json
import subprocess

import pytest

from padcue import binfmt
from padcue.client import DeviceClient, proc_hash
from padcue.mockdevice import MockDevice
from padcue.project import Project
from padcue.switchsim import parse_input_report
from tests.test_hostc import run_c
from tests.test_procon import Device, run_handshake

A = 1 << binfmt.BUTTONS["A"]
B = 1 << binfmt.BUTTONS["B"]
ZL = 1 << binfmt.BUTTONS["ZL"]

# 「ZL を押しっぱなしのまま、コンボ部品を3回くり返し、最後に解放」
FLOW = {
    "schema": 1,
    "name": "通し検証",
    "pre": "拠点前",
    "body": [
        {"type": "label", "text": "構え"},
        {"type": "hold", "buttons": ["ZL"]},
        {"type": "wait", "frames": 10},
        {"type": "label", "text": "連続攻撃"},
        {"type": "loop", "count": 3, "body": [
            {"type": "part", "ref": "コンボ"},
            {"type": "wait", "frames": 12},
        ]},
        {"type": "label", "text": "解除"},
        {"type": "release", "buttons": ["ZL"]},
        {"type": "wait", "frames": 30},
    ],
}
# 1行=1フレーム。A を3F押し、途中で B とスティック左・ジャイロ上を重ねる
PART_CSV = """F,A,B,LX,GP
1,1,,,
2,1,1,-1200,300
3,1,1,-1200,300
4,,,,
"""


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path)
    (tmp_path / "procedures").mkdir()
    (tmp_path / "parts").mkdir()
    (tmp_path / "procedures" / "通し検証.flow.json").write_text(
        json.dumps(FLOW, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "parts" / "コンボ.csv").write_text(PART_CSV, encoding="utf-8")
    return p


def test_full_chain_from_flow_to_usb_reports(project, host_exe, procon_exe, tmp_path):
    # --- 1. コンパイル ---
    r = project.build("通し検証")
    assert r.warnings == []
    # 構え10 + (4+12)×3 + 解除後30 = 88 フレーム
    assert r.total_frames == 88
    assert [l["text"] for l in r.labels] == ["構え", "連続攻撃", "解除"]

    # --- 2. 転送(模擬デバイス経由。実機と同じプロトコル) ---
    with MockDevice() as dev, DeviceClient("127.0.0.1", dev.port) as cli:
        assert cli.put(r.name, r.blob) == proc_hash(r.blob)
        cli.commit(r.name)
        stored = {e["name"]: e for e in cli.list()}
        assert stored[r.name]["hash"] == r.hash

    # --- 3. 実機と同じ C 実行エンジンで送出列を生成 ---
    emissions = run_c(host_exe, r.blob, tmp_path, 1)
    # (frame, buttons, lx, ly, rx, ry, gx, gy, gz, ax, ay, az)
    got = [(e[0], e[1], e[2], e[6]) for e in emissions]
    assert got == [
        (0, ZL, 0, 0),                 # 構え: ZL を押しっぱなし
        (10, ZL | A, 0, 0),            # コンボ1行目
        (11, ZL | A | B, -1200, 300),  # 2行目: B・スティック左・ジャイロ上
        (13, ZL, 0, 0),                # 4行目: すべて離す(ZL は継続)
        (26, ZL | A, 0, 0),            # 2周目(ブロックの正確な再生)
        (27, ZL | A | B, -1200, 300),
        (29, ZL, 0, 0),
        (42, ZL | A, 0, 0),            # 3周目
        (43, ZL | A | B, -1200, 300),
        (45, ZL, 0, 0),
        (58, 0, 0, 0),                 # 解除
    ]

    # --- 4. 実機と同じ転送層で USB レポートへ変換し、模擬 Switch が解釈する ---
    device = Device(procon_exe)
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

    # Switch が受け取る形(レポートのボタン3バイト、スティックはワイヤ形式 0..4095)
    assert seen == [
        (0, (0x00, 0x80, 0x80), 2048, 0),          # ZL は左バイトの 0x80
        (10, (0x08, 0x80, 0x80), 2048, 0),         # + A(右バイト 0x08)
        (11, (0x0C, 0x80, 0x80), 848, 300),        # + B(0x04)、-1200 → 848
        (13, (0x00, 0x80, 0x80), 2048, 0),
        (26, (0x08, 0x80, 0x80), 2048, 0),
        (27, (0x0C, 0x80, 0x80), 848, 300),
        (29, (0x00, 0x80, 0x80), 2048, 0),
        (42, (0x08, 0x80, 0x80), 2048, 0),
        (43, (0x0C, 0x80, 0x80), 848, 300),
        (45, (0x00, 0x80, 0x80), 2048, 0),
        (58, (0x00, 0x80, 0x00), 2048, 0),         # すべて解放(給電ビットは残る)
    ]


def test_raw_stick_resolution_survives_the_whole_chain(project, host_exe,
                                                       procon_exe, tmp_path):
    """生値の +1 が、コンパイル〜USB レポートまで潰れずに届くこと。

    精密なゲーム挙動検証(1刻みの入力差を見る用途)の前提。
    """
    values = [-2048, -2047, -1, 0, 1, 2046, 2047]
    body = []
    for v in values:
        body.append({"type": "stick", "side": "L", "x": v, "y": 0})
        body.append({"type": "wait", "frames": 10})
    (project.root / "procedures" / "刻み.flow.json").write_text(
        json.dumps({"schema": 1, "name": "刻み", "body": body},
                   ensure_ascii=False), encoding="utf-8")

    r = project.build("刻み")
    emissions = run_c(host_exe, r.blob, tmp_path, 1)
    assert [e[2] for e in emissions] == values   # C エンジンまでは符号付き生値

    device = Device(procon_exe)
    try:
        run_handshake(device)
        wire = []
        for e in emissions:
            device.set_state(lx=e[2])
            wire.append(parse_input_report(device.input_report())["left"][0])
    finally:
        device.close()
    # ワイヤ形式は +2048 の 1:1。隣り合う値の差が保たれている
    assert wire == [v + 2048 for v in values]
    assert wire[1] - wire[0] == 1 and wire[4] - wire[3] == 1


def test_procedure_binary_is_rejected_if_corrupted(project, host_exe, tmp_path):
    """転送経路で1ビット化けたデータは実行前に必ず弾かれること。"""
    r = project.build("通し検証")
    broken = bytearray(r.blob)
    broken[-3] ^= 0x01
    p = tmp_path / "broken.bin"
    p.write_bytes(bytes(broken))
    res = subprocess.run([str(host_exe), str(p), "1", "100000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 5" in res.stdout      # CRC 不一致
