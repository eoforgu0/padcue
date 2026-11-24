"""C 実行エンジン(firmware/components/pademu_core)と Python 参照実装の一致検証。

同一バイナリに対する出力列(絶対フレーム+全状態 12 値)の完全一致を、
固定の手順群+乱数生成の手順群で確認する。C のビルドと gcc がないときの
扱いは conftest.py の core_host_exe / gcc fixture にある。

周回数は手順の中の SETCNT に入っている(変換器が手順全体をくり返しで囲む)
ので、装置にもハーネスにも渡さない。周回数を変えるということは
別のバイナリを作るということなので、周回数は変換のときに与える。
"""
import itertools
import random
import struct
import subprocess
import zlib
from dataclasses import replace

import pytest

from padcue import binfmt, engine
from tests.conftest import CORE
from tests.helpers import compile_nodes, read_c_marks, record_byte, rewrite_byte, run_c


def py_emissions(events):
    return [
        (e.frame, e.buttons, e.lx, e.ly, e.rx, e.ry,
         e.gx, e.gy, e.gz, e.ax, e.ay, e.az)
        for e in engine.run(events)
    ]


def _press(button, frames):
    return {"type": "press", "buttons": [button], "frames": frames}


def _wait(frames):
    return {"type": "wait", "frames": frames}


_loop_ids = itertools.count(1)


def _loop(count, body):
    """くり返し。識別子は 1 つのファイルの中で重複しなければよいので、呼ぶ
    たびに別の値を振る(file-format.md §6-1)。"""
    return {"type": "loop", "id": f"lp-{next(_loop_ids)}", "count": count,
            "body": body}


FIXED_PROGRAMS = [
    [_press("A", 3), _wait(5), _press("B", 3), _wait(5)],
    [_loop(3, [_press("A", 3), _wait(7)])],
    [_loop(2, [_press("A", 2), _loop(3, [_press("B", 1), _wait(4)]), _wait(5)])],
    [{"type": "hold", "buttons": ["ZL"]},
     {"type": "stick", "side": "L", "x": 0, "y": binfmt.STICK_MAX},
     _loop(3, [_press("A", 2), _wait(5)]),
     {"type": "stick", "side": "L", "x": 0, "y": 0},
     {"type": "release", "buttons": ["ZL"]},
     _wait(10)],
    [_press("A", 3), _loop(4, [_wait(10)]), _press("A", 3), _wait(10)],
    # ジャイロ(長さ付き・回しっぱなし)。C エンジンの gx..az 復元も 12 値
    # 完全一致の対象に含める(モーションを動かさない列だけでは穴が残る)
    [{"type": "gyro", "gy": 2000, "frames": 30}, _wait(10),
     {"type": "gyro", "gp": -500, "gr": 700}, _wait(20),
     {"type": "gyro"}, _wait(5)],
    [_loop(3, [{"type": "gyro", "gp": 1000, "frames": 5}, _wait(10)])],
]


@pytest.mark.parametrize("laps", [1, 3])
@pytest.mark.parametrize("i", range(len(FIXED_PROGRAMS)))
def test_fixed_programs_match(core_host_exe, tmp_path, i, laps):
    c = compile_nodes(FIXED_PROGRAMS[i], laps=laps)
    blob = binfmt.encode(c.events, c.total_frames)
    assert run_c(core_host_exe, blob, tmp_path) == py_emissions(c.events)


def _random_body(rng, depth):
    """テスト用の手順をノードの配列として組む(深さは 2 まで)。"""
    body = []
    for _ in range(rng.randint(2, 5)):
        kind = rng.randint(0, 5)
        if kind == 0:
            body.append(_press(rng.choice(["A", "B", "X", "ZL", "DU"]),
                               rng.randint(3, 20)))
        elif kind == 1:
            body.append(_wait(rng.randint(3, 30)))
        elif kind == 2:
            body.append({"type": "stick", "side": rng.choice(["L", "R"]),
                         "x": rng.randint(-2048, 2047),
                         "y": rng.randint(-2048, 2047)})
        elif kind == 3:
            body.append({"type": "hold", "buttons": [rng.choice(["ZR", "L"])]})
        elif kind == 4:
            body.append({"type": "release", "buttons": [rng.choice(["ZR", "L"])]})
        elif kind == 5 and depth < 2:
            body.append(_loop(rng.randint(2, 4),
                              [*_random_body(rng, depth + 1), _wait(5)]))
        else:
            body.append(_wait(rng.randint(3, 30)))
    return body


@pytest.mark.parametrize("seed", range(15))
def test_random_programs_match(core_host_exe, tmp_path, seed):
    rng = random.Random(1000 + seed)
    body = [*_random_body(rng, 0), _wait(10)]
    laps = rng.randint(1, 3)
    c = compile_nodes(body, laps=laps)
    blob = binfmt.encode(c.events, c.total_frames)
    assert run_c(core_host_exe, blob, tmp_path) == py_emissions(c.events)


# ---- 区切りのマーク(レコードの末尾バイト) ----


# 直線手順のレコードの並び。手順全体を囲むくり返しがあるので
# [SETCNT, STATE, STATE, STATE, STATE, DJNZ, STATE, END] になる。最初の
# STATE が周の先頭、DJNZ の次の STATE が最後の周の終わりである
MARKED = [_press("A", 3), _wait(5), _press("B", 3), _wait(5)]


def test_c_accepts_marks_and_reports_them(core_host_exe, tmp_path):
    """変換器が置いたマークを decode が受け付け、ハーネスが見せること。

    マークは時刻にも制御の流れにも影響しないので、出力列は
    マークを外したバイナリとまったく同じでなければならない。
    """
    c = compile_nodes(MARKED)
    marked = binfmt.encode(c.events, c.total_frames)
    stripped = [replace(ev, mark=0)
                if isinstance(ev, (binfmt.State, binfmt.Await)) else ev
                for ev in c.events]
    plain = binfmt.encode(stripped, c.total_frames)
    assert (run_c(core_host_exe, marked, tmp_path)
            == run_c(core_host_exe, plain, tmp_path))
    # レコード番号・マーク・その時点のカウンターの値(「停止した位置」の中身)。
    # カウンター 0 は手順全体を囲むくり返しで、1 周目は 1、抜けた後は 0 である。
    # レコード 1 が周の先頭、レコード 6 が最後の周の終わりである
    assert read_c_marks(core_host_exe, marked, tmp_path) == [
        (1, binfmt.MARK_LAP, {0: 1}),
        (6, binfmt.MARK_LAP, {0: 0}),
    ]


def test_c_rejects_unknown_mark_bits(core_host_exe, tmp_path):
    """知らないビットを立てたレコードを拒否すること(PC 側 binfmt.decode と同じ判定)。

    通すと、装置がそのビットを別の種類の区切りとして読む。
    """
    c = compile_nodes(MARKED)
    plain = binfmt.encode(c.events, c.total_frames)
    bad = rewrite_byte(plain, record_byte(1, 31), 0x08)   # STATE に知らないビット
    p = tmp_path / "bad_mark.bin"
    p.write_bytes(bad)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 12" in res.stdout   # PADEMU_ERR_MARK
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(bad)


def test_c_rejects_a_mark_on_a_record_that_cannot_have_one(core_host_exe, tmp_path):
    """マークを持てるのは STATE と AWAIT だけであること。ほかは末尾が 0 であること。"""
    c = compile_nodes(MARKED)
    plain = binfmt.encode(c.events, c.total_frames)
    assert isinstance(c.events[0], binfmt.SetCnt)   # 前提の確認
    bad = rewrite_byte(plain, record_byte(0, 31), binfmt.MARK_LABEL)
    p = tmp_path / "bad_tail.bin"
    p.write_bytes(bad)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 12" in res.stdout   # PADEMU_ERR_MARK
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(bad)


# ---- 壊れたバイナリの拒否 ----


def test_c_rejects_corruption(core_host_exe, tmp_path):
    c = compile_nodes([_press("A", 5), _wait(5)])
    blob = bytearray(binfmt.encode(c.events, c.total_frames))
    blob[-1] ^= 0xFF
    p = tmp_path / "bad.bin"
    p.write_bytes(bytes(blob))
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 5" in res.stdout  # PADEMU_ERR_CRC


def test_c_rejects_overflowing_event_count(core_host_exe, tmp_path):
    """イベント数が桁あふれを起こす値でも、長さチェックが拒否すること。

    実機の size_t は 32bit なので、`count * 32` と掛けると count が 2^27 で
    積が桁あふれして 0 になる。レコード部が空(ヘッダーの 18 バイトだけ)のデータが
    チェックを通り、直後のループが 1 億 3 千万回ぶん確保していない領域を読む。
    crc の対象はヘッダーだけになるので、値も合わせられる。

    このテスト自体はホスト(64bit)で動くので、掛け算のままでも通ってしまい、
    桁あふれを再現できない。そのため、長さの検査が割り算の形であることは、
    C のソースを読んで確かめる(掛け算に戻すと実機だけで誤る)。
    """
    src = " ".join((CORE / "pademu_core.c").read_text(encoding="utf-8").split())
    assert "count != rec_len / PADEMU_RECORD_SIZE" in src, "長さの検査が割り算でない"
    head = struct.pack("<4sHII", b"PDT0", binfmt.SCHEMA_VERSION,
                       0x08000000, 10)
    blob = head + struct.pack("<I", zlib.crc32(head))
    assert len(blob) == binfmt.HEADER_SIZE
    p = tmp_path / "overflow.bin"
    p.write_bytes(blob)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 4" in res.stdout   # PADEMU_ERR_LENGTH
    # PC 側は多倍長なので元から拒否する(両実装が同じ判定であることの確認)
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(blob)


def test_c_rejects_zero_loop_count(core_host_exe, tmp_path):
    """くり返し回数 0 のバイナリを、装置側も実行前に拒否すること。

    実行は「1 減らしてから 0 か見る」ので、0 から引くと C 側(uint32)が
    アンダーフローして最大値になり、約 42 億回くり返す。Python 側は任意精度なので
    -1 になって読み飛ばす。この不一致を残さないよう、decode の段階で両方が拒否する
    (PC 側は test_binfmt が見ている)。
    """
    blob = binfmt.encode([binfmt.SetCnt(0, 0), binfmt.State(0),
                          binfmt.Djnz(0, target=1, advance=3),
                          binfmt.End()], 3)
    p = tmp_path / "zeroloop.bin"
    p.write_bytes(blob)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 8" in res.stdout   # PADEMU_ERR_ZERO_CYCLE


def test_c_rejects_an_unknown_opcode(core_host_exe, tmp_path):
    """表にない opcode を、装置側も拒否すること。

    PC 側の binfmt.decode も同じ番号を拒否する(test_binfmt)。
    """
    c = compile_nodes(MARKED)
    plain = binfmt.encode(c.events, c.total_frames)
    assert isinstance(c.events[-1], binfmt.End)    # 前提の確認
    bad = rewrite_byte(plain, record_byte(len(c.events) - 1, 4), 7)
    p = tmp_path / "opcode7.bin"
    p.write_bytes(bad)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 6" in res.stdout   # PADEMU_ERR_OPCODE
    with pytest.raises(binfmt.DecodeError):
        binfmt.decode(bad)


def test_a_spent_counter_stops_both_engines(core_host_exe, tmp_path):
    """回数を使い切ったカウンターの DJNZ に来たら、C も Python も異常にすること。

    復号はこの形を通す(変換器は作らないが、形は正しい)。C は uint32 で
    数えるので、0 から減らすとアンダーフローして最大値になり、約 42 億回くり返す。
    Python は -1 になって抜ける。どちらも異常にして、同じバイナリに対する答えを揃える
    (binary-format.md の DJNZ)。
    """
    events = [binfmt.SetCnt(0, 1), binfmt.State(0),
              binfmt.Djnz(0, target=1, advance=10),
              binfmt.Djnz(0, target=1, advance=10), binfmt.End()]
    blob = binfmt.encode(events, 20)
    binfmt.decode(blob)
    p = tmp_path / "spent.bin"
    p.write_bytes(blob)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR step 10" in res.stdout   # PADEMU_ERR_COUNTER
    with pytest.raises(engine.EngineError, match="使い切った"):
        engine.run(events)
    with pytest.raises(engine.WalkError, match="使い切った"):
        engine.walk(events, 0, 0, 0, {}, 20)


@pytest.mark.parametrize("timeout, on_timeout", [
    (10, 3),    # 選択肢の数を超える
    (10, 0),    # 時間切れがあるのに進む選択肢がない
    (0, 1),     # 時間切れがないのに進む選択肢を持つ
])
def test_c_rejects_a_bad_await_timeout(core_host_exe, tmp_path, timeout, on_timeout):
    """時間切れと進む選択肢の組が合わない AWAIT を、両方の実装が拒否すること。

    on_timeout は、時間切れがあれば進む選択肢の番号(1..選択肢の数)で、なければ
    0 である。選択肢の数を超えると、装置は時間切れのたびに選択に失敗し、選択待ち
    から抜けられない。encode は正しい組しか符号化しないので、正しく符号化した
    バイト列の時間切れと番号を書き換えて作る。
    """
    events = [binfmt.Await(0, (1, 2), 10, 1), binfmt.End(), binfmt.End()]
    blob = binfmt.encode(events, 10)
    # AWAIT のレコードの中の位置は binary-format.md の AWAIT レコードのとおり
    blob = rewrite_byte(blob, record_byte(0, 5), timeout)
    blob = rewrite_byte(blob, record_byte(0, 9), on_timeout)
    with pytest.raises(binfmt.DecodeError, match="on_timeout"):
        binfmt.decode(blob)

    p = tmp_path / "bad_on_timeout.bin"
    p.write_bytes(blob)
    res = subprocess.run([str(core_host_exe), str(p), "1000"],
                         capture_output=True, text=True)
    assert res.returncode == 3
    assert "ERR decode 7" in res.stdout   # PADEMU_ERR_TARGET
